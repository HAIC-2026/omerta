"""Offline tests for demo hardening: health/readiness, AI-service failure handling, no stack
traces, environment configuration, CORS and the /about page. No API calls are made."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from agent import tools
from agent.program_tools import load_programs
from web import app as webapp

FAKE_KEY = "sk-ant-test-FAKE-hardening-key-000"


def _status_error(cls, status, message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(status, request=req), body=None)


class Boom:
    def __init__(self, exc):
        self.exc, self.messages = exc, []

    def ask(self, q):
        raise self.exc


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    webapp.app.state.limiter = webapp.default_limiter()
    webapp._deep_cache.clear()
    yield TestClient(webapp.app, raise_server_exceptions=False)
    webapp.app.dependency_overrides.clear()


def ask(client):
    return client.post("/chat", json={"question": "q"})


# ---------------------------------------------------------------- health

def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_readyz_reports_data_but_not_config(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    j = r.json()
    assert j["ready"] is True and j["chat_available"] is True
    c = j["checks"]
    assert c["acs_districts"] == len(tools.load_data()["districts"])
    assert c["district_context"] == c["acs_districts"]
    assert c["programs"] == len(load_programs()["programs"])
    assert c["programs_verified"] == load_programs()["metadata"]["date_verified"]
    assert c["map_shapes"] is True and c["chat_configured"] is True
    assert "ai_service" not in c  # deep check only on request
    # The default response is only what monitoring needs: no model, effort, limits or proxy mode.
    assert set(j) == {"ready", "chat_available", "checks"}
    assert set(c) == {"acs_districts", "district_context", "programs", "programs_verified", "map_shapes",
                      "legislators_serving", "legislators_fetched", "chat_configured"}
    for hidden in ("claude", "model", "effort", "rate_limit", "trust_proxy", "max_concurrent", "allowed_origins"):
        assert hidden not in r.text
    assert FAKE_KEY not in r.text and "sk-ant" not in r.text


def test_readyz_deep_adds_only_the_ai_service_check(client, monkeypatch):
    monkeypatch.setattr(webapp, "_check_model_api", lambda: "ok")
    plain, r = client.get("/readyz").json(), client.get("/readyz", params={"deep": 1})
    j = r.json()
    assert set(j) == {"ready", "chat_available", "checks"}
    assert set(j["checks"]) == set(plain["checks"]) | {"ai_service"} and j["checks"]["ai_service"] == "ok"
    for hidden in ("claude", "model", "effort", "rate_limit", "trust_proxy", "max_concurrent", "allowed_origins"):
        assert hidden not in r.text
    assert FAKE_KEY not in r.text and "sk-ant" not in r.text


# ---------------------------------------------------------------- request body cap

def _chat_post(client, content: bytes, **headers):
    return client.post("/chat", content=content, headers={"content-type": "application/json", **headers})


def test_chat_body_over_the_cap_is_a_clean_413(client):
    big = json.dumps({"question": "hi", "session_id": None, "pad": "x" * webapp.MAX_BODY_BYTES}).encode()
    r = _chat_post(client, big)
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large" and "too large" in r.json()["error"]
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"      # security headers still applied
    assert FAKE_KEY not in r.text


def test_chat_body_cap_counts_bytes_when_no_length_is_declared(client):
    def chunks():
        for _ in range(5):
            yield b" " * 4096                                      # 20 KB, sent chunked
    r = client.post("/chat", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_chat_body_at_the_cap_still_reaches_validation_and_normal_questions_work(client):
    from tests.test_web import FakeAgent
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: FakeAgent("ok")   # no network
    exact = b'{"question": "hi"}'.ljust(webapp.MAX_BODY_BYTES)        # trailing spaces: valid JSON
    assert len(exact) == webapp.MAX_BODY_BYTES
    assert _chat_post(client, exact).status_code == 200
    longest = json.dumps({"question": "ʻ" * webapp.MAX_MESSAGE_CHARS}).encode()   # multi-byte, max length
    assert len(longest) < webapp.MAX_BODY_BYTES
    assert _chat_post(client, longest).status_code == 200
    assert _chat_post(client, exact + b" ").status_code == 413


def test_body_cap_applies_only_to_chat(client):
    assert client.get("/readyz").status_code == 200
    assert client.post("/healthz", content=b"x" * (webapp.MAX_BODY_BYTES + 1)).status_code == 405


# ---------------------------------------------------------------- map -> chat handoff

def test_map_hands_the_question_over_in_the_url_fragment():
    """Fragments are never sent to the server, so the question can't reach access logs."""
    js = (webapp.STATIC_DIR / "map.js").read_text(encoding="utf-8")
    assert '"/#ask="' in js and "?ask=" not in js


