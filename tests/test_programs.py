"""Offline tests for data/programs.json, find_programs, district_program_context and the
program grounding checks."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import urlparse

import pytest

from agent import program_tools as pt
from agent import tools
from agent.chat import build_tool_schemas, run_tool
from agent.grounding import (
    collect_tool_numbers,
    eligibility_claims,
    extract_numbers,
    is_supported,
    unsourced_programs,
)

DATA = json.loads(pt.PROGRAMS_PATH.read_text(encoding="utf-8"))
PROGRAMS = DATA["programs"]
ALLOWED = set(DATA["metadata"]["allowed_source_domains"])
REQUIRED_IDS = {"snap", "med_quest", "wic", "h_heap", "tanf_taonf", "hcv_section8", "school_meals",
                "kupuna_care", "veterans_services"}


def norm(s):
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("‘", "'")
                  .replace("–", "-").replace("—", "-")).strip()


# ---------------------------------------------------------------- data file

def test_required_programs_present():
    assert REQUIRED_IDS <= {p["id"] for p in PROGRAMS}


@pytest.mark.parametrize("p", PROGRAMS, ids=[p["id"] for p in PROGRAMS])
def test_program_record_complete(p):
    for key in ("name", "agency", "agency_quote", "needs", "source_url", "serves", "eligibility_factors",
                "application", "date_verified"):
        assert p.get(key), f"{p['id']} missing {key}"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["date_verified"])
    assert p["serves"]["text"] and p["serves"]["quote"]
    assert p["application"]["url"].startswith("https://")
    for f in p["eligibility_factors"]:
        assert f["factor"] and f["quote"]


@pytest.mark.parametrize("p", PROGRAMS, ids=[p["id"] for p in PROGRAMS])
def test_sources_are_official(p):
    urls = [p["source_url"], p["application"].get("quote_source_url"), p.get("agency_quote_source_url"),
            *[n.get("source_url") for n in p.get("time_sensitive", [])]]
    for u in filter(None, urls):
        assert urlparse(u).scheme == "https"
        assert (urlparse(u).hostname or "").removeprefix("www.") in ALLOWED, u


@pytest.mark.parametrize("p", PROGRAMS, ids=[p["id"] for p in PROGRAMS])
def test_no_unquoted_numbers(p):
    """Every number in a paraphrase must appear in its quote, so no threshold is guessed."""
    def check(text, quote):
        q = {Decimal(n) for n in extract_numbers(quote)}
        for n in extract_numbers(text):
            assert is_supported(n, q), f"{p['id']}: {n!r} in {text!r} not in its quote"
    check(p["serves"]["text"], p["serves"]["quote"] + " " + p["agency_quote"])
    for f in p["eligibility_factors"]:
        check(f["factor"], f["quote"])


def test_income_tables_match_their_quotes():
    for p in PROGRAMS:
        for t in (p.get("income_standards") or {}).get("tables", []):
            q = norm(t["quote"])
            assert norm(t["title"]) in q
            for size, vals in t["rows"].items():
                assert f"{size} {' '.join(vals)}" in q, (p["id"], size)
            assert " ".join(t["each_additional_member"]) in q


def test_screening_points_at_real_factors():
    for p in PROGRAMS:
        for rule in (p.get("screening") or {}).values():
            assert 0 <= rule["factor_index"] < len(p["eligibility_factors"])
            for f in rule.get("facts", []):
                assert f in pt.FACT_KEYS


def test_district_metrics_exist():
    metrics = set(tools.load_data()["metrics"])
    for p in PROGRAMS:
        assert p["district_metrics"] and set(p["district_metrics"]) <= metrics


# ---------------------------------------------------------------- find_programs

def test_find_programs_food_framing_and_citations():
    out = pt.find_programs("food")
    ids = {p["id"] for p in out["programs"]}
    assert {"snap", "wic", "school_meals"} <= ids
    assert "confirm with the agency" in out["framing"].lower()
    for p in out["programs"]:
        assert p["citation"] and p["source_url"] in p["citation"] and p["date_verified"] in p["citation"]
        assert any("agency" in n.lower() for n in p["screening"]["notes"])
        assert p["screening"]["status"] != "eligible"


def test_never_declares_eligibility():
    blob = json.dumps(pt.find_programs("any", {"household_size": 3, "island": "Oahu", "child_under_5": True,
                                              "age_60_plus": True, "veteran": True}))
    assert not re.search(r'"(eligible|qualifies|qualified)"', blob)
    assert "may_qualify_confirm_with_agency" in blob


def test_household_size_returns_published_row():
    out = pt.find_programs("food", {"household_size": 4})
    snap = next(p for p in out["programs"] if p["id"] == "snap")
    gross = snap["income_standards"]["tables"][0]
    src = next(p for p in PROGRAMS if p["id"] == "snap")["income_standards"]["tables"][0]
    assert gross["values"] == src["rows"]["4"]
    big = pt.find_programs("food", {"household_size": 12})
    t = next(p for p in big["programs"] if p["id"] == "snap")["income_standards"]["tables"][0]
    assert "values" not in t and t["each_additional_member"]


def test_island_requirement_moves_hcv_aside():
    out = pt.find_programs("housing", {"island": "Maui"})
    assert not out["programs"]
    hcv = out["programs_with_a_requirement_the_stated_facts_do_not_match"][0]
    assert hcv["id"] == "hcv_section8" and "Maui" in " ".join(hcv["screening"]["notes"])
    on_oahu = pt.find_programs("housing", {"island": "Oahu"})
    assert on_oahu["programs"][0]["id"] == "hcv_section8"
    assert on_oahu["programs"][0]["time_sensitive"]  # waiting-list note travels with it


def test_child_under_5_implies_children_and_matches_wic():
    out = pt.find_programs("food", {"child_under_5": True})
    wic = next(p for p in out["programs"] if p["id"] == "wic")
    assert wic["screening"]["status"] == pt.STATUS_MATCH
    assert out["household_facts_used"]["has_children"] is True


def test_age_requirement_for_kupuna_care():
    yes = pt.find_programs("older_adult_care", {"age_60_plus": True})
    assert yes["programs"][0]["id"] == "kupuna_care"
    no = pt.find_programs("older_adult_care", {"age_60_plus": False})
    assert not no["programs"] and no["programs_with_a_requirement_the_stated_facts_do_not_match"]
    unknown = pt.find_programs("older_adult_care")
    assert unknown["programs"][0]["screening"]["status"] == pt.STATUS_UNKNOWN


@pytest.mark.parametrize("args", [
    ("groceries", None), ("", None), (None, None),
    ("food", {"income": 2000}), ("food", {"household_size": 0}), ("food", {"household_size": True}),
    ("food", {"household_size": 2.5}), ("food", {"island": "Oʻahu"}), ("food", {"veteran": "yes"}),
    ("food", "family of 4"),
])
def test_find_programs_invalid_inputs(args):
    out = pt.find_programs(*args)
    assert "error" in out and set(out) <= {"error", "valid_needs", "valid_facts", "valid_islands"}


# ---------------------------------------------------------------- district_program_context

def test_district_program_context_house_46():
    out = pt.district_program_context("house", 46)
    assert out["island_as_written"] == "Oahu"
    vets = next(x for x in out["pairs"] if x["program"]["id"] == "veterans_services")
    v = vets["related_district_measures"]["veteran_rate"]
    assert v["value"] == tools.load_data()["index"][("house", 46)]["metrics"]["veteran_rate"]["value"]
    med = tools.statewide_context("veteran_rate", "house")["chambers"]["house"]["median_of_districts"]
    assert v["median_of_districts_in_chamber"] == med
    assert "Table B21001" in v["citation"]
    assert "geographic_note" not in next(x for x in out["pairs"] if x["program"]["id"] == "hcv_section8")
    assert "don't" in out["note"] or "do not" in out["note"]


def test_district_program_context_neighbor_island_notes_hcv():
    # House District 1 is on Hawaiʻi Island per the reapportionment description.
    out = pt.district_program_context("house", 1)
    hcv = next(x for x in out["pairs"] if x["program"]["id"] == "hcv_section8")
    assert "only" in hcv["geographic_note"] and out["island_as_written"] in hcv["geographic_note"]
    assert out["island_as_written"] != "Oahu"


@pytest.mark.parametrize("args", [("house", 52), ("senate", 0), ("assembly", 1), ("house", "x")])
def test_district_program_context_invalid(args):
    assert "error" in pt.district_program_context(*args)


def test_program_numbers_pass_grounding():
    out = pt.find_programs("food", {"household_size": 3})
    snap = next(p for p in out["programs"] if p["id"] == "snap")
    val = snap["income_standards"]["tables"][0]["values"][0]
    answer = f"For a household of 3, SNAP's published gross monthly standard is {val} (130% FPL)."
    src: set[Decimal] = set()
    collect_tool_numbers(out, src)
    assert all(is_supported(n, src) for n in extract_numbers(answer))


# ---------------------------------------------------------------- agent wiring

def test_tools_registered_with_strict_schemas():
    schemas = {s["name"]: s for s in build_tool_schemas()}
    assert {"find_programs", "district_program_context"} <= set(schemas)
    fp = schemas["find_programs"]["input_schema"]
    assert fp["properties"]["need"]["enum"][-1] == "any"
    assert set(fp["properties"]["household"]["properties"]) == {"household_size", "island", *pt.FACT_KEYS}
    assert run_tool("find_programs", {"need": "food"})["programs"]
    assert run_tool("district_program_context", {"chamber": "senate", "district": 8})["pairs"]


# ---------------------------------------------------------------- grounding checks

def test_unsourced_programs():
    food = pt.find_programs("food")
    assert unsourced_programs("You may qualify for WIC and SNAP.", [food]) == []
    assert unsourced_programs("Consider Section 8 and Kūpuna Care.", [food]) == ["Section 8", "Kūpuna Care"]
    # SNAP named in an ACS metric label counts as sourced.
    prof = tools.get_district_profile("house", 1)
    assert unsourced_programs("SNAP receipt is shown above.", [prof]) == []
    assert unsourced_programs("No programs mentioned.", []) == []


def test_eligibility_claims():
    assert eligibility_claims("You are eligible for SNAP.")
    assert eligibility_claims("This family qualifies for WIC.")
    assert not eligibility_claims("You may qualify for SNAP; confirm with the agency.")
    assert not eligibility_claims("Only the agency can decide whether you are eligible.")
    assert not eligibility_claims("They might be eligible for WIC.")
    assert not eligibility_claims("You are not eligible to apply online.")
