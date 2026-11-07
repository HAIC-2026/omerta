"""Web front end for the district-profile agent.

Run locally:
    uvicorn web.app:app --reload

Endpoints:
    GET  /            chat UI (web/static/index.html)
    GET  /api/meta    chambers, valid district numbers and metrics (for the picker)
    POST /chat        {"messages": [{"role": "user"|"assistant", "content": "..."}]}
                      -> answer, citations, low-reliability values and tool results
    GET  /healthz     liveness

The Anthropic API key stays on the server: it is read from .env by agent.chat and never
included in any response.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Literal

import anthropic
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from agent import tools
from agent.grounding import stated_flagged_values

log = logging.getLogger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Limits (override with environment variables)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


MAX_MESSAGES = _env_int("CHAT_MAX_MESSAGES", 20)        # conversation length per request
MAX_MESSAGE_CHARS = _env_int("CHAT_MAX_MESSAGE_CHARS", 2000)
MAX_TOTAL_CHARS = _env_int("CHAT_MAX_TOTAL_CHARS", 20000)
RATE_PER_IP_PER_MINUTE = _env_int("RATE_LIMIT_PER_MINUTE", 6)
RATE_PER_IP_PER_DAY = _env_int("RATE_LIMIT_PER_DAY", 150)
RATE_GLOBAL_PER_MINUTE = _env_int("RATE_LIMIT_GLOBAL_PER_MINUTE", 30)
MAX_CONCURRENT = _env_int("CHAT_MAX_CONCURRENT", 4)
# Only trust X-Forwarded-For when running behind a proxy you control.
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"


# ---------------------------------------------------------------------------
# Rate limiting: in-memory sliding windows (single process)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limits. check() returns seconds to wait, or 0 if allowed."""

    def __init__(self, rules: list[tuple[str, int, int]], clock: Callable[[], float] = time.monotonic):
        # rules: (scope, max_requests, window_seconds); scope is "ip" or "global"
        self.rules = rules
        self.clock = clock
        self._hits: dict[tuple, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, ip: str) -> int:
        now = self.clock()
        with self._lock:
            wait = 0.0
            keys = []
            for scope, limit, window in self.rules:
                key = (scope, window, ip if scope == "ip" else "*")
                q = self._hits[key]
                while q and q[0] <= now - window:
                    q.popleft()
                if limit <= 0 or len(q) >= limit:
                    wait = max(wait, (q[0] + window - now) if q else window)
                keys.append(key)
            if wait > 0:
                return max(1, int(wait + 0.999))
            for key in keys:
                self._hits[key].append(now)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def default_limiter() -> RateLimiter:
    return RateLimiter([
        ("ip", RATE_PER_IP_PER_MINUTE, 60),
        ("ip", RATE_PER_IP_PER_DAY, 86400),
        ("global", RATE_GLOBAL_PER_MINUTE, 60),
    ])


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Agent dependency (overridden in tests)
# ---------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()


def get_agent():
    """A fresh Agent per request, sharing one Anthropic client."""
    global _client
    from agent.chat import Agent, make_client
    with _client_lock:
        if _client is None:
            _client = make_client()  # raises RuntimeError if the key is missing
    return Agent(_client)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("content")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message is empty")
        return v


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)

    @field_validator("messages")
    @classmethod
    def well_formed(cls, msgs: list[Message]) -> list[Message]:
        for i, m in enumerate(msgs):
            expected = "user" if i % 2 == 0 else "assistant"
            if m.role != expected:
                raise ValueError("messages must alternate user/assistant, starting and ending with user")
        if msgs[-1].role != "user":
            raise ValueError("the last message must be from the user")
        if sum(len(m.content) for m in msgs) > MAX_TOTAL_CHARS:
            raise ValueError("conversation is too long; start a new conversation")
        return msgs


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Hawaiʻi District Profiles", docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = default_limiter()
app.state.slots = threading.BoundedSemaphore(MAX_CONCURRENT)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status, headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = str(first.get("msg", "Invalid request")).removeprefix("Value error, ")
    return error(422, "invalid_request", msg)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/meta")
