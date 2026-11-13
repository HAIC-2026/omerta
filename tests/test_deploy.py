"""Deploy configuration: render.yaml, Python version, runtime requirements, env-var docs and
proxy handling. Offline."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import app as webapp

ROOT = Path(__file__).resolve().parent.parent


def _render_service() -> dict:
    import yaml
    spec = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    assert len(spec["services"]) == 1     # one instance: rate limits and sessions are in-process
    return spec["services"][0]


def test_render_start_command_binds_port_on_all_interfaces_with_one_worker():
    svc = _render_service()
    assert svc["type"] == "web" and svc["runtime"] == "python"
    cmd = svc["startCommand"]
    assert cmd.startswith("uvicorn web.app:app ")
    assert "--host 0.0.0.0" in cmd and "--port $PORT" in cmd
    assert "--workers 1" in cmd          # rate limits are in-process
    assert "--reload" not in cmd and "--proxy-headers" not in cmd
    assert svc["buildCommand"] == "pip install -r requirements.txt"


def test_render_service_settings_match_the_live_setup():
    svc = _render_service()
    assert svc["plan"] == "starter"       # always on; free instances spin down
    assert svc["healthCheckPath"] == "/healthz"
    assert svc["branch"] == "main" and svc["autoDeployTrigger"] == "commit"
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["TRUST_PROXY"]["value"] == "2"     # Cloudflare + Render's proxy
    # Every configured name is one the app reads and the README documents.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for k in env:
        assert f"| `{k}` |" in readme


def test_render_yaml_never_holds_the_api_key():
    svc = _render_service()
    key = next(e for e in svc["envVars"] if e["key"] == "ANTHROPIC_API_KEY")
    assert key == {"key": "ANTHROPIC_API_KEY", "sync": False}
    assert "sk-ant" not in (ROOT / "render.yaml").read_text(encoding="utf-8")


def test_heroku_files_are_gone():
    for f in ("Procfile", ".slugignore", "runtime.txt", "app.json"):
        assert not (ROOT / f).exists(), f


def test_python_version_matches_the_tested_interpreter():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"3\.\d+", pinned)
    assert pinned == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert not (ROOT / "runtime.txt").exists()   # Render reads .python-version


def test_runtime_requirements_are_runtime_only():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = {re.split(r"[<>=\[ ]", ln.strip())[0].lower()
             for ln in req.splitlines() if ln.strip() and not ln.startswith("#")}
    assert {"fastapi", "uvicorn", "anthropic", "python-dotenv"} <= names
    assert not names & {"pytest", "playwright", "axe-playwright-python", "pypdf", "pyshp", "httpx"}
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in dev and "pytest" in dev


def test_every_env_var_the_app_reads_is_documented():
    code = "".join((ROOT / f).read_text(encoding="utf-8") for f in ("web/app.py", "agent/chat.py"))
    names = set(re.findall(r'(?:getenv|_env_int)\(\s*"([A-Z_]+)"', code)) | {"PORT"}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme[readme.index("## Configuration"):readme.index("## Operations")]
    for n in sorted(names):
        assert f"| `{n}` |" in table, f"{n} not documented in the README configuration table"
    assert "ANTHROPIC_API_KEY" in names and "ALLOWED_ORIGINS" in names


def test_secrets_are_not_committed():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in gi


class _Req:
    def __init__(self, xff, host="10.0.0.1"):
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}
        self.client = type("C", (), {"host": host})()


# Render (measured 2026-11-13): "<client>, <proxy>". Forged entries are added in front.
CLIENT, EDGE = "203.0.113.9", "172.70.1.1"


@pytest.mark.parametrize("xff,want", [
    (f"{CLIENT}, {EDGE}", CLIENT),                        # plain request on Render
    (f"1.2.3.4, {CLIENT}, {EDGE}", CLIENT),               # client-forged entry is ignored
    (f"1.2.3.4,5.6.7.8, {CLIENT}, {EDGE}", CLIENT),
    (f"{CLIENT},{EDGE}", CLIENT),                         # no spaces
    (EDGE, webapp.UNVERIFIED_CLIENT),                     # too few hops: shared bucket
    (None, webapp.UNVERIFIED_CLIENT),                     # never the (forgeable) peer address
    ("", webapp.UNVERIFIED_CLIENT),
    (f", {EDGE}", webapp.UNVERIFIED_CLIENT),              # empty client slot
])
def test_client_ip_on_render_is_second_from_the_right(monkeypatch, xff, want):
    monkeypatch.setattr(webapp, "TRUST_PROXY", 2)
    assert webapp.client_ip(_Req(xff)) == want


@pytest.mark.parametrize("xff,want", [
    ("203.0.113.9", "203.0.113.9"),
    ("1.2.3.4, 203.0.113.9", "203.0.113.9"),
    (None, webapp.UNVERIFIED_CLIENT),
])
def test_client_ip_behind_one_proxy_uses_last_entry(monkeypatch, xff, want):
    monkeypatch.setattr(webapp, "TRUST_PROXY", 1)
    assert webapp.client_ip(_Req(xff)) == want


def test_client_ip_ignores_forwarded_header_without_proxy(monkeypatch):
    monkeypatch.setattr(webapp, "TRUST_PROXY", 0)
    assert webapp.client_ip(_Req("1.2.3.4")) == "10.0.0.1"


@pytest.mark.parametrize("value,want", [("0", 0), ("1", 1), ("2", 2), ("-1", 0), ("yes", 0), ("", 0)])
def test_trust_proxy_parses_as_hop_count(monkeypatch, value, want):
    import importlib
    monkeypatch.setenv("TRUST_PROXY", value)
    try:
        assert importlib.reload(webapp).TRUST_PROXY == want
    finally:
        monkeypatch.delenv("TRUST_PROXY")
        importlib.reload(webapp)


def _post_from(forwarded: list[str]) -> list[int]:
    from tests.test_web import FakeAgent
    webapp.app.state.limiter = webapp.RateLimiter([("ip", 2, 60)])
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: FakeAgent("ok")
    try:
        c = TestClient(webapp.app)
        return [c.post("/chat", json={"question": "hi"}, headers={"x-forwarded-for": x}).status_code
                for x in forwarded]
    finally:
        webapp.app.dependency_overrides.clear()
        webapp.app.state.limiter = webapp.default_limiter()


def test_forged_forwarded_for_cannot_dodge_the_limit(monkeypatch):
    """On Render, rotating fake leading X-Forwarded-For entries still hits one bucket."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.setattr(webapp, "TRUST_PROXY", 2)
    codes = _post_from([f"9.9.9.{i}, {CLIENT}, {EDGE}" for i in range(3)])
    assert codes == [200, 200, 429]


