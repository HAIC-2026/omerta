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
BOTH = [m for m in ROSTER if m.get("both_listed")]            # two listed for a district; neither picked
SOLE = [m for m in SERVING if not m.get("both_listed")]


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


def test_one_serving_member_per_district_or_both_listed():
    """Every district has exactly one serving member, or exactly two the site lists side by
    side: one with a term-end date after the fetch date and one with no end date."""
    for chamber, n in (("house", 51), ("senate", 25)):
        for d in range(1, n + 1):
            here = lambda rows: [m for m in rows if m["chamber"] == chamber and m["district"] == d]
            sole, both = here(SOLE), here(BOTH)
            if both:
                assert not sole and len(both) == 2, (chamber, d)
                ending = [m for m in both if m["term_end_as_listed"]]
                assert len(ending) == 1 and ending[0]["term_end_as_listed"] > META["date_fetched"]
                assert ending[0]["status_as_written"] and all(m["serving"] for m in both)
            else:
                assert len(sole) == 1, (chamber, d)
    assert sorted({(m["chamber"], m["district"]) for m in BOTH}) == sorted(map(tuple, META["both_listed_districts"]))


def test_roster_matches_what_the_site_listed_on_november_13_2026():
    assert META["date_fetched"] == "2026-11-13" and META["election_status"] == "updating-after-election"
    by = lambda d: {m["name_first_last"]: m for m in ROSTER if m["chamber"] == "house" and m["district"] == d}
    # Term ended 11/02/2026 (already passed): the newly listed member is serving.
    for d, new, old in ((21, "Derek Turbin", "Jackson D. Sayama"), (26, "Ian Ross", "Della Au Belatti"),
                        (39, "Corey Rosenlee", "Elijah Pierick")):
        rows = by(d)
        assert rows[new]["serving"] is True and not rows[new]["both_listed"] and rows[new]["status_as_written"] == []
        assert rows[old]["serving"] is False and rows[old]["status_as_written"] == ["Term of Office Ended: 11/02/2026"]
    # Term-end date 11/16/2026 (after the fetch date): both stored, neither picked.
    for d, ending, other in ((14, "Elle Cochran", "Pa'ele Kiakona"), (18, "Joe Gedeon", "James Wataru"),
                             (32, "Garner M. Shimizu", "Micah Aiu")):
        rows = by(d)
        assert set(rows) == {ending, other} and all(m["both_listed"] for m in rows.values())
        assert rows[ending]["status_as_written"] == ["Term of Office Ended: 11/16/2026"]
        assert rows[other]["status_as_written"] == []
    assert META["both_listed_districts"] == [["house", 14], ["house", 18], ["house", 32]]


def _member(district, name, end=None, serving=True, chamber="house"):
    return {"chamber": chamber, "district": district, "name_as_written": name, "serving": serving,
            "term_end_as_listed": end, "both_listed": False}


def _full_house_and_senate(replace: dict):
    members = [_member(d, f"H{d}") for d in range(1, 52)] + [_member(d, f"S{d}", chamber="senate") for d in range(1, 26)]
    return [m for m in members if (m["chamber"], m["district"]) not in replace] + [x for v in replace.values() for x in v]


def test_fetch_script_records_both_listed_only_for_a_future_end_date():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import fetch_legislators as fl
    # One with a term-end date that hasn't passed + one with none: both listed, no problem.
    ms = _full_house_and_senate({("house", 18): [_member(18, "Gedeon, Joe", "2026-11-16"), _member(18, "Wataru, James")]})
    assert fl.check_districts(ms) == []
    assert [m["name_as_written"] for m in ms if m["both_listed"]] == ["Gedeon, Joe", "Wataru, James"]
    # A passed end date is serving=false: one serving member, nothing both-listed.
    ms = _full_house_and_senate({("house", 21): [_member(21, "Sayama", "2026-11-02", serving=False), _member(21, "Turbin")]})
    assert fl.check_districts(ms) == [] and not any(m["both_listed"] for m in ms)
    # Anything else ambiguous still fails, and nothing is marked.
    for bad in ([_member(5, "A"), _member(5, "B")],                                       # two, no dates
                [_member(5, "A", "2026-11-16"), _member(5, "B", "2026-11-20")],           # two, both ending
                [_member(5, "A", "2026-11-16"), _member(5, "B"), _member(5, "C")],        # three
                [_member(5, "A", "2026-11-02", serving=False)]):                          # none serving
        ms = _full_house_and_senate({("house", 5): bad})
        problems = fl.check_districts(ms)
        assert len(problems) == 1 and problems[0].startswith("house 5:"), problems
        assert not any(m["both_listed"] for m in ms)
    assert fl.check_districts(_full_house_and_senate({("house", 5): [_member(5, "A")]}) + [_member(52, "X")]) \
        == ["house: unexpected districts [52]"]


