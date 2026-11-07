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
    return client.post("/chat", json={"messages": [{"role": "user", "content": "q"}]})


# ---------------------------------------------------------------- health

def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_readyz_reports_data_and_config(client):
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
    assert j["config"]["model"] and j["config"]["rate_limit_per_minute"] >= 1
    assert FAKE_KEY not in r.text and "sk-ant" not in r.text


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


def test_unhandled_errors_never_show_a_stack_trace(client, monkeypatch):
    def broken():
        raise KeyError("internal detail /secret/path")
    monkeypatch.setattr(tools, "load_data", broken)
    r = client.get("/api/meta")
    assert r.status_code == 500
    assert r.json() == {"error": "Something went wrong on our side. Please try again.", "code": "internal_error"}
    assert "Traceback" not in r.text and "secret" not in r.text


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
