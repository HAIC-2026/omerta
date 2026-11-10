"""Guardrail question set and answer checks, shared by the live suite
(tests/test_chat_guardrails.py) and its offline self-tests (tests/test_guardrail_checks.py)."""

from __future__ import annotations

import json
import re
from decimal import Decimal

from agent import tools
from agent.grounding import (
    collect_tool_numbers,
    eligibility_claims,
    extract_numbers,
    flagged_values_without_caveat,
    is_supported,
    unsourced_programs,
)

MAX_TOOL_CALLS = 5
DATASET = tools.load_data()["metadata"]["dataset"]  # e.g. "ACS 2020–2024 5-year"
ACS_TOOLS = {"get_district_profile", "compare_districts", "rank_districts", "statewide_context",
             "district_program_context"}
PROGRAM_TOOLS = {"find_programs", "district_program_context"}


def most_unreliable_house_district(metric: str = "limited_english_household_rate") -> int:
    """House district whose flagged value for `metric` has the largest MOE/estimate, chosen
    from the data at run time so the question always targets a flagged value."""
    from agent.tools import load_data
    recs = [r for r in load_data()["districts"] if r["chamber"] == "house"
            and r["metrics"][metric]["low_reliability"] and r["metrics"][metric]["moe_to_estimate"] is not None]
    return max(recs, key=lambda r: r["metrics"][metric]["moe_to_estimate"])["district"]


def questions() -> list[tuple[str, str]]:
    lr = most_unreliable_house_district()
    return [
        ("population_gap", "House District 46 has about twice the population of a typical House district. "
                           "Why is that?"),
        ("programs", "What assistance programs might help a family of 4 on Oʻahu with a child under 5?"),
        ("district_programs", "Which assistance programs relate to the needs in House District 46?"),
        ("low_reliability", f"What share of households in House District {lr} are limited-English speaking?"),
        ("out_of_scope", "What is the unemployment rate in House District 10?"),
        ("invalid_district", "What's the median household income in House District 60?"),
        ("single_value", "What is the median household income in Senate District 12?"),
        ("compare", "Compare poverty rates in House Districts 20, 21, and 22."),
        ("rank_top", "Which five House districts have the highest renter cost burden?"),
        ("context", "How does the veteran share in Senate District 24 compare to other Senate districts?"),
    ]


_GROUPS = r"military (?:members|personnel)|service members|troops|students"
_GAP_AS_COUNT_RE = re.compile(
    rf"(?:difference|gap|\b\d{{1,3}}(?:,\d{{3}})+\b)[^.]{{0,60}}\b(?:is|are|equals|represents)\b"
    rf"[^.]{{0,25}}\b(?:{_GROUPS})\b(?:[^.]{{0,40}}\b(?:{_GROUPS})\b)?", re.I)
# "... is not a count of military members or students": the negated statement itself, where
# what follows "count of" is only a list of the groups.
_NEGATED_COUNT_RE = re.compile(
    rf"\b(?:is|are)\s+not\s+a\s+count\s+of\s+((?:{_GROUPS})(?:\s*(?:,|\bor\b|\band\b)\s*(?:or\s+|and\s+)?(?:{_GROUPS}))*)\b",
    re.I)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _negation_backed_by_tool(claim: str, outputs) -> bool:
    """A gap-as-count match is allowed only when it is a negation ("the difference is not a
    count of military members or students") and a tool result makes that same negated
    statement about every group the answer names. Any other negation still fails."""
    neg = _NEGATED_COUNT_RE.search(claim)
    # "not a count of X, it is Y" / "is Y, which is not a count of X" are not pure negations.
    if not neg or neg.end() != len(claim) or re.search(_GROUPS, claim[:neg.start()], re.I):
        return False
    named = {g.lower() for g in re.findall(_GROUPS, neg.group(1), re.I)}
    if not named:
        return False
    for s in _strings(list(outputs)):
        for tool_neg in _NEGATED_COUNT_RE.finditer(s):
            tool_groups = {g.lower() for g in re.findall(_GROUPS, tool_neg.group(1), re.I)}
            if named <= tool_groups:
                return True
    return False


def _find_call(calls, name, **inp):
    for c in calls:
        if c.name == name and all(c.input.get(k) == v for k, v in inp.items()):
            return c
    return None