def test_visitors_behind_the_same_proxy_get_separate_limits(monkeypatch):
    """The bug "last entry" had on Render: every visitor keyed on the proxy's address."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.setattr(webapp, "TRUST_PROXY", 2)
    codes = _post_from([f"203.0.113.{i}, {EDGE}" for i in range(4)])
    assert codes == [200, 200, 200, 200]


def test_readyz_reports_roster_and_proxy_mode(monkeypatch):
    c = TestClient(webapp.app).get("/readyz").json()["checks"]
    assert "trust_proxy" not in c                      # operating config is only under ?deep=1
    monkeypatch.setattr(webapp, "_check_model_api", lambda: "ok")
    deep = TestClient(webapp.app).get("/readyz", params={"deep": 1}).json()["checks"]
    assert c["legislators_serving"] == 76
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["legislators_fetched"])
    assert isinstance(deep["trust_proxy"], int) and deep["trust_proxy"] >= 0


@pytest.mark.parametrize("fname", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_is_pinned_exactly(fname):
    lines = [ln.split("#")[0].strip() for ln in (ROOT / fname).read_text(encoding="utf-8").splitlines()]
    reqs = [ln for ln in lines if ln and not ln.startswith("-r ")]
    assert reqs
    for ln in reqs:
        assert re.match(r"^[A-Za-z0-9_.\-]+(\[[a-z,]+\])?==\d[\w.]*(\s*;.+)?$", ln), ln


def test_pins_match_the_installed_test_environment():
    """The versions pinned are the ones this test run is using (where installed here)."""
    from importlib import metadata
    for fname in ("requirements.txt", "requirements-dev.txt"):
        for ln in (ROOT / fname).read_text(encoding="utf-8").splitlines():
            ln = ln.split("#")[0].strip()
            if not ln or ln.startswith("-r ") or ";" in ln:
                continue
            name, version = ln.split("==")
            name = name.split("[")[0]
            assert metadata.version(name) == version, f"{name}: pinned {version}, installed {metadata.version(name)}"