def meta():
    data = tools.load_data()
    md = data["metadata"]
    labels: dict[str, dict] = {}
    for rec in data["districts"]:
        for key, m in rec["metrics"].items():
            labels.setdefault(key, {"key": key, "label": m["label"], "unit": m["unit"]})
    return {
        "dataset": md["dataset"],
        "boundary_vintage": md["boundary_vintage"],
        "chambers": {c: tools._districts_in(c) for c in tools.CHAMBERS},
        "metrics": [labels[k] for k in data["metrics"]],
    }


def _citations(outputs: list[dict]) -> list[str]:
    seen: list[str] = []

    def walk(o: Any):
        if isinstance(o, dict):
            found = [o.get("citation"), *(o.get("citations") or [])]
            for c in found:
                if isinstance(c, str) and c not in seen:
                    seen.append(c)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for out in outputs:
        walk(out)
    # A profile's combined "Tables A, B, C" citation repeats the single-table ones; drop it
    # when every table it names is already cited on its own.
    single = {c for c in seen if " Table " in c}
    def redundant(c: str) -> bool:
        if " Tables " not in c:
            return False
        head, _, rest = c.partition(" Tables ")
        tables, _, tail = rest.partition(";")
        return all(f"{head} Table {t.strip()};{tail}" in single for t in tables.split(","))
    return [c for c in seen if not redundant(c)]


SOURCE_URL_PREFIXES = ("https://api.census.gov/", "https://elections.hawaii.gov/")


def _source_urls(output: dict) -> list[str]:
    urls: list[str] = []

    def walk(o: Any):
        if isinstance(o, dict):
            u = o.get("source_url")
            for x in (u if isinstance(u, list) else [u]):
                if isinstance(x, str) and x.startswith(SOURCE_URL_PREFIXES) and x not in urls:
                    urls.append(x)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(output)
    return urls


@app.post("/chat")
def chat(body: ChatRequest, request: Request, agent=Depends(get_agent)):
    # Sync handler: FastAPI runs it in a worker thread, so the blocking agent call
    # doesn't stall the event loop.
    wait = request.app.state.limiter.check(client_ip(request))
    if wait:
        return error(429, "rate_limited",
                     f"Too many questions in a short time. Please wait {wait} seconds and try again.",
                     headers={"Retry-After": str(wait)})
    slots: threading.BoundedSemaphore = request.app.state.slots
    if not slots.acquire(blocking=False):
        return error(503, "busy", "The service is busy right now. Please try again in a moment.",
                     headers={"Retry-After": "5"})
    try:
        agent.messages = [{"role": m.role, "content": m.content} for m in body.messages[:-1]]
        result = agent.ask(body.messages[-1].content)
    except anthropic.RateLimitError:
        return error(503, "upstream_busy", "The AI service is busy. Please try again in a minute.",
                     headers={"Retry-After": "30"})
    except (anthropic.APIConnectionError, anthropic.APITimeoutError):
        return error(502, "upstream_unreachable", "Couldn't reach the AI service. Please try again.")
    except anthropic.APIStatusError as exc:
        log.warning("Anthropic API error %s (request id %s)", exc.status_code,
                    getattr(exc, "request_id", None))
        return error(502, "upstream_error", "The AI service returned an error. Please try again.")
    except Exception:
        log.exception("chat failed")
        return error(500, "internal_error", "Something went wrong on our side. Please try again.")
    finally:
        slots.release()

    outputs = [c.output for c in result.tool_calls]
    return {
        "answer": result.text,
        "citations": _citations(outputs),
        "low_reliability": stated_flagged_values(result.text, outputs),
        "tool_results": [
            {"tool": c.name, "input": c.input, "is_error": c.is_error,
             "citations": _citations([c.output]), "source_urls": _source_urls(c.output),
             "output": c.output}
            for c in result.tool_calls
        ],
        "stop_reason": result.stop_reason,
        "hit_tool_limit": result.hit_tool_limit,
    }


@app.exception_handler(RuntimeError)
async def not_configured(_request: Request, exc: RuntimeError):
    # make_client() raises RuntimeError when ANTHROPIC_API_KEY is missing.
    log.error("Service not configured: %s", exc)
    return error(503, "not_configured", "The chat service isn't configured on this server.")