def test_handed_over_question_is_prefilled_never_auto_submitted():
    js = (webapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    block = js[js.index("var handed = null;"):js.index('if (document.readyState === "loading")')]
    assert "window.location.hash" in block and "window.location.search" in block   # old ?ask= links too
    assert "replaceState" in block                       # stripped from the address bar
    assert "textarea.value = handed.slice(0, 2000)" in block and "textarea.focus()" in block
    assert "ask(" not in block and "submit" not in block.lower().replace("never sent automatically", "")


def test_readyz_without_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    j = client.get("/readyz").json()
    assert j["ready"] is True and j["chat_available"] is False
    assert j["checks"]["chat_configured"] is False


@pytest.mark.parametrize("exc,status", [
    (None, "ok"),
    (_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), "auth_error"),
    (anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.anthropic.com")), "unreachable"),
    (_status_error(anthropic.InternalServerError, 500, "boom"), "error"),
])
def test_readyz_deep(client, monkeypatch, exc, status):
    class Models:
        def retrieve(self, model):
            if exc:
                raise exc
            return {"id": model}

    class Client:
        models = Models()

        def with_options(self, **kw):
            return self

    monkeypatch.setattr("agent.chat.make_client", lambda: Client())
    j = client.get("/readyz", params={"deep": 1}).json()
    assert j["checks"]["ai_service"] == status
    assert j["chat_available"] is (status == "ok")
    assert "invalid x-api-key" not in json.dumps(j)


# ---------------------------------------------------------------- AI service failures

@pytest.mark.parametrize("exc,status,code", [
    (_status_error(anthropic.BadRequestError, 400,
                   "Your credit balance is too low to access the Anthropic API."), 503, "upstream_unavailable"),
    (_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), 503, "upstream_unavailable"),
    (_status_error(anthropic.PermissionDeniedError, 403, "forbidden"), 503, "upstream_unavailable"),
    (_status_error(anthropic.RateLimitError, 429, "rate limited"), 503, "upstream_busy"),
    (_status_error(anthropic.APIStatusError, 529, "overloaded_error: upstream detail 529"), 503, "upstream_busy"),
    (_status_error(anthropic.InternalServerError, 500, "internal"), 503, "upstream_busy"),
    (anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com")), 502, "upstream_unreachable"),
    (_status_error(anthropic.BadRequestError, 400, "messages: invalid"), 502, "upstream_error"),
])
def test_ai_service_failures_are_clear_and_generic(client, exc, status, code):
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: Boom(exc)
    r = ask(client)
    assert r.status_code == status
    j = r.json()
    assert j["code"] == code
    assert "map" in j["error"].lower()  # points to features that still work
    assert {a["url"] for a in j["alternatives"]} == {"/map", "/about"}
    # The upstream message, keys and internals never reach the browser.
    upstream = getattr(exc, "message", "") or ""
    for leak in (upstream, "x-api-key", "Traceback", "anthropic", FAKE_KEY):
        if leak:
            assert leak.lower() not in r.text.lower(), leak


def test_missing_key_points_to_data_features(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(webapp, "_client", None)
    monkeypatch.setattr("agent.chat.load_dotenv", lambda *a, **k: None)
    r = ask(client)
    assert r.status_code == 503 and r.json()["code"] == "not_configured"
    assert r.json()["alternatives"]


def test_unhandled_errors_never_show_a_stack_trace(client, monkeypatch, caplog, capfd):
    def broken():
        raise KeyError("internal detail /secret/path")
    monkeypatch.setattr(tools, "load_data", broken)
    with caplog.at_level("DEBUG"):
        r = client.get("/api/meta")
    assert r.status_code == 500
    assert r.json() == {"error": "Something went wrong on our side. Please try again.", "code": "internal_error"}
    assert "Traceback" not in r.text and "secret" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    # The log has the exception type and the request ID, never the message or a traceback.
    rid = r.headers["x-request-id"]
    logged = caplog.text + "".join(capfd.readouterr())
    assert f"unhandled error: KeyError request_id={rid}" in logged
    assert "secret" not in logged and "internal detail" not in logged and "Traceback" not in logged
    assert all(rec.exc_info is None for rec in caplog.records)


def test_a_failed_chat_never_logs_the_question_or_the_exception_message(client, caplog, capfd):
    from tests.test_web import FakeAgent
    question = "What about my-private-question-text in District 7?"
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: FakeAgent(
        "x", exc=ValueError(f"could not parse input: {question}"))
    with caplog.at_level("DEBUG"):
        r = client.post("/chat", json={"question": question})
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    rid = r.headers["x-request-id"]
    logged = caplog.text + "".join(capfd.readouterr())
    assert f"chat failed: ValueError request_id={rid}" in logged
    assert '"error_type":"ValueError"' in logged              # the request log line has the type too
    assert "my-private-question-text" not in logged and "could not parse" not in logged
    assert "Traceback" not in logged and all(rec.exc_info is None for rec in caplog.records)


# ---------------------------------------------------------------- configuration

def _run_py(code: str, **env) -> str:
    e = {**os.environ, **env}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e,
                         cwd=str(webapp.STATIC_DIR.parent.parent), timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    return out.stdout.strip()


def test_env_config_model_and_limits():
    out = _run_py("import agent.chat as c, web.app as w; "
                  "print(c.MODEL, c.EFFORT, c.MAX_TOKENS, w.RATE_PER_IP_PER_MINUTE, w.ALLOWED_ORIGINS)",
                  CHAT_MODEL="claude-test-model", CHAT_EFFORT="low", CHAT_MAX_TOKENS="1234",
                  RATE_LIMIT_PER_MINUTE="3", ALLOWED_ORIGINS="https://a.example, https://b.example")
    assert out == "claude-test-model low 1234 3 ['https://a.example', 'https://b.example']"


def test_cors_off_by_default(client):
    r = client.get("/api/meta", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_only_listed_origins(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://haic.example")
    try:
        mod = importlib.reload(webapp)
        c = TestClient(mod.app)
        ok = c.get("/api/meta", headers={"Origin": "https://haic.example"})
        assert ok.headers.get("access-control-allow-origin") == "https://haic.example"
        bad = c.get("/api/meta", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in bad.headers
    finally:
        monkeypatch.delenv("ALLOWED_ORIGINS")
        importlib.reload(webapp)


# ---------------------------------------------------------------- /about

def test_about_page(client):
    r = client.get("/about")
    assert r.status_code == 200
    t = r.text
    meta = tools.load_data()["metadata"]
    assert meta["dataset"] in t and meta["boundary_vintage"] in t
    assert load_programs()["metadata"]["date_verified"] in t
    assert "nonpartisan" in t and "grounding guarantee" in t.lower() and "Limitations" in t
    assert "Reapportionment Commission" in t and "cartographic boundary" in t
    assert "Hawaiʻi" in t and "Hawai'i" not in t
    assert FAKE_KEY not in t