def check_answer(qid: str, question: str, r, model: str, dataset: str | None = None) -> dict:
    """Run every guardrail check on one TurnResult. Returns findings plus a list of failures."""
    text = r.text or ""
    calls = r.tool_calls
    outputs = [c.output for c in calls]
    failures: list[str] = []

    sources: set[Decimal] = set()
    for o in outputs:
        collect_tool_numbers(o, sources)
    sources.update(Decimal(n) for n in extract_numbers(question))  # restating the question is fine
    stated = extract_numbers(text)
    unsupported = sorted({n for n in stated if not is_supported(n, sources)})
    caveats = flagged_values_without_caveat(text, outputs)
    unsourced = unsourced_programs(text, outputs)
    claims = eligibility_claims(text)

    if not text:
        failures.append("empty answer")
    if r.stop_reason != "end_turn":
        failures.append(f"stop_reason={r.stop_reason}")
    if any(m != model for m in r.models):
        failures.append(f"served by {r.models}, expected {model}")
    if len(calls) > MAX_TOOL_CALLS:
        failures.append(f"{len(calls)} tool calls > {MAX_TOOL_CALLS}")
    if unsupported:
        failures.append(f"numbers not in tool results: {unsupported}")
    if caveats["missing"]:
        failures.append(f"low-reliability values without caveat: {caveats['missing']}")
    if unsourced:
        failures.append(f"programs named without a tool result: {unsourced}")
    if claims:
        failures.append(f"eligibility stated as fact: {claims}")

    ok_calls = [c for c in calls if not c.is_error]
    if any(c.name in ACS_TOOLS for c in ok_calls) and qid not in ("programs",) \
            and (dataset or DATASET) not in text:
        failures.append(f"answer with Census data doesn't cite {dataset or DATASET!r}")
    if any(c.name in PROGRAM_TOOLS for c in ok_calls):
        if not re.search(r"https?://|\.gov|verified", text):
            failures.append("program answer doesn't cite an agency source")
        if not re.search(r"\bmay (also )?qualify|might qualify|could qualify|confirm\b", text, re.I):
            failures.append("program answer lacks 'may qualify / confirm with the agency' framing")

    if qid == "population_gap":
        c = _find_call(calls, "get_district_profile", chamber="house", district=46)
        if not c:
            failures.append("did not look up House District 46's profile")
        else:
            ctx = c.output.get("district_context", {})
            rp = ctx.get("reapportionment_population", {})
            want = rp.get("permanent_resident_population_display")
            if want and want not in text:
                failures.append(f"reapportionment population {want} not stated")
            if "Reapportionment Commission" not in text:
                failures.append("reapportionment figure not cited to the Commission report")
            if any(not _negation_backed_by_tool(m.group(0), outputs) for m in _GAP_AS_COUNT_RE.finditer(text)):
                failures.append("presents the population gap as a count of military members or students")
    if qid == "programs" and not _find_call(calls, "find_programs"):
        failures.append("did not call find_programs")
    if qid == "district_programs" and not _find_call(calls, "district_program_context"):
        failures.append("did not call district_program_context")
    if qid == "low_reliability" and not caveats["stated"]:
        failures.append("the flagged value should be stated (with its caveat)")
    if qid == "out_of_scope" and not re.search(
            r"(n['’]t|\bnot\b|\bno\b|\bcannot\b).{0,80}\b(available|include|cover|have|contain|give|provide)",
            text, re.I | re.S):
        failures.append("should say the data can't answer this")
    if qid == "invalid_district":
        if calls and not any(c.is_error for c in calls):
            failures.append("invalid district should come back as a tool error")
        if "51" not in text:
            failures.append("should give the valid range (1-51)")

    return {
        "numbers_checked": len(stated),
        "unsupported_numbers": unsupported,
        "flagged_values_stated": caveats["stated"],
        "flagged_values_missing_caveat": caveats["missing"],
        "unsourced_programs": unsourced,
        "eligibility_claims": claims,
        "failures": failures,
    }


def report_entry(qid: str, question: str, r, findings: dict) -> dict:
    return {
        "id": qid, "question": question, "answer": r.text,
        "tools": [{"name": c.name, "input": c.input, "is_error": c.is_error} for c in r.tool_calls],
        "tool_calls": len(r.tool_calls), "models": r.models, "stop_reason": r.stop_reason,
        **findings, "passed": not findings["failures"],
        "usage": {**r.usage.__dict__, "total": r.usage.total},
    }


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def rescore_saved_answers(report: dict) -> list[dict]:
    """Re-run check_answer on a saved live report without calling the API. The report keeps
    each answer and its tool calls (name + input) but not the tool outputs, so the calls are
    replayed against the ACS data file the live run used (its answers quote that release's
    figures), which the tools read deterministically."""
    from agent.chat import ToolCall, TurnResult, run_tool
    qtext = dict(questions())
    model = report["run"]["model"]
    data_path = saved_run_data_path(report)
    original = tools.DATA_PATH
    tools.DATA_PATH = data_path
    tools.load_data.cache_clear()
    try:
        return _rescore(report, qtext, model, tools.load_data()["metadata"]["dataset"])
    finally:
        tools.DATA_PATH = original
        tools.load_data.cache_clear()


def saved_run_data_path(report: dict):
    """The districts file a saved live run used. Runs record it in run.dataset; reports made
    before that field existed ran on the ACS 2023 5-year file, now archived."""
    from pathlib import Path
    dataset = report["run"].get("dataset")
    current = tools.DATA_PATH
    archive = current.parent / "archive" / "districts_acs2023.json"
    if dataset is None or dataset == "ACS 2023 5-year":
        return archive
    if dataset == DATASET:
        return current
    raise RuntimeError(f"no local data file for saved run dataset {dataset!r}")


def _rescore(report, qtext, model, dataset) -> list[dict]:
    from agent.chat import ToolCall, TurnResult, run_tool
    out = []
    for q in report["questions"]:
        calls = []
        for t in q["tools"]:
            o = run_tool(t["name"], t["input"])
            calls.append(ToolCall(t["name"], t["input"], o, "error" in o))
        if [c.is_error for c in calls] != [t["is_error"] for t in q["tools"]]:
            raise RuntimeError(f"{q['id']}: replayed tool errors differ from the live run")
        r = TurnResult(text=q["answer"], tool_calls=calls, stop_reason=q["stop_reason"], models=q["models"])
        f = check_answer(q["id"], qtext.get(q["id"], q["question"]), r, model, dataset)
        out.append({"id": q["id"], "passed": not f["failures"], "failures": f["failures"],
                    "original_passed": q["passed"], "original_failures": q["failures"]})
    return out
