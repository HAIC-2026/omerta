"""Offline tests for the Armed Forces metric, district context from the 2021 Reapportionment
Commission final report, and consistent display formatting."""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from agent import tools
from agent.grounding import collect_tool_numbers, extract_numbers, is_supported

CTX_PATH = tools.CONTEXT_PATH
CTX = json.loads(CTX_PATH.read_text(encoding="utf-8"))
DATA = tools.load_data()


def ctx_rec(chamber, d):
    return next(r for r in CTX["districts"] if r["chamber"] == chamber and r["district"] == d)


def acs_rec(chamber, d):
    return DATA["index"][(chamber, d)]


# ---------------------------------------------------------------- Armed Forces metric

@pytest.mark.parametrize("key", ["armed_forces", "armed_forces_rate"])
def test_armed_forces_metric_present_everywhere(key):
    for r in DATA["districts"]:
        m = r["metrics"][key]
        assert m["table_id"] == "B23025"
        assert m["dataset"] == DATA["metadata"]["dataset"]
        assert "B23025_006E" in m["variable_ids"] and "B23025_006M" in m["variable_ids"]
        assert m["value"] is not None and m["moe"] is not None
        assert isinstance(m["low_reliability"], bool)
        assert "api.census.gov" in m["source_url"] and "key=" not in m["source_url"]


def test_armed_forces_rate_definition():
    m = DATA["districts"][0]["metrics"]["armed_forces_rate"]
    assert m["unit"] == "percent"
    assert set(m["variable_ids"]) == {"B23025_006E", "B23025_006M", "B23025_001E", "B23025_001M"}
    assert "Armed Forces" in m["variable_labels"]["B23025_006"]
    for r in DATA["districts"]:
        rate = r["metrics"]["armed_forces_rate"]
        assert rate["numerator_estimate"] == r["metrics"]["armed_forces"]["value"]
        if rate["denominator_estimate"]:
            assert rate["value"] == round(100 * rate["numerator_estimate"] / rate["denominator_estimate"], 2)


def test_armed_forces_in_profile_with_citation():
    p = tools.get_district_profile("house", 46)
    m = p["metrics"]["armed_forces"]
    assert m["value"] == acs_rec("house", 46)["metrics"]["armed_forces"]["value"]
    assert "Table B23025;" in m["citation"]


# ---------------------------------------------------------------- context file integrity

def test_context_covers_every_district_and_nothing_unverified():
    keys = {(r["chamber"], r["district"]) for r in CTX["districts"]}
    assert keys == set(DATA["index"])
    assert CTX["verification"]["unverified"] == []
    for r in CTX["districts"]:
        assert "reapportionment" in r and "boundary_description" in r, (r["chamber"], r["district"])


def test_context_source_is_official():
    src = CTX["source"]
    assert src["url"].startswith("https://elections.hawaii.gov/")
    assert "2021 Reapportionment Commission" in src["title"]


def test_population_totals_are_consistent():
    pb = CTX["population_basis"]
    assert pb["census_population"] - pb["non_permanent_residents_extracted"] == pb["permanent_resident_population"]
    for chamber in tools.CHAMBERS:
        total = sum(r["reapportionment"]["permanent_resident_population"]
                    for r in CTX["districts"] if r["chamber"] == chamber)
        assert total == pb["permanent_resident_population"]


def test_exclusion_recorded_as_stated():
    pb = CTX["population_basis"]
    assert "non-permanent military and student residents were extracted" in pb["exclusion_statement"]
    # The statement's figures are the recorded figures (not typed separately).
    nums = {int(n) for n in extract_numbers(pb["statement"])}
    assert pb["non_permanent_residents_extracted"] in nums
    assert pb["permanent_resident_population"] in nums
    for key in ("statement_source", "exclusion_source"):
        assert pb[key]["pdf_page"] > 0 and pb[key]["page_label"]


def test_every_district_cites_pages():
    for r in CTX["districts"]:
        bd = r["boundary_description"]
        assert bd["pdf_page"] > 0 and re.fullmatch(r"D-\d{3}", bd["page_label"])
        assert bd["description"].startswith("The ")
        for s in r["reapportionment"]["sources"]:
            assert s["pdf_page"] > 0 and s["page_label"]
            assert "Targets and Deviations" in s["table"]


def test_senate_descriptions_list_every_house_district():
    listed = {h for r in CTX["districts"] if r["chamber"] == "senate"
              for h in r["boundary_description"]["representative_districts_listed"]}
    assert listed == set(tools._districts_in("house"))


