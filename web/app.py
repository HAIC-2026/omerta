"""Web front end for the district-profile agent.

Run locally:
    uvicorn web.app:app --reload

Endpoints:
    GET  /            chat UI (web/static/index.html)
    GET  /api/meta    chambers, valid district numbers and metrics (for the picker)
    POST /chat        {"session_id": "..." | null, "question": "..."}
                      -> session_id, answer, verification, citations, low-reliability values
                         and tool results. The conversation is held on the server.
    GET  /healthz     liveness

The Anthropic API key stays on the server: it is read from .env by agent.chat and never
included in any response.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent import tools
from agent.chat import ServiceNotConfigured
from agent.grounding import (both_listed_in_answer, check_answer_runtime, roster_note_for,
                             stated_flagged_values, supports_answer)
from web.sessions import SessionsFull, SessionStore

log = logging.getLogger("web")

# One JSON line per /chat request: latency, tool calls, tokens and outcome. Never the question,
# the answer or tool inputs. Written to stdout so the host's log stream (Render logs) shows it.
request_log = logging.getLogger("web.requests")
if not request_log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    request_log.addHandler(_h)
    request_log.setLevel(logging.INFO)
    request_log.propagate = False

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Limits (override with environment variables)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


MAX_MESSAGES = _env_int("CHAT_MAX_MESSAGES", 20)        # questions + answers per conversation
MAX_MESSAGE_CHARS = _env_int("CHAT_MAX_MESSAGE_CHARS", 2000)
MAX_TOTAL_CHARS = _env_int("CHAT_MAX_TOTAL_CHARS", 20000)
RATE_PER_IP_PER_MINUTE = _env_int("RATE_LIMIT_PER_MINUTE", 6)
RATE_PER_IP_PER_DAY = _env_int("RATE_LIMIT_PER_DAY", 150)
RATE_GLOBAL_PER_MINUTE = _env_int("RATE_LIMIT_GLOBAL_PER_MINUTE", 30)
RATE_GLOBAL_PER_DAY = _env_int("RATE_LIMIT_GLOBAL_PER_DAY", 500)   # whole-site daily cap on questions
MAX_CONCURRENT = _env_int("CHAT_MAX_CONCURRENT", 4)
SESSION_TTL_MINUTES = _env_int("SESSION_TTL_MINUTES", 60)  # idle time before a conversation expires
SESSION_MAX = _env_int("SESSION_MAX", 200)                # conversations held in memory at once
MAX_BODY_BYTES = 16 * 1024   # POST /chat body cap; a 2,000-character question fits many times over
# How many proxies in front of the app append to X-Forwarded-For (0 = not behind a proxy;
# the header is ignored). Render needs 2; see client_ip().
TRUST_PROXY = max(0, _env_int("TRUST_PROXY", 0))


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
        return self.check_detail(ip)[0]

    def check_detail(self, ip: str) -> tuple[int, list[tuple[str, int, int]]]:
        """(seconds to wait, the rules that blocked). Allowed requests return (0, [])."""
        now = self.clock()
        with self._lock:
            wait = 0.0
            keys, blocked = [], []
            for rule in self.rules:
                scope, limit, window = rule
                key = (scope, window, ip if scope == "ip" else "*")
                q = self._hits[key]
                while q and q[0] <= now - window:
                    q.popleft()
                if limit <= 0 or len(q) >= limit:
                    wait = max(wait, (q[0] + window - now) if q else window)
                    blocked.append(rule)
                keys.append(key)
            if wait > 0:
                return max(1, int(wait + 0.999)), blocked
            for key in keys:
                self._hits[key].append(now)
            return 0, []

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def default_limiter() -> RateLimiter:
    return RateLimiter([
        ("ip", RATE_PER_IP_PER_MINUTE, 60),
        ("ip", RATE_PER_IP_PER_DAY, 86400),
        ("global", RATE_GLOBAL_PER_MINUTE, 60),
        ("global", RATE_GLOBAL_PER_DAY, 86400),
    ])


UNVERIFIED_CLIENT = "unverified-forwarding"


def client_ip(request: Request) -> str:
    """The client's IP for rate limiting.

    Behind TRUST_PROXY=N proxies, each proxy appends the address it received the request
    from, so the client is the Nth X-Forwarded-For entry from the RIGHT. Anything further
    left is whatever the client sent and can be forged to dodge the limits.

    On Render, N is 2. Render doesn't document its X-Forwarded-For format. It says only that
    its load balancer terminates TLS and forwards to the service
    (https://render.com/docs/web-services#connecting-from-the-public-internet). Requests
    arrive through Cloudflare, which appends the client's IP to any X-Forwarded-For the
    client sent (https://developers.cloudflare.com/fundamentals/reference/http-headers/).
    Measured on the live service (2026-11-13): a plain request arrives with 2 entries,
    the client's IP (equal to CF-Connecting-IP) and then a proxy address. Forged entries are
    kept, in front of the client's IP. So the last entry is a proxy, and the client is 2nd from the right.

    If there are fewer than N entries, the request didn't come through the expected proxies,
    so it goes into one shared bucket. It never falls back to request.client.host, because
    on Render uvicorn rewrites that from X-Forwarded-For (Render sets
    FORWARDED_ALLOW_IPS=*, https://render.com/docs/environment-variables#python-3),
    so it is the forgeable leftmost entry."""
    if TRUST_PROXY:
        entries = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",")]
        if len(entries) >= TRUST_PROXY and entries[-TRUST_PROXY]:
            return entries[-TRUST_PROXY]
        return UNVERIFIED_CLIENT
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

class ChatRequest(BaseModel):
    """The client sends only its session ID (if it has one) and the new question. Anything
    else, such as a client-written conversation history, is rejected."""
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, max_length=64)
    question: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question is empty")
        return v


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Hawaiʻi District Profiles", docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = default_limiter()
app.state.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
app.state.sessions = SessionStore(ttl_seconds=SESSION_TTL_MINUTES * 60, max_sessions=SESSION_MAX)

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


class ChatBodyLimit:
    """Reject POST /chat bodies over MAX_BODY_BYTES with a JSON 413 before they are parsed.
    Checks Content-Length first, then counts the bytes actually received (a chunked or
    mislabelled body can't get past it). The body, at most the cap, is buffered and replayed."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/chat":
            return await self.app(scope, receive, send)
        too_large = JSONResponse(
            {"error": "That request is too large. Questions are limited to "
                      f"{MAX_MESSAGE_CHARS:,} characters.", "code": "payload_too_large"},
            status_code=413, headers={"Connection": "close"})
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return await too_large(scope, receive, send)
        chunks, size, more = [], 0, True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":      # client disconnected
                return
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return await too_large(scope, receive, send)
            chunks.append(chunk)
            more = msg.get("more_body", False)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


app.add_middleware(ChatBodyLimit)   # added before security_headers, so its 413 gets those headers


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:
        # Last resort, caught here so the exception never reaches the server's own error
        # logger (which would print a traceback). Log the type and an ID only: an exception
        # message can quote request input. Never send a stack trace to the browser.
        rid = uuid.uuid4().hex[:16]
        log.error("unhandled error: %s request_id=%s", type(exc).__name__, rid)
        response = error(500, "internal_error", "Something went wrong on our side. Please try again.",
                         headers={"X-Request-ID": rid})
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # The pages use no camera, microphone or location. HSTS is only honoured over HTTPS.
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
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
    """Readiness: data files loaded, chat configured. Carries only what monitoring needs:
    ready, counts, data dates and whether chat is available. ?deep=1 adds the AI service
    check (no tokens used). No operating config (model, effort, limits, proxy hops) is
    exposed. Data features stay up even when the AI service is down."""
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
    from agent.legislator_tools import load_legislators
    roster = load_legislators()
    # Districts the roster covers: one serving member, or two the site lists side by side.
    checks["legislators_serving"] = (len({(m["chamber"], m["district"]) for m in roster["legislators"]
                                          if m["serving"]}) if roster else 0)
    checks["legislators_fetched"] = roster["metadata"]["date_fetched"] if roster else None
    checks["chat_configured"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    if deep:
        checks["ai_service"] = _check_model_api()
    data_ok = checks["acs_districts"] > 0
    chat_ok = checks["chat_configured"] and checks.get("ai_service", "ok") == "ok"
    body = {"ready": data_ok, "chat_available": chat_ok, "checks": checks}
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


SOURCE_URL_PREFIXES = ("https://api.census.gov/", "https://elections.hawaii.gov/",
                       "https://www.capitol.hawaii.gov/") + _program_source_prefixes()


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
    rec: dict[str, Any] = {"event": "chat", "request_id": uuid.uuid4().hex[:16]}
    start = time.perf_counter()
    response: Any = None
    try:
        response = _chat(body, request, agent, rec)
        if isinstance(response, dict):
            response = JSONResponse(response)
        return response
    finally:
        rec["latency_ms"] = round((time.perf_counter() - start) * 1000)
        if isinstance(response, JSONResponse):
            rec["status"] = response.status_code
            try:
                rec["code"] = json.loads(response.body).get("code")
            except Exception:
                pass
            response.headers["X-Request-ID"] = rec["request_id"]
        else:
            rec["status"] = 200 if response is not None else 500
        log_request(rec)


def log_request(rec: dict) -> None:
    request_log.info(json.dumps(rec, separators=(",", ":"), sort_keys=True))


def _usage_fields(usage) -> dict:
    """Token counts for the log line, including prompt-cache reads and writes, totalled and
    per API call (a healthy follow-up reads the earlier conversation from cache)."""
    out = {k: getattr(usage, k, 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    prompt = out["input_tokens"] + out["cache_read_input_tokens"] + out["cache_creation_input_tokens"]
    out["cache_hit_ratio"] = round(out["cache_read_input_tokens"] / prompt, 3) if prompt else None
    out["api_requests"] = getattr(usage, "requests", 0)
    out["api_calls"] = list(getattr(usage, "per_call", []) or [])
    return out


def _chat(body: ChatRequest, request: Request, agent, rec: dict):
    wait, blocked = request.app.state.limiter.check_detail(client_ip(request))
    if any(scope == "global" and window >= 86400 for scope, _, window in blocked):
        return error(429, "daily_limit",
                     "This demo has reached its limit of questions for today, so the AI chat is paused "
                     "until the limit resets. The district map, one-page profiles and about page still "
                     "work.", headers={"Retry-After": str(wait)}, alternatives=True)
    if wait:
        return error(429, "rate_limited",
                     f"Too many questions in a short time. Please wait {wait} seconds and try again.",
                     headers={"Retry-After": str(wait)})
    store: SessionStore = request.app.state.sessions
    status = "continued"
    session = store.get(body.session_id) if body.session_id else None
    if session is None:
        status = "expired" if body.session_id else "new"
        try:
            session = store.create()
        except SessionsFull:
            return error(503, "busy", "The service is busy right now. Please try again in a moment.",
                         headers={"Retry-After": "5"})
    if len(session.questions) >= MAX_MESSAGES // 2 or             session.total_question_chars + len(body.question) > MAX_TOTAL_CHARS:
        return error(422, "conversation_full", "This conversation has reached its length limit. "
                     "Start a new conversation to keep asking questions.")
    if not session.lock.acquire(blocking=False):
        return error(409, "conversation_busy", "Still answering your previous question. Please wait "
                     "for it to finish.")
    slots: threading.BoundedSemaphore = request.app.state.slots
    if not slots.acquire(blocking=False):
        session.lock.release()
        return error(503, "busy", "The service is busy right now. Please try again in a moment.",
                     headers={"Retry-After": "5"})
    rec["session_status"] = status
    rec["session_turn"] = len(session.questions) + 1
    questions = session.questions + [body.question]
    prior = list(session.tool_outputs)
    try:
        agent.messages = list(session.messages)
        result = agent.ask(body.question)
        result, check, retried = verify_with_retry(agent, result, prior, questions)
        rec.update({
            "tool_calls": len(result.tool_calls),
            "tools": [c.name for c in result.tool_calls],
            "tool_errors": sum(1 for c in result.tool_calls if c.is_error),
            "verified": check["verified"], "retried": retried,
            "numbers_checked": check["numbers_checked"], "numbers_supported": check["numbers_supported"],
            "stop_reason": result.stop_reason, "models": sorted(set(result.models)),
            **_usage_fields(result.usage),
        })
    except anthropic.APIError as exc:
        rec["upstream_error"] = type(exc).__name__
        return upstream_error(exc)
    except Exception as exc:
        # Type and request ID only: an exception message or traceback can quote the question.
        rec["error_type"] = type(exc).__name__
        log.error("chat failed: %s request_id=%s", type(exc).__name__, rec["request_id"])
        return error(500, "internal_error", "Something went wrong on our side. Please try again.",
                     alternatives=True)
    else:
        # Commit the turn only once it has a final answer.
        session.messages = list(agent.messages)
        turn = len(session.questions) + 1
        session.tool_calls.extend({"tool": c.name, "input": c.input, "is_error": c.is_error,
                                   "output": c.output, "turn": turn} for c in result.tool_calls)
        session.questions.append(body.question)
    finally:
        slots.release()
        session.lock.release()
        if not session.questions:
            store.discard(session.id)  # a brand-new conversation whose first question failed
        else:
            store.touch(session)

    all_outputs = session.tool_outputs
    lookups = answer_lookups(result.text, session.tool_calls, len(session.questions))
    return {
        "session_id": session.id,
        "session_status": status,
        "answer": result.text,
        "verification": verification_summary(check, retried),
        "citations": _citations([t["output"] for t in lookups]),
        "low_reliability": stated_flagged_values(result.text, all_outputs),
        # Shown under any answer that names a legislator (or found no one), so the roster's
        # date travels with the name even if the model's wording drops it.
        "roster_note": roster_note_for(result.text, all_outputs),
        # Districts named in the answer for which the site lists two members: both names with
        # the site's status text, shown with the note whatever the model wrote.
        "roster_both_listed": [b["listing_statement"] for b in both_listed_in_answer(result.text, all_outputs)],
        # This turn's lookups, then earlier lookups in the conversation that support a number
        # or fact in this answer (reused: true), so a follow-up answered from earlier data
        # still shows where it came from.
        "tool_results": [
            {"tool": t["tool"], "input": t["input"], "is_error": t["is_error"],
             "reused": t["turn"] != len(session.questions), "turn": t["turn"],
             "citations": _citations([t["output"]]), "source_urls": _source_urls(t["output"]),
             "output": t["output"]}
            for t in lookups
        ],
        "stop_reason": result.stop_reason,
        "hit_tool_limit": result.hit_tool_limit,
    }


def answer_lookups(text: str, calls: list[dict], turn: int) -> list[dict]:
    """The tool calls an answer's Sources panel lists: every call made this turn, then each
    earlier call in the session whose result supports a number or fact the answer states
    (not failed calls, and only the latest of identical lookups or one this turn repeated)."""
    def key(c):
        return c["tool"], json.dumps(c["input"], sort_keys=True, default=str)
    current = [c for c in calls if c["turn"] == turn]
    seen = {key(c) for c in current}
    earlier: list[dict] = []
    for c in reversed([c for c in calls if c["turn"] < turn]):
        if c["is_error"] or key(c) in seen or not supports_answer(text, c["output"]):
            continue
        seen.add(key(c))
        earlier.append(c)
    return current + earlier[::-1]


def verify_with_retry(agent, result, prior_outputs: list[dict], questions: list[str]):
    """Check the answer against every tool output it may use. On failure, ask the model ONCE to
    fix the listed problems. Returns (result, check, retried); result.tool_calls includes the
    calls from both attempts. If the retry itself fails at the API, the first answer is kept,
    unverified."""
    def run_check(r):
        return check_answer_runtime(r.text, prior_outputs + [c.output for c in r.tool_calls], questions)

    check = run_check(result)
    if check["verified"] or not hasattr(agent, "revise"):
        return result, check, False
    try:
        second = agent.revise(check["failures"])
    except anthropic.APIError as exc:
        log.warning("verification retry failed at the API: %s", type(exc).__name__)
        return result, check, True
    second.tool_calls = result.tool_calls + second.tool_calls
    second.usage.merge(result.usage)
    second.hit_tool_limit = second.hit_tool_limit or result.hit_tool_limit
    return second, run_check(second), True


def verification_summary(check: dict, retried: bool) -> dict:
    return {
        "verified": check["verified"],
        "numbers_checked": check["numbers_checked"],
        "numbers_supported": check["numbers_supported"],
        "retried": retried,
        "issues": check["failures"],
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
    # Fallback for errors outside the middleware above. Type only, never the message.
    log.error("unhandled error: %s", type(exc).__name__)
    return error(500, "internal_error", "Something went wrong on our side. Please try again.")