def test_fetch_script_election_status():
    import sys
    from datetime import date
    sys.path.insert(0, str(ROOT / "scripts"))
    import fetch_legislators as fl
    shown = {"house": "(Showing Representatives for the 2026 regular session)",
             "senate": "(Showing Senators for the 2026 regular session)"}
    d = date(2026, 11, 13)
    old_only = [_member(28, "Holt", "2026-02-13", serving=False)]
    assert fl.election_status(shown, d, old_only)["election_status"] == "pre-election"
    turnover = old_only + [_member(21, "Sayama", "2026-11-02", serving=False), _member(18, "Gedeon", "2026-11-16")]
    st = fl.election_status(shown, d, turnover)
    assert st["election_status"] == "updating-after-election" and "2 districts" in st["election_note"]
    assert "not certified election results" in st["election_note"]
    other = {k: v.replace("2026 regular session", "2027 regular session") for k, v in shown.items()}
    assert fl.election_status(other, d, turnover)["election_status"] == "unstated"


def test_former_members_keep_the_sites_status_text():
    for m in ROSTER:
        if not m["serving"]:
            assert any(s.startswith("Term of Office Ended:") for s in m["status_as_written"])


def test_election_status_is_recorded_as_the_site_states_it():
    for chamber, word in (("house", "Representatives"), ("senate", "Senators")):
        assert META["membership_as_stated_by_site"][chamber].startswith(f"(Showing {word} for")
    assert META["election_status"] in ("pre-election", "updating-after-election", "post-election", "unstated")
    if META["election_status"] in ("pre-election", "updating-after-election"):
        assert all("2026 regular session" in s for s in META["membership_as_stated_by_site"].values())
    if META["election_status"] == "updating-after-election":
        assert any((m["term_end_as_listed"] or "") >= "2026-11-02" for m in ROSTER)
        assert "doesn't say the update is complete" in META["election_note"]
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
        want = [m["name_as_written"] for m in SOLE if m["chamber"] == chamber and m["district"] == d]
        assert [x["name_as_written"] for x in leg["serving"]] == want
        both = [m for m in BOTH if m["chamber"] == chamber and m["district"] == d]
        assert [x["name_as_written"] for x in leg.get("both_listed", [])] == [m["name_as_written"] for m in both]
        assert ("listing_statement" in leg) == bool(both) and bool(want) != bool(both)
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
    assert r["match"]["serving"] is True and r["match"]["both_listed"] is False
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
    text = r.text.replace("&#x27;", "'")
    assert NOTE in text and META["election_note"] in text
    assert "House District 14, House District 18, House District 32 the site lists two members" in text
    assert "does not reflect" not in text and "may predate" not in text


# --- roster freshness note ----------------------------------------------------------

NOTE = lt.as_listed_note()


def test_as_listed_note_wording():
    from datetime import date
    d = date.fromisoformat(META["date_fetched"])
    assert NOTE.startswith(f"as listed on capitol.hawaii.gov on {d:%B} {d.day}, {d.year}")
    if META["election_status"] == "pre-election":
        assert NOTE.endswith(", a listing that does not reflect the Nov 3, 2026 general election")
    if META["election_status"] == "updating-after-election":
        assert NOTE == ("as listed on capitol.hawaii.gov on November 13, 2026, while the site was being "
                        "updated after the Nov 3, 2026 general election")
    pre = {"date_fetched": "2026-11-10", "election_status": "pre-election"}
    assert lt.as_listed_note(pre).endswith(", a listing that does not reflect the Nov 3, 2026 general election")
    assert lt.as_listed_note({"date_fetched": "2027-01-20", "election_status": "unstated"}) \
        == "as listed on capitol.hawaii.gov on January 20, 2027"


def test_every_legislator_result_carries_the_note():
    m = SERVING[0]
    assert lt.find_district_by_legislator(m["name_first_last"])["as_listed_note"] == NOTE
    assert lt.find_district_by_legislator("Lee")["as_listed_note"] == NOTE
    assert get_district_profile(m["chamber"], m["district"])["legislator"]["as_listed_note"] == NOTE


def test_not_found_says_a_new_member_may_not_be_listed_yet():
    r = lt.find_district_by_legislator("Jane Madeup")
    assert r["as_listed_note"] == NOTE and NOTE in r["error"]
    if META["election_status"] == "pre-election":
        assert r["roster_may_predate_election"] is True
        assert "may predate the election" in r["error"]
    if META["election_status"] == "updating-after-election":
        assert r["roster_may_predate_election"] is True
        assert "still being updated after the election, so a newly elected legislator may not be listed yet" in r["error"]
        assert "may predate" not in r["error"]


def test_prompt_requires_the_note_the_not_listed_message_and_both_names():
    assert "include the tool's `as_listed_note` word for word" in SYSTEM_PROMPT
    assert "don't just say \"not found\"" in SYSTEM_PROMPT
    assert "a newly elected legislator may not be listed yet" in SYSTEM_PROMPT
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "while the site was being updated after the Nov 3, 2026 general election" in flat
    assert "does not reflect" not in flat
    assert "`both_listed`" in flat and "`listing_statement` word for word" in flat and "Don't pick one" in flat


# --- districts where the site lists two members ------------------------------------------

GEDEON_WATARU = "capitol.hawaii.gov lists Joe Gedeon (Term of Office Ended: 11/16/2026) and James Wataru"