def test_areas_are_clean():
    for r in CTX["districts"]:
        for a in r["boundary_description"].get("areas") or []:
            assert a and len(a) < 60 and not re.search(r"comprises|includes|District|described", a), a


# ---------------------------------------------------------------- exposed via the profile tool

def test_profile_includes_context_for_house_46():
    p = tools.get_district_profile("house", 46)
    ctx = p["district_context"]
    c = ctx_rec("house", 46)
    assert ctx["areas"]["areas_as_written"] == c["boundary_description"]["areas"]
    assert "Wahiawa" in ctx["areas"]["areas_as_written"]
    assert re.search(r"Appendix D .*p\. D-\d{3} \(PDF p\. \d+\)", ctx["areas"]["citation"])

    rp = ctx["reapportionment_population"]
    assert rp["permanent_resident_population"] == c["reapportionment"]["permanent_resident_population"]
    assert re.search(r"Table \d+ .*p\. \d+ \(PDF p\. \d+\)", rp["citation"])
    assert "non-permanent military and student" in rp["population_basis"]["exclusion_verbatim"]

    cmp_ = ctx["acs_vs_reapportionment"]
    acs = acs_rec("house", 46)["metrics"]["total_population"]
    assert cmp_["acs_total_population"] == acs["value"]
    assert cmp_["acs_minus_reapportionment"] == acs["value"] - rp["permanent_resident_population"]
    assert cmp_["acs_minus_reapportionment_moe"] == acs["moe"]
    assert cmp_["display"] == f"{cmp_['acs_minus_reapportionment']:,.0f} ± {acs['moe']:,.0f}"
    assert "not a count of military" in cmp_["note"]


@pytest.mark.parametrize("chamber", tools.CHAMBERS)
def test_every_profile_has_context(chamber):
    for d in tools._districts_in(chamber):
        ctx = tools.get_district_profile(chamber, d)["district_context"]
        assert {"areas", "reapportionment_population", "acs_vs_reapportionment"} <= set(ctx)


def test_profile_without_context_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "load_context", lambda path=None: None)
    p = tools.get_district_profile("house", 46)
    assert "district_context" not in p and "metrics" in p


def test_context_numbers_pass_the_grounding_check():
    """An answer quoting the context figures must pass the guardrail's number check."""
    p = tools.get_district_profile("house", 46)
    ctx = p["district_context"]
    answer = (f"The ACS estimates {p['metrics']['total_population']['value_display']} people "
              f"({p['metrics']['total_population']['moe_display']}), while the Commission's "
              f"permanent resident population is {ctx['reapportionment_population']['permanent_resident_population_display']}. "
              f"The gap is {ctx['acs_vs_reapportionment']['display']}. The report removed "
              f"{ctx['reapportionment_population']['population_basis']['non_permanent_residents_extracted']:,} "
              "non-permanent residents statewide.")
    sources: set[Decimal] = set()
    collect_tool_numbers(p, sources)
    assert all(is_supported(n, sources) for n in extract_numbers(answer))


# ---------------------------------------------------------------- display formatting

PCT = re.compile(r"^\d{1,3}(,\d{3})*\.\d{2}%$")


def test_display_strings_have_consistent_decimals():
    for chamber in tools.CHAMBERS:
        for d in tools._districts_in(chamber):
            for k, m in tools.get_district_profile(chamber, d)["metrics"].items():
                if m["value"] is None:
                    continue
                v, e = m["value_display"], m["moe_display"]
                assert e.startswith("± ")
                if m["unit"] == "percent":
                    assert PCT.match(v), (k, v)
                    assert PCT.match(e[2:]), (k, e)   # e.g. "± 2.10%", never "± 2.1"
                elif m["unit"] == "dollars":
                    assert re.fullmatch(r"\$\d{1,3}(,\d{3})*", v) and re.fullmatch(r"± \$\d{1,3}(,\d{3})*", e)
                elif m["unit"] == "years":
                    assert re.fullmatch(r"\d+\.\d", v) and re.fullmatch(r"± \d+\.\d", e)
                else:
                    assert re.fullmatch(r"\d{1,3}(,\d{3})*", v) and re.fullmatch(r"± \d{1,3}(,\d{3})*", e)


def test_display_example():
    assert tools._fmt(2.1, "percent", sign="± ") == "± 2.10%"
    assert tools._fmt(1.74, "percent", sign="± ") == "± 1.74%"
    assert tools._fmt(79754.0, "dollars") == "$79,754"
    assert tools._fmt(42.1, "years") == "42.1"
