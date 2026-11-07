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
from agent.chat import ServiceNotConfigured
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

# Cross-origin access is off unless ALLOWED_ORIGINS lists origins (comma-separated),
# e.g. ALLOWED_ORIGINS=https://haic.example.org
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if ALLOWED_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], allow_credentials=False, max_age=600)

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


ALTERNATIVES = [{"label": "District map", "url": "/map"}, {"label": "About this tool", "url": "/about"}]


def error(status: int, code: str, message: str, headers: dict | None = None,
          alternatives: bool = False) -> JSONResponse:
    body: dict[str, Any] = {"error": message, "code": code}
    if alternatives:
        body["alternatives"] = ALTERNATIVES
    return JSONResponse(body, status_code=status, headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = str(first.get("msg", "Invalid request")).removeprefix("Value error, ")
    return error(422, "invalid_request", msg)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/district/{chamber}/{district}/profile", include_in_schema=False)
def district_profile(chamber: str, district: str, download: int = 0):
    """Print-ready one-page profile built only from tool data (no model involved)."""
    from fastapi.responses import HTMLResponse
    from web.profile_page import render
    status, html = render(chamber, district)
    headers = {"Cache-Control": "no-cache"}
    if download and status == 200:
        headers["Content-Disposition"] = (f'attachment; filename="hawaii-{chamber.lower()}-district-'
                                          f'{int(district)}-profile.html"')
    return HTMLResponse(html, status_code=status, headers=headers)


SHAPES_PATH = Path(__file__).resolve().parent.parent / "data" / "district_shapes.json"
ISLAND_GROUPS = {"Oahu": "oahu", "Hawaii": "hawaii", "Maui": "maui", "Molokai": "maui", "Lanai": "maui",
                 "Kahoolawe": "maui", "Kauai": "kauai", "Niihau": "kauai"}


def _shapes() -> dict | None:
    if not hasattr(_shapes, "cache"):
        _shapes.cache = (__import__("json").loads(SHAPES_PATH.read_text(encoding="utf-8"))
                         if SHAPES_PATH.exists() else None)
    return _shapes.cache


@app.get("/map", include_in_schema=False)
def map_page():
    return FileResponse(STATIC_DIR / "map.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/map")
def map_data(chamber: str = "house", metric: str = "poverty_rate"):
    """District shapes joined with one metric's tool values (value, MOE, reliability)."""
    shapes = _shapes()
    if shapes is None:
        return error(503, "map_unavailable", "Map boundaries haven't been built on this server.")
    ch, err = tools._check_chamber(chamber)
    if err:
        return error(422, "invalid_request", err["error"])
    m, err = tools._check_metric(metric)
    if err:
        return error(422, "invalid_request", err["error"])
    data = tools.load_data()
    ctx = tools.load_context()
    rows = []
    for d in tools._districts_in(ch):
        mv = tools._metric_view(data["index"][(ch, d)]["metrics"][m])
        geo = shapes["chambers"][ch].get(str(d))
        isl = None
        if ctx and (ch, d) in ctx["index"]:
            isls = ctx["index"][(ch, d)].get("boundary_description", {}).get("islands") or []
            isl = next((ISLAND_GROUPS[i] for i in isls if i in ISLAND_GROUPS), None)
        rows.append({
            "district": d, "path": geo["d"] if geo else None, "bbox": geo["bbox"] if geo else None,
            "island_group": isl, "value": mv["value"], "value_display": mv["value_display"],
            "moe_display": mv["moe_display"], "low_reliability": mv["low_reliability"],
            "moe_percent_of_estimate": mv["moe_percent_of_estimate"], "caveat": mv.get("caveat"),
        })
    first = data["index"][(ch, tools._districts_in(ch)[0])]["metrics"][m]
    return {
        "chamber": ch, "metric": m, "label": first["label"], "unit": first["unit"],
        "view_box": shapes["view_box"], "districts": rows,
        "citation": tools._citation([first["table_id"]]),
        "boundaries": shapes["source"], "not_drawn": shapes["not_drawn"],
        "low_reliability_rule": data["metadata"].get("low_reliability_rule"),
    }


@app.get("/healthz")
def healthz():
    """Liveness: the process is up."""
    return {"ok": True}


_deep_cache: dict[str, Any] = {}


def _check_model_api() -> str:
    """Cheap upstream check (models.retrieve uses no tokens). Cached for 60 s. Never returns
    upstream error text."""
    now = time.monotonic()
    if _deep_cache.get("at") and now - _deep_cache["at"] < 60:
        return _deep_cache["status"]
    from agent.chat import MODEL, make_client
    try:
        make_client().with_options(timeout=10, max_retries=0).models.retrieve(MODEL)
        status = "ok"
    except ServiceNotConfigured:
        status = "not_configured"
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        status = "auth_error"
    except (anthropic.APIConnectionError, anthropic.APITimeoutError):
        status = "unreachable"
    except anthropic.APIError:
        status = "error"
    _deep_cache.update(at=now, status=status)
    return status


@app.get("/readyz")
def readyz(deep: int = 0):
    """Readiness: data files loaded, chat configured. ?deep=1 also checks the AI service
    (no tokens used). Data features stay up even when the AI service is down."""
    from agent.chat import EFFORT, MODEL
    checks: dict[str, Any] = {}
    try:
        data = tools.load_data()
        checks["acs_districts"] = len(data["districts"])
    except Exception:
        checks["acs_districts"] = 0
    ctx = tools.load_context()
    checks["district_context"] = len(ctx["districts"]) if ctx else 0
    try:
        from agent.program_tools import load_programs
        pr = load_programs()
        checks["programs"] = len(pr["programs"])
        checks["programs_verified"] = pr["metadata"].get("date_verified")
    except Exception:
        checks["programs"] = 0
    checks["map_shapes"] = _shapes() is not None
    checks["chat_configured"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    if deep:
        checks["ai_service"] = _check_model_api()
    data_ok = checks["acs_districts"] > 0
    chat_ok = checks["chat_configured"] and checks.get("ai_service", "ok") == "ok"
    body = {"ready": data_ok, "chat_available": chat_ok, "checks": checks,
            "config": {"model": MODEL, "effort": EFFORT, "rate_limit_per_minute": RATE_PER_IP_PER_MINUTE,
                       "rate_limit_per_day": RATE_PER_IP_PER_DAY, "rate_limit_global_per_minute":
                       RATE_GLOBAL_PER_MINUTE, "max_concurrent": MAX_CONCURRENT,
                       "allowed_origins": ALLOWED_ORIGINS}}
    return JSONResponse(body, status_code=200 if data_ok else 503)


@app.get("/about", include_in_schema=False)
def about():
    from fastapi.responses import HTMLResponse
    from web.about_page import render as render_about
    return HTMLResponse(render_about(), headers={"Cache-Control": "no-cache"})


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


def _program_source_prefixes() -> tuple[str, ...]:
    try:
        from agent.program_tools import load_programs
        doms = load_programs()["metadata"]["allowed_source_domains"]
    except Exception:
        return ()
    return tuple(f"https://{d}/" for d in doms) + tuple(f"https://www.{d}/" for d in doms)


SOURCE_URL_PREFIXES = ("https://api.census.gov/", "https://elections.hawaii.gov/") + _program_source_prefixes()


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
    except anthropic.APIError as exc:
        return upstream_error(exc)
    except Exception:
        log.exception("chat failed")
        return error(500, "internal_error", "Something went wrong on our side. Please try again.",
                     alternatives=True)
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


DATA_STILL_WORKS = " The district map and district profiles don't use the AI service and still work."


def upstream_error(exc: Exception) -> JSONResponse:
    """Map an Anthropic API failure to a clear, generic message. Never echoes upstream detail."""
    status = getattr(exc, "status_code", None)
    rid = getattr(exc, "request_id", None)
    log.warning("Anthropic API failure: %s status=%s request_id=%s", type(exc).__name__, status, rid)
    msg = str(getattr(exc, "message", "") or "").lower()
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return error(502, "upstream_unreachable",
                     "Couldn't reach the AI service. Please try again in a moment." + DATA_STILL_WORKS,
                     alternatives=True)
    if isinstance(exc, anthropic.RateLimitError):
        return error(503, "upstream_busy", "The AI service is handling too many requests. Please try again "
                     "in a minute." + DATA_STILL_WORKS, headers={"Retry-After": "30"}, alternatives=True)
    if status == 400 and ("credit" in msg or "billing" in msg or "balance" in msg):
        return error(503, "upstream_unavailable", "The AI service is temporarily unavailable for this demo "
                     "(its usage limit has been reached)." + DATA_STILL_WORKS, alternatives=True)
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return error(503, "upstream_unavailable", "The AI service isn't available on this server right now."
                     + DATA_STILL_WORKS, alternatives=True)
    if status in (500, 502, 503, 504, 529) or isinstance(exc, anthropic.InternalServerError):
        return error(503, "upstream_busy", "The AI service is temporarily overloaded or down. Please try "
                     "again in a few minutes." + DATA_STILL_WORKS, headers={"Retry-After": "60"},
                     alternatives=True)
    return error(502, "upstream_error", "The AI service returned an error. Please try again."
                 + DATA_STILL_WORKS, alternatives=True)


@app.exception_handler(ServiceNotConfigured)
async def not_configured(_request: Request, exc: ServiceNotConfigured):
    log.error("Service not configured: %s", exc)
    return error(503, "not_configured", "The chat service isn't configured on this server."
                 + DATA_STILL_WORKS, alternatives=True)


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    # Last resort: log server-side, never send a stack trace to the browser.
    log.exception("unhandled error")
    return error(500, "internal_error", "Something went wrong on our side. Please try again.")