@pytest.mark.parametrize("m", BOTH, ids=lambda m: m["name_as_written"])
def test_lookup_finds_either_listed_member_and_returns_both(m):
    for q in (m["name_first_last"], m["name_as_written"].split(",")[0]):
        r = lt.find_district_by_legislator(q)
        assert r["match"]["name_as_written"] == m["name_as_written"] and r["match"]["both_listed"] is True
        pair = [x for x in BOTH if x["district"] == m["district"]]
        assert [x["name_as_written"] for x in r["both_listed"]] == [x["name_as_written"] for x in pair]
        for x in pair:
            assert x["name_first_last"] in r["listing_statement"]
            assert all(s in r["listing_statement"] for s in x["status_as_written"])
        assert "Don't pick one" in r["note"] and r["as_listed_note"] == NOTE
        assert "current_member_of_district" not in r


def test_listing_statement_uses_the_sites_own_status_text():
    assert lt.find_district_by_legislator("Wataru")["listing_statement"] == GEDEON_WATARU
    assert lt.find_district_by_legislator("Joe Gedeon")["listing_statement"] == GEDEON_WATARU
    leg = get_district_profile("house", 18)["legislator"]
    assert leg["listing_statement"] == GEDEON_WATARU and leg["serving"] == []
    assert "Don't pick one" in leg["both_listed_note"]


def test_grounding_accepts_both_names_and_fails_an_answer_that_picks_one():
    from agent.grounding import check_answer_runtime
    for out in (get_district_profile("house", 18), lt.find_district_by_legislator("James Wataru")):
        both = f"For House District 18, {GEDEON_WATARU}, {NOTE}."
        ok = check_answer_runtime(both, [out])
        assert ok["unsourced_legislators"] == [] and ok["both_listed_named_one_only"] == []
        assert not [f for f in ok["failures"] if "egislator" in f or "two members" in f], ok["failures"]
        for one, other in (("James Wataru", "Joe Gedeon"), ("Joe Gedeon", "James Wataru")):
            bad = check_answer_runtime(f"{one} represents House District 18, {NOTE}.", [out])
            assert bad["verified"] is False and bad["both_listed_named_one_only"] == [GEDEON_WATARU]
            assert any("lists two members" in f and one in f and GEDEON_WATARU in f for f in bad["failures"])
    # A district with one serving member is unaffected.
    out = get_district_profile("house", 21)
    r = check_answer_runtime(f"Derek Turbin represents House District 21, {NOTE}.", [out])
    assert r["both_listed_named_one_only"] == [] and not any("two members" in f for f in r["failures"])


def test_chat_response_and_ui_show_both_listed_members(monkeypatch):
    from agent.chat import ToolCall
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    out = lt.find_district_by_legislator("Wataru")
    call = ToolCall("find_district_by_legislator", {"name": "Wataru"}, out, False)
    body = _chat_with(f"For House District 18, {GEDEON_WATARU}, {NOTE}.", [call])
    assert body["roster_note"] == NOTE and body["roster_both_listed"] == [GEDEON_WATARU]
    assert body["verification"]["verified"] is True
    assert body["tool_results"][0]["output"]["listing_statement"] == GEDEON_WATARU   # what Sources reads
    # Even if the model names only one, the UI line under the answer carries both.
    body = _chat_with(f"James Wataru represents House District 18, {NOTE}.", [call])
    assert body["roster_both_listed"] == [GEDEON_WATARU] and body["verification"]["verified"] is False
    # Not about that district: nothing extra.
    turbin = lt.find_district_by_legislator("Turbin")
    call = ToolCall("find_district_by_legislator", {"name": "Turbin"}, turbin, False)
    assert _chat_with(f"Derek Turbin represents House District 21, {NOTE}.", [call])["roster_both_listed"] == []
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data.roster_both_listed" in js                                   # line under the answer
    assert "t.output.listing_statement" in js and "t.output.legislator.listing_statement" in js   # Sources


def test_one_pager_shows_both_listed_members_and_the_newly_listed_member():
    from fastapi.testclient import TestClient
    from web import app as webapp
    c = TestClient(webapp.app)
    for d in (14, 18, 32):
        pair = [m for m in BOTH if m["district"] == d]
        text = c.get(f"/district/house/{d}/profile").text.replace("&#x27;", "'")
        stmt = get_district_profile("house", d)["legislator"]["listing_statement"]
        assert f"<strong>Representative:</strong> {stmt}; {NOTE}.</p>" in text
        assert "Term of Office Ended: 11/16/2026" in text
        for m in pair:
            assert m["name_first_last"] in text and m["profile_url"].replace("&", "&amp;") in text
    text = c.get("/district/house/21/profile").text
    assert f"<strong>Representative:</strong> Derek Turbin, {NOTE}.</p>" in text
    assert "Sayama" not in text


def _chat_with(answer, calls):
    from fastapi.testclient import TestClient
    from tests.test_web import FakeAgent
    from web import app as webapp
    webapp.app.state.limiter = webapp.default_limiter()
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: FakeAgent(answer, calls)
    try:
        return TestClient(webapp.app).post("/chat", json={"question": "q"}).json()
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
