"""Legislator roster: data provenance, name matching, the profile field, grounding and the UI
chip. Offline; reads only data/legislators.json."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from agent import legislator_tools as lt
from agent.chat import SYSTEM_PROMPT, TOOL_FUNCS, build_tool_schemas
from agent.grounding import unsourced_legislators
from agent.tools import get_district_profile
from tests.guardrail_checks import example_legislator

ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "data" / "legislators.json").read_text(encoding="utf-8"))
META, ROSTER = RAW["metadata"], RAW["legislators"]
SERVING = [m for m in ROSTER if m["serving"]]


# --- provenance ----------------------------------------------------------------

def test_every_record_is_from_capitol_hawaii_gov():
    for url in META["source_urls"]:
        assert urlparse(url).netloc == "www.capitol.hawaii.gov"
    for m in ROSTER:
        assert urlparse(m["source_url"]).netloc == "www.capitol.hawaii.gov"
        assert re.fullmatch(r"https://www\.capitol\.hawaii\.gov/legislature/memberpage\.aspx\?member=\d+&year=\d{4}",
                            m["profile_url"])
        assert m["date_fetched"] == META["date_fetched"]
        assert m["chamber"] in ("house", "senate")


def test_no_party_affiliation():
    assert "party affiliation" in META["fields_not_recorded"]
    for m in ROSTER:
        assert not any("party" in k for k in m)
        assert not re.search(r"\((?:D|R|I|G|L|NP)\)", m["name_as_written"])


def test_one_serving_member_per_district():
    for chamber, n in (("house", 51), ("senate", 25)):
        for d in range(1, n + 1):
            assert len([m for m in SERVING if m["chamber"] == chamber and m["district"] == d]) == 1


def test_former_members_keep_the_sites_status_text():
    for m in ROSTER:
        if not m["serving"]:
            assert any(s.startswith("Term of Office Ended:") for s in m["status_as_written"])


def test_election_status_is_recorded_as_the_site_states_it():
    for chamber, word in (("house", "Representatives"), ("senate", "Senators")):
        assert META["membership_as_stated_by_site"][chamber].startswith(f"(Showing {word} for")
    assert META["election_status"] in ("pre-election", "post-election", "unstated")
    if META["election_status"] == "pre-election":
        assert all("2026 regular session" in s for s in META["membership_as_stated_by_site"].values())
    assert "November 3, 2026" in META["election_note"] or META["election_status"] == "unstated"


# --- name matching --------------------------------------------------------------

def test_normalize_ignores_case_okina_and_kahako():
    assert lt.normalize_name("Kāneʻohe") == lt.normalize_name("KANEOHE") == "kaneohe"
    assert lt.normalize_name("Hawai‘i") == lt.normalize_name("Hawai'i") == "hawaii"


@pytest.mark.parametrize("m", SERVING, ids=lambda m: m["name_as_written"])
def test_every_serving_member_found_by_full_name(m):
    for q in (m["name_first_last"], m["name_as_written"], m["name_first_last"].upper()):
        r = lt.find_district_by_legislator(q)
        assert r.get("match", {}).get("name_as_written") == m["name_as_written"], q
        assert (r["match"]["chamber"], r["match"]["district"]) == (m["chamber"], m["district"])
        assert r["citation"].startswith("Hawaiʻi State Legislature")


def test_okina_and_kahako_in_query_are_ignored():
    m = next(x for x in SERVING if "a" in x["name_first_last"].split()[-1])
    last = m["name_first_last"].split()[-1]
    marked = last.replace("a", "ā", 1) + "ʻ"
    first = m["name_first_last"].split()[0]
    r = lt.find_district_by_legislator(f"{first} {marked}")
    assert r.get("match", {}).get("name_as_written") == m["name_as_written"]


def test_shared_name_is_ambiguous_with_candidates():
    surnames = {}
    for m in ROSTER:
        surnames.setdefault(lt.normalize_name(m["name_as_written"].split(",")[0]), []).append(m)
    shared = next(k for k, v in surnames.items() if len(v) > 1)
    r = lt.find_district_by_legislator(shared)
    assert r["ambiguous"] is True and "match" not in r
    assert len(r["candidates"]) >= 2
    assert all(c["chamber"] and c["district"] for c in r["candidates"])


@pytest.mark.parametrize("q", ["Chan", "Stan Chang", "Nobody Here", "Changg"])
def test_no_fuzzy_or_partial_matching(q):
    r = lt.find_district_by_legislator(q)
    assert "error" in r and "match" not in r and "candidates" not in r


@pytest.mark.parametrize("q", ["", "   ", "--", None, 5])
def test_invalid_query(q):
    assert "error" in lt.find_district_by_legislator(q)


def test_former_member_points_to_current_member():
    former = [m for m in ROSTER if not m["serving"]]
    for m in former:
        r = lt.find_district_by_legislator(m["name_as_written"])
        assert r["match"]["serving"] is False
        cur = r["current_member_of_district"]
        assert len(cur) == 1 and cur[0]["district"] == m["district"] and cur[0]["serving"]


# --- profile, chat wiring ---------------------------------------------------------

@pytest.mark.parametrize("chamber,n", [("house", 51), ("senate", 25)])
def test_profile_carries_the_districts_legislator(chamber, n):
    for d in range(1, n + 1):
        leg = get_district_profile(chamber, d)["legislator"]
        want = [m["name_as_written"] for m in SERVING if m["chamber"] == chamber and m["district"] == d]
        assert [x["name_as_written"] for x in leg["serving"]] == want
        assert leg["roster_date_fetched"] == META["date_fetched"]
        assert leg["election_note"] == META["election_note"]


def test_tool_is_wired_into_chat():
    names = {s["name"] for s in build_tool_schemas()}
    assert "find_district_by_legislator" in names and "find_district_by_legislator" in TOOL_FUNCS
    assert "Name a legislator only if this turn's tool results contain that name" in SYSTEM_PROMPT
    assert "don't guess who represents a district" in SYSTEM_PROMPT


# --- grounding ------------------------------------------------------------------

def test_grounding_accepts_names_from_tool_results():
    m = SERVING[0]
    out = [lt.find_district_by_legislator(m["name_first_last"])]
    text = f"{m['name_first_last']} represents District {m['district']}. Senator {m['name_first_last']} ..."
    assert unsourced_legislators(text, out) == []


def test_grounding_flags_roster_names_not_in_tool_results():
    a, b = SERVING[0], SERVING[1]
    out = [lt.find_district_by_legislator(a["name_first_last"])]
    text = f"{a['name_first_last']} and {b['name_first_last']} both serve."
    assert unsourced_legislators(text, out) == [b["name_first_last"]]


def test_grounding_flags_titled_names_not_in_tool_results():
    out = [lt.find_district_by_legislator(SERVING[0]["name_first_last"])]
    assert unsourced_legislators("Ask Representative Jane Madeup about it.", out) == ["Jane Madeup"]
    assert unsourced_legislators("Senator Madeup. The roster agrees.", out) == ["Madeup"]


def test_grounding_ignores_district_phrases():
    text = "Senate District 9 matches the 9th Representative District; Representatives for the session."
    assert unsourced_legislators(text, []) == []


# --- UI chip --------------------------------------------------------------------

def test_ui_chip_uses_a_real_serving_legislator():
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'data-question="Which district does ([^"?]+) represent\?"', html)
    assert m and m.group(1) == example_legislator()
    r = lt.find_district_by_legislator(m.group(1))
    assert r["match"]["serving"] is True
    assert r["match"]["name_first_last"] == m.group(1)


def test_about_page_describes_the_roster():
    from fastapi.testclient import TestClient
    from web import app as webapp
    r = TestClient(webapp.app).get("/about")
    assert r.status_code == 200
    assert META["date_fetched"] in r.text
    for stmt in META["membership_as_stated_by_site"].values():
        assert stmt in r.text
    assert "party affiliation is not" in r.text
    assert "doesn't know who represents" not in r.text


# --- roster freshness note ----------------------------------------------------------

NOTE = lt.as_listed_note()


def test_as_listed_note_wording():
    from datetime import date
    d = date.fromisoformat(META["date_fetched"])
    assert NOTE.startswith(f"as listed on capitol.hawaii.gov on {d:%B} {d.day}, {d.year}")
    if META["election_status"] == "pre-election":
        assert NOTE.endswith(", before the Nov 3, 2026 general election results took effect")


def test_every_legislator_result_carries_the_note():
    m = SERVING[0]
    assert lt.find_district_by_legislator(m["name_first_last"])["as_listed_note"] == NOTE
    assert lt.find_district_by_legislator("Lee")["as_listed_note"] == NOTE
    assert get_district_profile(m["chamber"], m["district"])["legislator"]["as_listed_note"] == NOTE


def test_not_found_says_the_roster_may_predate_the_election():
    r = lt.find_district_by_legislator("Jane Madeup")
    assert r["as_listed_note"] == NOTE and NOTE in r["error"]
    if META["election_status"] == "pre-election":
        assert r["roster_may_predate_election"] is True
        assert "may predate the election" in r["error"]


def test_prompt_requires_the_note_and_the_predate_message():
    assert "include the tool's `as_listed_note` word for word" in SYSTEM_PROMPT
    assert "don't just say \"not found\"" in SYSTEM_PROMPT
    assert "may predate the election" in SYSTEM_PROMPT


def _chat_with(answer, calls):
    from fastapi.testclient import TestClient
    from tests.test_web import FakeAgent
    from web import app as webapp
    webapp.app.state.limiter = webapp.default_limiter()
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: FakeAgent(answer, calls)
    try:
        return TestClient(webapp.app).post("/chat", json={"messages": [{"role": "user", "content": "q"}]}).json()
    finally:
        webapp.app.dependency_overrides.clear()


def test_chat_response_carries_note_when_a_legislator_is_named(monkeypatch):
    from agent.chat import ToolCall
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    m = SERVING[0]
    out = lt.find_district_by_legislator(m["name_first_last"])
    call = ToolCall("find_district_by_legislator", {"name": m["name_first_last"]}, out, False)
    assert _chat_with(f"{m['name_first_last']} represents District {m['district']}.", [call])["roster_note"] == NOTE
    # Not named in the answer: no note.
    assert _chat_with("I couldn't tell.", [call])["roster_note"] is None


def test_chat_response_carries_note_when_no_one_is_found(monkeypatch):
    from agent.chat import ToolCall
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    out = lt.find_district_by_legislator("Jane Madeup")
    call = ToolCall("find_district_by_legislator", {"name": "Jane Madeup"}, out, True)
    assert _chat_with("That name isn't in the roster.", [call])["roster_note"] == NOTE


def test_profile_without_naming_the_legislator_has_no_note(monkeypatch):
    from agent.chat import ToolCall
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    out = get_district_profile("house", 1)
    call = ToolCall("get_district_profile", {"chamber": "house", "district": 1}, out, False)
    assert _chat_with("House District 1 is on Hawaiʻi Island.", [call])["roster_note"] is None


def test_ui_renders_the_roster_note():
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data.roster_note" in js and "roster-note" in js


def test_guardrail_fails_a_named_legislator_without_the_note():
    from agent.chat import ToolCall, TurnResult
    from tests.guardrail_checks import check_answer
    m = SERVING[0]
    out = lt.find_district_by_legislator(m["name_first_last"])
    calls = [ToolCall("find_district_by_legislator", {"name": m["name_first_last"]}, out, False)]
    chamber = "Senate" if m["chamber"] == "senate" else "House"
    bare = f"{m['name_first_last']} represents {chamber} District {m['district']}. Source: {out['citation']}."
    good = f"{m['name_first_last']} represents {chamber} District {m['district']}, {NOTE}. Source: {out['citation']}."
    fails = lambda t: check_answer("legislator", "q", TurnResult(text=t, tool_calls=calls, stop_reason="end_turn",
                                                                  models=["m"]), "m")["failures"]
    assert any("roster note" in f for f in fails(bare))
    assert fails(good) == []


def test_one_pager_shows_the_serving_legislator_with_source_and_date():
    from fastapi.testclient import TestClient
    from web import app as webapp
    for chamber, d in (("house", 28), ("senate", 18), ("senate", 9)):
        r = TestClient(webapp.app).get(f"/district/{chamber}/{d}/profile")
        assert r.status_code == 200
        serving = [m for m in SERVING if m["chamber"] == chamber and m["district"] == d]
        former = [m for m in ROSTER if m["chamber"] == chamber and m["district"] == d and not m["serving"]]
        text = r.text.replace("&quot;", '"').replace("&#x27;", "'")
        assert serving[0]["name_first_last"] in text
        assert all(f["name_first_last"] not in text for f in former)
        assert NOTE in text
        assert serving[0]["profile_url"].replace("&", "&amp;") in r.text
        assert "capitol.hawaii.gov" in text and META["date_fetched"] in text
