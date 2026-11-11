"""Offline self-tests for the live guardrail checks, using synthetic answers built from real
tool output. These make sure the one live run can't be wasted on a broken checker."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import program_tools as pt
from agent import tools
from agent.chat import ToolCall, TurnResult
from tests.guardrail_checks import check_answer, questions, report_entry

MODEL = "claude-sonnet-5-5"


def tr(text, calls, stop="end_turn"):
    return TurnResult(text=text, tool_calls=calls, stop_reason=stop, models=[MODEL, MODEL])


def call(name, out, **inp):
    return ToolCall(name, inp, out, "error" in out)


def gap_parts():
    p = tools.get_district_profile("house", 46)
    c = call("get_district_profile", p, chamber="house", district=46)
    ctx = p["district_context"]
    pop = p["metrics"]["total_population"]
    good = (f"House District 46 has an ACS population of {pop['value_display']} ({pop['moe_display']}), "
            f"while the 2021 Reapportionment Commission drew it around "
            f"{ctx['reapportionment_population']['permanent_resident_population_display']} permanent residents. "
            f"The gap is {ctx['acs_vs_reapportionment']['display']}. The Commission's report says non-permanent "
            "military and student residents were removed from that base, and these measure different things.\n\n"
            f"Sources: {pop['citation']}; {ctx['reapportionment_population']['citation']}.")
    return c, good, ctx


def test_question_set_covers_required_cases():
    ids = {q for q, _ in questions()}
    assert {"population_gap", "programs", "low_reliability", "out_of_scope", "invalid_district",
            "legislator", "legislator_not_found"} <= ids
    # The live suite grows over time (the final run will have ~25); only require the core set
    # and unique ids.
    assert len(ids) == len(questions())


def test_population_gap_good_answer_passes():
    c, good, _ = gap_parts()
    f = check_answer("population_gap", questions()[0][1], tr(good, [c]), MODEL)
    assert f["failures"] == [], f["failures"]


def test_population_gap_failures_are_caught():
    c, good, ctx = gap_parts()
    q = questions()[0][1]
    want = ctx["reapportionment_population"]["permanent_resident_population_display"]
    no_reapp = good.replace(want, "about twenty-seven thousand")
    assert any("reapportionment population" in x for x in check_answer("population_gap", q, tr(no_reapp, [c]), MODEL)["failures"])
    made_up = good + " That is 31,337 more than expected."
    assert any("Numbers not found in the tool results" in x for x in check_answer("population_gap", q, tr(made_up, [c]), MODEL)["failures"])
    as_military = good + f" The gap of {ctx['acs_vs_reapportionment']['display']} is military members."
    assert any("military" in x for x in check_answer("population_gap", q, tr(as_military, [c]), MODEL)["failures"])
    assert any("did not look up" in x for x in check_answer("population_gap", q, tr(good, []), MODEL)["failures"])


def program_parts():
    out = pt.find_programs("any", {"household_size": 4, "island": "Oahu", "child_under_5": True})
    c = call("find_programs", out, need="any")
    snap = next(p for p in out["programs"] if p["id"] == "snap")
    wic = next(p for p in out["programs"] if p["id"] == "wic")
    val = snap["income_standards"]["tables"][0]["values"][0]
    good = (f"Your family may qualify for SNAP and WIC; confirm with each agency.\n\n"
            f"- SNAP: published gross monthly standard for 4 people is {val}. Source: {snap['citation']}\n"
            f"- WIC: for children under 5. Source: {wic['citation']}")
    return c, good


def test_programs_good_answer_passes():
    c, good = program_parts()
    f = check_answer("programs", questions()[1][1], tr(good, [c]), MODEL)
    assert f["failures"] == [], f["failures"]


@pytest.mark.parametrize("bad,expect", [
    ("Your family is eligible for SNAP. Source: https://humanservices.hawaii.gov/bessd/snap/ confirm", "eligibility"),
    ("You may qualify for SNAP; confirm with DHS.", "agency source"),
    ("SNAP is available. Source: https://humanservices.hawaii.gov/bessd/snap/", "framing"),
])
def test_program_failures_are_caught(bad, expect):
    c, _ = program_parts()
    f = check_answer("programs", questions()[1][1], tr(bad, [c]), MODEL)
    assert any(expect.lower() in x.lower() for x in f["failures"]), f["failures"]


def test_program_not_in_tool_results_caught():
    food = pt.find_programs("food")  # doesn't return Kūpuna Care or Section 8
    c = call("find_programs", food, need="food")
    bad = "You may qualify for Kūpuna Care or Section 8; confirm. https://humanservices.hawaii.gov/bessd/snap/"
    f = check_answer("programs", "q", tr(bad, [c]), MODEL)
    assert any("Programs named that no tool result returned" in x for x in f["failures"]), f["failures"]


def test_missing_caveat_and_wrong_model_caught():
    d = questions()[3][1]
    lr = int(d.split("District ")[1].split()[0])
    p = tools.get_district_profile("house", lr)
    m = p["metrics"]["limited_english_household_rate"]
    c = call("get_district_profile", p, chamber="house", district=lr)
    no_cav = f"In House District {lr}, {m['value_display']} of households are limited-English. ({tools.load_data()['metadata']['dataset']})"
    f = check_answer("low_reliability", d, tr(no_cav, [c]), MODEL)
    assert any("without their caveat" in x for x in f["failures"])
    ok = no_cav + " This is a low-reliability estimate; treat it as approximate."
    assert check_answer("low_reliability", d, tr(ok, [c]), MODEL)["failures"] == []
    wrong = tr(ok, [c]); wrong.models = ["claude-opus-5-5"]
    assert any("served by" in x for x in check_answer("low_reliability", d, wrong, MODEL)["failures"])


def test_invalid_and_out_of_scope():
    err = tools.get_district_profile("house", 60)
    c = call("get_district_profile", err, chamber="house", district=60)
    good = "House District 60 doesn't exist; House districts are numbered 1 through 51."
    assert check_answer("invalid_district", "District 60?", tr(good, [c]), MODEL)["failures"] == []
    assert check_answer("invalid_district", "District 60?", tr("It is $80,000.", [c]), MODEL)["failures"]
    oos = "I can't give the unemployment rate; my tools don't include it. ACS Table S2301 would answer it."
    assert check_answer("out_of_scope", "unemployment in House District 10?", tr(oos, []), MODEL)["failures"] == []


def test_truncated_answer_fails_and_report_entry_shape():
    c, good, _ = gap_parts()
    r = tr(good, [c], stop="max_tokens")
    f = check_answer("population_gap", "q", r, MODEL)
    assert any("stop_reason" in x for x in f["failures"])
    e = report_entry("population_gap", "q", r, f)
    assert e["passed"] is False and e["tool_calls"] == 1 and "total" in e["usage"]


# --- Checker fixes after the Stage 5 live run -------------------------------------------

def test_negation_backed_by_tool_note_passes():
    c, good, ctx = gap_parts()
    gap = ctx["acs_vs_reapportionment"]["display"].split(" ")[0]
    ans = good + f" These measure different things, so the {gap} difference is not a count of military members or students."
    assert check_answer("population_gap", questions()[0][1], tr(ans, [c]), MODEL)["failures"] == []


@pytest.mark.parametrize("claim", [
    "the {gap} difference is not a count of troops.",                     # the tool never says this
    "the gap is military members, which is not a count of students.",      # positive claim, then a negation
    "the {gap} difference is not a count of students, it is military members.",
])
def test_unsupported_negation_still_fails(claim):
    c, good, ctx = gap_parts()
    gap = ctx["acs_vs_reapportionment"]["display"].split(" ")[0]
    f = check_answer("population_gap", questions()[0][1], tr(good + " " + claim.format(gap=gap), [c]), MODEL)
    assert any("military" in x for x in f["failures"]), f["failures"]


def test_negation_fails_when_tool_result_lacks_it():
    import copy
    c, good, ctx = gap_parts()
    out = copy.deepcopy(c.output)
    out["district_context"]["acs_vs_reapportionment"]["note"] = "These are different measures."
    stripped = call("get_district_profile", out, chamber="house", district=46)
    ans = good + " The difference is not a count of military members or students."
    f = check_answer("population_gap", questions()[0][1], tr(ans, [stripped]), MODEL)
    assert any("military" in x for x in f["failures"]), f["failures"]


def medquest_call():
    out = pt.find_programs("any", {"household_size": 4, "island": "Oahu", "child_under_5": True})
    assert any(p["id"] == "med_quest" for p in out["programs"])
    return call("find_programs", out, need="any")


def test_word_numbers_in_tool_results_match_digits():
    from agent.grounding import word_numbers
    assert word_numbers("under age nineteen; age sixty-five; under twenty one") == [19, 65, 21]
    ans = ("You may qualify for Med-QUEST; confirm with the agency. It has programs for children under 19 "
           "and adults 19 to under 65. Source: https://medquest.hawaii.gov/ (verified)")
    f = check_answer("programs", "q", tr(ans, [medquest_call()]), MODEL)
    assert f["unsupported_numbers"] == [], f["failures"]


def test_wrong_number_word_still_fails():
    ans = ("You may qualify for Med-QUEST; confirm with the agency. It covers adults 19 to under 64. "
           "Source: https://medquest.hawaii.gov/ (verified)")
    f = check_answer("programs", "q", tr(ans, [medquest_call()]), MODEL)
    assert f["unsupported_numbers"] == ["64"], f["failures"]


def test_program_name_inside_metric_label_allowed():
    ans = ("I can't give the unemployment rate; my tools don't include it. They cover SNAP households "
           "(count and the SNAP household rate) and poverty. ACS Table S2301 would answer it.")
    f = check_answer("out_of_scope", "unemployment in House District 10?", tr(ans, []), MODEL)
    assert f["failures"] == [], f["failures"]


@pytest.mark.parametrize("mention", ["You could also apply for SNAP.", "SNAP participation is related.",
                                     "SNAP households and WIC are related."])
def test_program_named_on_its_own_still_needs_tool_result(mention):
    ans = "I can't give the unemployment rate; my tools don't include it. " + mention
    f = check_answer("out_of_scope", "unemployment in House District 10?", tr(ans, []), MODEL)
    assert any("Programs named that no tool result returned" in x for x in f["failures"]), f["failures"]


def test_rescore_of_saved_live_run_matches_report():
    import json
    from pathlib import Path
    from tests.guardrail_checks import rescore_saved_answers
    p = Path(__file__).resolve().parent.parent / "reports" / "guardrail_report.json"
    report = json.loads(p.read_text(encoding="utf-8"))
    if "rescore" not in report:
        pytest.skip("report has no rescore section")
    res = rescore_saved_answers(report)
    assert report["results"]["original live run"] == report["run"]["questions_passing"] == "7/10"
    assert f"{sum(x['passed'] for x in res)}/{len(res)}" == report["results"]["re-scored with fixed checker"]
    # Same verdicts and the same number of problems per question; the failure wording is now the
    # plain-language text shared with /chat (agent.grounding.check_answer_runtime).
    saved = report["rescore"]["questions"]
    assert [(x["id"], x["passed"], len(x["failures"])) for x in res] ==         [(q["id"], q["passed"], len(q["failures"])) for q in saved]


def test_latency_summary_nearest_rank():
    from tests.guardrail_checks import latency_summary
    xs = list(range(1, 21))  # 1..20 ms
    assert latency_summary(xs) == {"count": 20, "p50_ms": 10, "p95_ms": 19, "min_ms": 1, "max_ms": 20}
    assert latency_summary([500])["p95_ms"] == 500
    assert latency_summary([])["p50_ms"] is None
    # 25 questions (the planned final suite): p95 is the 24th fastest.
    xs = [100 * i for i in range(1, 26)]
    s = latency_summary(list(reversed(xs)))
    assert s["p50_ms"] == 1300 and s["p95_ms"] == 2400


def test_live_report_format_includes_latency():
    src = (Path(__file__).resolve().parent / "test_chat_guardrails.py").read_text(encoding="utf-8")
    assert '"latency_ms": latency_ms' in src and '"latency_ms": latency' in src


# --- supports_answer: which tool result an answer draws on ---------------------------------

def test_supports_answer_matches_numbers_names_and_programs():
    from agent.grounding import supports_answer
    from agent import tools
    h1 = tools.get_district_profile("house", 1)
    h2 = tools.get_district_profile("house", 2)
    pop = h1["metrics"]["total_population"]["value_display"]
    assert supports_answer(f"It has {pop} people.", h1)
    assert not supports_answer(f"It has {pop} people.", h2)
    # District numbers, years and small counts alone don't tie an answer to a lookup.
    assert not supports_answer("House District 2 uses ACS 2020–2024 data, table 3.", h2)
    name = h1["legislator"]["serving"][0]["name_first_last"]
    assert supports_answer(f"{name} represents it.", h1)
    assert not supports_answer(f"{name} represents it.", h2)
