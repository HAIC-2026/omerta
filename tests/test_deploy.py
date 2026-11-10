"""Deploy configuration: Procfile, Python version, runtime requirements, env-var docs and
proxy handling. Offline."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import app as webapp

ROOT = Path(__file__).resolve().parent.parent


def test_procfile_binds_port_on_all_interfaces_with_one_worker():
    lines = [ln for ln in (ROOT / "Procfile").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("web: uvicorn web.app:app ")
    cmd = lines[0]
    assert "--host 0.0.0.0" in cmd and "--port $PORT" in cmd
    assert "--workers 1" in cmd          # rate limits are in-process
    assert "--reload" not in cmd and "--proxy-headers" not in cmd


def test_python_version_matches_the_tested_interpreter():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"3\.\d+", pinned)
    assert pinned == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert not (ROOT / "runtime.txt").exists()   # deprecated on Heroku


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


@pytest.mark.parametrize("xff,want", [
    ("203.0.113.9", "203.0.113.9"),
    ("1.2.3.4, 203.0.113.9", "203.0.113.9"),     # client-forged first entry is ignored
    ("1.2.3.4,5.6.7.8, 203.0.113.9", "203.0.113.9"),
    (None, "10.0.0.1"),
    ("", "10.0.0.1"),
])
def test_client_ip_behind_proxy_uses_last_forwarded_entry(monkeypatch, xff, want):
    monkeypatch.setattr(webapp, "TRUST_PROXY", True)
    assert webapp.client_ip(_Req(xff)) == want


def test_client_ip_ignores_forwarded_header_without_proxy(monkeypatch):
    monkeypatch.setattr(webapp, "TRUST_PROXY", False)
    assert webapp.client_ip(_Req("1.2.3.4")) == "10.0.0.1"


def test_forged_forwarded_for_cannot_dodge_the_limit(monkeypatch):
    """Behind the proxy, rotating a fake first X-Forwarded-For entry still hits one bucket."""
    from tests.test_web import FakeAgent
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.setattr(webapp, "TRUST_PROXY", True)
    webapp.app.state.limiter = webapp.RateLimiter([("ip", 2, 60)])
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: FakeAgent("ok")
    try:
        c = TestClient(webapp.app)
        codes = [c.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]},
                        headers={"x-forwarded-for": f"9.9.9.{i}, 203.0.113.9"}).status_code
                 for i in range(3)]
    finally:
        webapp.app.dependency_overrides.clear()
        webapp.app.state.limiter = webapp.default_limiter()
    assert codes == [200, 200, 429]


def test_readyz_reports_roster_and_proxy_mode():
    c = TestClient(webapp.app).get("/readyz").json()["checks"]
    assert c["legislators_serving"] == 76
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["legislators_fetched"])
    assert c["trust_proxy"] in (True, False)
