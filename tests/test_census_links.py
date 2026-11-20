"""Offline tests for the "Data lookups" source links: the data.census.gov table page is the
primary link, the raw Census API query the secondary one. No network calls are made."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import tools
from agent.chat import ToolCall, TurnResult
from agent.program_tools import district_program_context
from web import app as webapp
from web.census_links import (CENSUS_TABLE_LINKS, census_table_label, census_table_links,
                              census_table_url, strip_api_key)

TABLES = {"B01003", "B01002", "B19013", "B17001", "B25070", "B22003", "C16002", "B21001", "B23025"}
HOUSE = "https://data.census.gov/table/ACSDT5Y2024.{}?g=040XX00US15$6200000"
SENATE = "https://data.census.gov/table/ACSDT5Y2024.{}?g=040XX00US15$6100000"


def _by_table(links):
    return {(link["table_id"], link["chamber"]): link for link in links}


def test_template_and_geo_codes_live_in_one_constant():
    assert CENSUS_TABLE_LINKS["url_template"] == "https://data.census.gov/table/ACSDT5Y2024.{table}?g={geo}"
    assert CENSUS_TABLE_LINKS["chamber_geo"] == {"house": "040XX00US15$6200000",
                                                 "senate": "040XX00US15$6100000"}
    # The year in the template is the year of the data the app serves.
    meta = tools.load_data()["metadata"]
    assert meta["acs_year"] == CENSUS_TABLE_LINKS["acs_year"]
    assert f"ACSDT5Y{meta['acs_year']}." in CENSUS_TABLE_LINKS["url_template"]


def test_the_data_uses_exactly_the_tables_the_links_were_verified_for():
    used = {m["table_id"] for r in tools.load_data()["districts"] for m in r["metrics"].values()}
    assert used == TABLES


# ------------------------------------------------------------ House lookups

def test_house_profile_lookup_links_every_table_for_that_district():
    links = census_table_links(tools.get_district_profile("house", 46))
    by = _by_table(links)
    assert {t for t, _ in by} == TABLES and {c for _, c in by} == {"house"}
    rent = by[("B25070", "house")]
    assert rent["url"] == "https://data.census.gov/table/ACSDT5Y2024.B25070?g=040XX00US15$6200000"
    assert rent["label"] == "Census table B25070 — all House districts (see District 46)"
    for link in links:
        assert link["url"] == HOUSE.format(link["table_id"])
        assert link["districts"] == [46]


def test_house_comparison_lookup_links_one_table_and_names_the_districts():
    links = census_table_links(tools.compare_districts("house", [3, 17], "median_household_income"))
    assert links == [{
        "table_id": "B19013", "chamber": "house", "districts": [3, 17],
        "url": "https://data.census.gov/table/ACSDT5Y2024.B19013?g=040XX00US15$6200000",
        "label": "Census table B19013 — all House districts (see Districts 3, 17)",
    }]


# ----------------------------------------------------------- Senate lookups

def test_senate_profile_lookup_links_every_table_for_that_district():
    links = census_table_links(tools.get_district_profile("senate", 4))
    by = _by_table(links)
    assert {t for t, _ in by} == TABLES and {c for _, c in by} == {"senate"}
    pop = by[("B01003", "senate")]
    assert pop["url"] == "https://data.census.gov/table/ACSDT5Y2024.B01003?g=040XX00US15$6100000"
    assert pop["label"] == "Census table B01003 — all Senate districts (see District 4)"
    assert all(link["url"] == SENATE.format(link["table_id"]) for link in links)


def test_senate_ranking_lookup_links_one_table():
    out = tools.rank_districts("senate", "poverty_rate", 3, "desc")
    links = census_table_links(out)
    assert len(links) == 1
    link = links[0]
    assert link["url"] == "https://data.census.gov/table/ACSDT5Y2024.B17001?g=040XX00US15$6100000"
    ranked = [r["district"] for r in out["results"]]
    assert link["districts"] == ranked
    assert link["label"] == ("Census table B17001 — all Senate districts (see Districts "
                             + ", ".join(map(str, ranked)) + ")")


# ------------------------------------------------------------- other shapes

def test_both_chamber_lookup_links_the_table_once_per_chamber():
    links = census_table_links(tools.statewide_context("median_age"))
    assert [(link["chamber"], link["url"]) for link in links] == [
        ("house", HOUSE.format("B01002")), ("senate", SENATE.format("B01002"))]


def test_program_context_lookup_links_its_tables():
    out = district_program_context("senate", 4)
    links = census_table_links(out)
    assert links and all(link["url"] == SENATE.format(link["table_id"]) for link in links)
    assert all(link["districts"] == [4] for link in links)


def test_long_rankings_do_not_list_every_district_in_the_label():
    link = census_table_links(tools.rank_districts("house", "poverty_rate", 51, "desc"))[0]
    assert link["label"] == "Census table B17001 — all House districts"
    assert census_table_label("B17001", "house") == "Census table B17001 — all House districts"


def test_lookups_without_census_values_get_no_table_links():
    assert census_table_links(tools.get_district_profile("house", 999)) == []       # error result
    assert census_table_links({"table_id": "B01003"}) == []                         # no chamber
    assert census_table_links({"chamber": "house", "table_id": "B01003",
                               "source_url": "https://example.com/"}) == []
    with pytest.raises(ValueError):
        census_table_url("B01003?x=1", "house")


# ------------------------------------------------- the API link, and /chat

def test_api_query_links_carry_no_key():
    for chamber, district in (("house", 46), ("senate", 4)):
        urls = webapp._source_urls(tools.get_district_profile(chamber, district))
        api = [u for u in urls if u.startswith("https://api.census.gov/")]
        assert api and all("key=" not in u.lower() for u in api)
    assert strip_api_key("https://api.census.gov/data/2024/acs/acs5?get=NAME&key=SECRET&in=state:15") \
        == "https://api.census.gov/data/2024/acs/acs5?get=NAME&in=state:15"
    assert strip_api_key("https://api.census.gov/data/2024/acs/acs5?get=NAME&key=SECRET") \
        == "https://api.census.gov/data/2024/acs/acs5?get=NAME"
    assert webapp._source_urls({"source_url": "https://api.census.gov/data?get=NAME&KEY=SECRET"}) \
        == ["https://api.census.gov/data?get=NAME"]


class _Agent:
    def __init__(self, answer, calls):
        self.answer, self.calls, self.messages = answer, calls, []

    def ask(self, question):
        return TurnResult(text=self.answer, tool_calls=self.calls, stop_reason="end_turn")


def test_chat_response_carries_table_links_and_leaves_the_output_untouched(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-FAKE")
    webapp.app.state.limiter = webapp.default_limiter()
    out = tools.get_district_profile("house", 46)
    call = ToolCall("get_district_profile", {"chamber": "house", "district": 46}, out, False)
    pop = out["metrics"]["total_population"]["value_display"]
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: _Agent(
        f"House District 46 has {pop} people.", [call])
    try:
        body = TestClient(webapp.app).post("/chat", json={"question": "House 46?"}).json()
    finally:
        webapp.app.dependency_overrides.clear()
    tr = body["tool_results"][0]
    assert tr["output"] == out                       # grounding data unchanged
    assert tr["census_tables"] == census_table_links(out)
    assert HOUSE.format("B25070") in [c["url"] for c in tr["census_tables"]]
    assert any(u.startswith("https://api.census.gov/") for u in tr["source_urls"])
    assert all("key=" not in u.lower() for u in tr["source_urls"])


def test_sources_panel_shows_the_table_link_first_and_the_api_link_second():
    js = (webapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "t.census_tables" in js and '"API query"' in js and "Census API query" not in js
    assert js.index("t.census_tables") < js.index('"API query"')
