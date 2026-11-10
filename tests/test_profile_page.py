"""Offline tests for the one-page district profile export (no model involved)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from agent import tools
from web import app as webapp


class _Struct(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture
def client():
    def boom():
        raise AssertionError("the profile page must not use the agent/model")
    webapp.app.dependency_overrides[webapp.get_agent] = boom
    yield TestClient(webapp.app)
    webapp.app.dependency_overrides.clear()


def get(client, chamber, d, **params):
    return client.get(f"/district/{chamber}/{d}/profile", params=params)


def test_profile_page_has_every_tool_value(client):
    r = get(client, "house", 46)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    p = tools.get_district_profile("house", 46)
    for k, m in p["metrics"].items():
        assert m["value_display"] in r.text, k
        assert m["moe_display"] in r.text, k
        assert m["table_id"] in r.text
    ctx = p["district_context"]
    assert ctx["reapportionment_population"]["permanent_resident_population_display"] in r.text
    assert ctx["acs_vs_reapportionment"]["display"] in r.text
    for a in ctx["areas"]["areas_as_written"]:
        assert a in r.text


def test_profile_page_only_tool_numbers(client):
    """Every number on the page comes from tool data, the dataset/vintage, or the generated date."""
    from decimal import Decimal
    from agent.grounding import collect_tool_numbers, extract_numbers, is_supported
    r = get(client, "senate", 8)
    s = _Struct(); s.feed(r.text)
    text = " ".join(s.text)
    src: set[Decimal] = set()
    collect_tool_numbers(tools.get_district_profile("senate", 8), src)
    collect_tool_numbers(tools.load_data()["metadata"], src)
    gen = re.search(r"Generated ([A-Z][a-z]+ \d{2}, \d{4} \d{2}:\d{2}) UTC", text).group(1)
    src.update(Decimal(n) for n in extract_numbers(gen))
    bad = [n for n in extract_numbers(text) if not is_supported(n, src)]
    assert not bad, bad


def test_caveats_and_flags(client):
    p = tools.get_district_profile("house", 46)
    flagged = [k for k, m in p["metrics"].items() if m["low_reliability"]]
    r = get(client, "house", 46)
    assert r.text.count('class="flag"') == len(flagged)
    if flagged:
        assert "Reliability caveats" in r.text and "Low reliability." in r.text
        from html import escape
        assert escape(tools.load_data()["metadata"]["low_reliability_rule"]) in r.text


def test_semantic_structure(client):
    r = get(client, "house", 1)
    s = _Struct(); s.feed(r.text)
    for tag in ("main", "header", "h1", "h2", "table", "caption", "thead", "th"):
        assert tag in s.tags, tag
    assert '<html lang="en">' in r.text
    assert 'scope="col"' in r.text and 'scope="row"' in r.text
    meta = tools.load_data()["metadata"]
    assert "Generated " in r.text and meta["dataset"] in r.text and meta["boundary_vintage"] in r.text
    assert "Hawaiʻi" in r.text


def test_citations_and_sources(client):
    r = get(client, "house", 46)
    assert f"U.S. Census Bureau, {tools.load_data()['metadata']['dataset']}, Tables" in r.text
    assert "Appendix D" in r.text and "PDF p." in r.text
    assert 'href="https://api.census.gov/' in r.text and 'href="https://elections.hawaii.gov/' in r.text
    assert "key=" not in r.text


def test_download_header(client):
    r = get(client, "senate", 12, download=1)
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="hawaii-senate-district-12-profile.html"'
    assert "content-disposition" not in get(client, "senate", 12).headers


@pytest.mark.parametrize("chamber,d", [("house", 52), ("senate", 26), ("assembly", 1), ("house", "abc"), ("house", 0)])
def test_invalid_district_404(client, chamber, d):
    r = get(client, chamber, d, download=1)
    assert r.status_code == 404
    assert "District not found" in r.text
    assert "content-disposition" not in r.headers


def test_model_text_cannot_reach_the_page(client):
    """The page takes no user/model text: only chamber and district from the URL."""
    r = client.get("/district/house/46/profile", params={"answer": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in r.text


def test_every_district_renders(client):
    for chamber in tools.CHAMBERS:
        for d in tools._districts_in(chamber):
            assert get(client, chamber, d).status_code == 200
