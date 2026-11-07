"""Automated accessibility checks for the web UI: axe-core (via Playwright) plus keyboard,
focus, zoom and phone-width checks. Uses a mocked agent, so no API calls are made.

Requires: pip install playwright axe-playwright-python && python -m playwright install chromium
Run:      python -m pytest tests/test_a11y.py -s
Set A11Y_REPORT=<path> to write the axe results as JSON.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
axe_mod = pytest.importorskip("axe_playwright_python.sync_playwright")

import anthropic  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402

from agent import tools  # noqa: E402
from agent.chat import ToolCall, TurnResult  # noqa: E402
from web import app as webapp  # noqa: E402

pytestmark = pytest.mark.a11y

WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
RESULTS: list[dict] = []


# ---------------------------------------------------------------- mocked agent

def _flagged_district() -> int:
    data = tools.load_data()
    return next(r["district"] for r in data["districts"] if r["chamber"] == "house"
                and r["metrics"]["limited_english_household_rate"]["low_reliability"])


class MockAgent:
    def __init__(self):
        self.messages = []

    def ask(self, q: str) -> TurnResult:
        if "error" in q.lower():
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
        if "slow" in q.lower():
            time.sleep(3)
        d = _flagged_district()
        out = tools.get_district_profile("house", d)
        m = out["metrics"]
        lep, inc, pop = (m["limited_english_household_rate"], m["median_household_income"],
                         m["total_population"])
        text = (
            f"Here is a profile of **House District {d}**.\n\n"
            "| Metric | Value | MOE |\n|---|---|---|\n"
            f"| Total population | {pop['value']:,.0f} | ± {pop['moe']:,.0f} |\n"
            f"| Median household income | ${inc['value']:,.0f} | ± ${inc['moe']:,.0f} |\n"
            f"| Limited-English households | {lep['value']}% | ± {lep['moe']} |\n\n"
            f"- The limited-English share ({lep['value']}%) is a low-reliability estimate.\n"
            "- Census place names may differ from Hawaiʻi spellings.\n\n"
            f"Source: {out['citation']}"
        )
        return TurnResult(text=text, stop_reason="end_turn",
                          tool_calls=[ToolCall("get_district_profile",
                                               {"chamber": "house", "district": d}, out, False)])


# ---------------------------------------------------------------- server

@pytest.fixture(scope="module")
def base_url():
    webapp.app.dependency_overrides[webapp.get_agent] = MockAgent
    webapp.app.state.limiter = webapp.RateLimiter([("ip", 1000, 60)])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)
    webapp.app.dependency_overrides.clear()
    webapp.app.state.limiter = webapp.default_limiter()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        b = None
        # Bundled Chromium first; fall back to installed Edge or Chrome (both Chromium).
        for kwargs in ({}, {"channel": "chromium"}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if b is None:
            pytest.skip("No Chromium-based browser available "
                        "(run: python -m playwright install chromium)")
        print(f"\nBrowser: {b.browser_type.name} {b.version}")
        yield b
        b.close()


@pytest.fixture(scope="module", autouse=True)
def report():
    yield
    print("\n\n=== axe-core results (WCAG 2.0/2.1 A + AA, plus best practices) ===")
    for r in RESULTS:
        print(f"  {r['state']:<28} WCAG violations: {len(r['wcag'])}  best-practice: {len(r['best_practice'])}")
        for v in r["wcag"] + r["best_practice"]:
            print(f"      - [{v['impact']}] {v['id']}: {v['help']} ({len(v['nodes'])} nodes)")
    path = os.getenv("A11Y_REPORT")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"axe_version": RESULTS[0]["axe_version"] if RESULTS else None,
                                          "tags": WCAG_TAGS + ["best-practice"], "states": RESULTS},
                                         indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  report: {path}")


def run_axe(page, state: str) -> list[dict]:
    axe = axe_mod.Axe()
    res = axe.run(page, options={"runOnly": {"type": "tag", "values": WCAG_TAGS + ["best-practice"]},
                                 "resultTypes": ["violations"]}).response
    slim = lambda v: {"id": v["id"], "impact": v["impact"], "help": v["help"], "tags": v["tags"],
                      "nodes": [n["target"] for n in v["nodes"]]}
    wcag = [slim(v) for v in res["violations"] if set(v["tags"]) & set(WCAG_TAGS)]
    bp = [slim(v) for v in res["violations"] if not set(v["tags"]) & set(WCAG_TAGS)]
    RESULTS.append({"state": state, "axe_version": res.get("testEngine", {}).get("version"),
                    "wcag": wcag, "best_practice": bp})
    return wcag


def new_page(browser, base_url, **ctx):
    context = browser.new_context(**ctx)
    page = context.new_page()
    page.goto(base_url + "/")
    page.wait_for_function("() => document.querySelectorAll('#pick-district option').length > 1")
    return page


def ask(page, q: str):
    page.fill("#question", q)
    page.press("#question", "Enter")


def no_horizontal_scroll(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


# ---------------------------------------------------------------- axe checks per UI state

def test_axe_initial(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    assert run_axe(page, "initial (desktop)") == []


def test_axe_answer_with_sources_open(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    ask(page, "Profile a district")
    page.wait_for_selector("article.msg-agent")
    page.click("details.sources summary")
    assert page.locator("mark.lowrel").count() >= 1
    assert page.locator(".lowrel-panel").count() == 1
    assert run_axe(page, "answer + sources open") == []


def test_axe_loading_state(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    ask(page, "slow question")
    page.wait_for_selector("#loading")
    assert page.get_attribute("#log", "aria-busy") == "true"
    page.wait_for_function("() => document.getElementById('status').textContent.includes('Looking up')")
    assert run_axe(page, "loading") == []
    page.wait_for_selector("article.msg-agent", timeout=10000)


def test_axe_error_state(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    ask(page, "trigger an error")
    page.wait_for_selector("#error-area .error-box")
    assert page.get_attribute("#error-area", "role") == "alert"
    assert page.locator("#error-area button", has_text="Try again").count() == 1
    assert run_axe(page, "error") == []


def test_axe_empty_submit_error(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    page.press("#question", "Enter")
    assert page.is_visible("#form-error")
    assert page.get_attribute("#question", "aria-invalid") == "true"
    assert run_axe(page, "empty-question validation") == []


def test_phone_width(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 375, "height": 740}, is_mobile=True,
                    has_touch=True, device_scale_factor=2)
    assert no_horizontal_scroll(page)
    ask(page, "Profile a district")
    page.wait_for_selector("article.msg-agent")
    assert no_horizontal_scroll(page)  # wide tables scroll inside their own region
    assert run_axe(page, "phone width 375px + answer") == []


def test_zoom_200_percent(browser, base_url):
    # 200% zoom on a 1280px-wide window = a 640px CSS viewport at 2x device pixels.
    page = new_page(browser, base_url, viewport={"width": 640, "height": 450}, device_scale_factor=2)
    assert no_horizontal_scroll(page)
    ask(page, "Profile a district")
    page.wait_for_selector("article.msg-agent")
    assert no_horizontal_scroll(page)
    assert page.is_visible("#send") and page.is_visible("#question")
    assert run_axe(page, "200% zoom + answer") == []


def test_axe_detects_planted_violations(browser, base_url):
    """Control: prove axe is actually running under our CSP by planting two violations."""
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    page.evaluate("""() => {
        const i = document.createElement('input'); i.id = 'planted'; document.body.appendChild(i);
        const p = document.createElement('p'); p.textContent = 'faint text';
        p.style.color = '#bbbbbb'; p.style.background = '#ffffff'; document.body.appendChild(p);
    }""")
    res = axe_mod.Axe().run(page, options={"runOnly": {"type": "tag", "values": WCAG_TAGS}}).response
    ids = {v["id"] for v in res["violations"]}
    assert "label" in ids and "color-contrast" in ids, ids


# ---------------------------------------------------------------- keyboard and focus

def test_keyboard_navigation_and_focus_visible(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    expected = {"skip-link", "chip", "pick-chamber", "pick-district", "pick-topic",
                "picker-submit", "new-chat", "question", "send"}
    seen, no_ring = set(), []
    for _ in range(25):
        page.keyboard.press("Tab")
        info = page.evaluate("""() => {
            const e = document.activeElement; const cs = getComputedStyle(e);
            return {id: e.id, cls: e.className, tag: e.tagName, type: e.type || '',
                    outline: cs.outlineStyle, width: parseFloat(cs.outlineWidth) || 0};
        }""")
        key = (info["id"] or ("skip-link" if "skip-link" in info["cls"] else "")
               or ("chip" if "chip" in info["cls"] else "")
               or ("picker-submit" if info["tag"] == "BUTTON" and info["type"] == "submit"
                   and not info["id"] else info["tag"]))
        if info["tag"] == "BODY":  # Tab wrapped past the last control to the browser UI
            continue
        seen.add(key)
        if info["outline"] == "none" or info["width"] < 2:
            no_ring.append(key)
    assert expected <= seen, f"not reachable by Tab: {expected - seen}"
    assert not no_ring, f"no visible focus ring on: {no_ring}"


def test_keyboard_only_flow(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    # Skip link moves focus to the question box.
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "question"
    # Picker fills in a question without sending it.
    page.focus("#pick-chamber")
    page.select_option("#pick-chamber", "senate")
    page.focus("#pick-topic")
    page.select_option("#pick-topic", "poverty_rate")
    page.locator("#picker button[type=submit]").press("Enter")
    assert "State Senate District" in page.input_value("#question")
    assert "poverty rate" in page.input_value("#question")
    assert page.evaluate("document.activeElement.id") == "question"
    # Send with Enter; open Sources with the keyboard.
    page.keyboard.press("Enter")
    page.wait_for_selector("article.msg-agent")
    page.focus("details.sources summary")
    page.keyboard.press("Enter")
    assert page.evaluate("document.querySelector('details.sources').open") is True
    # The scrollable table region is reachable by keyboard.
    assert page.get_attribute(".table-wrap", "tabindex") == "0"


def test_live_region_and_labels(browser, base_url):
    page = new_page(browser, base_url, viewport={"width": 1280, "height": 900})
    assert page.get_attribute("#log", "role") == "log"
    assert page.get_attribute("#log", "aria-live") == "polite"
    assert page.get_attribute("#status", "role") == "status"
    for sel in ("#question", "#pick-chamber", "#pick-district", "#pick-topic"):
        el_id = sel[1:]
        assert page.locator(f"label[for={el_id}]").count() == 1, f"no label for {sel}"
    # ʻokina (U+02BB) used, never an apostrophe stand-in.
    body = page.inner_text("body")
    assert "Hawaiʻi" in body and "Oʻahu" in body and "Lānaʻi" in body
    assert "Hawai'i" not in body and "Oahu" not in body
