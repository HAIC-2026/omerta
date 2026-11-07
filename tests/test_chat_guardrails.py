"""Live guardrail tests: every number the agent states must come from that turn's tool results.

These call the Anthropic API (Sonnet) and cost money, so they only run when
ANTHROPIC_API_KEY is set (in the environment or .env). Run them with:

    python -m pytest -m live -s

Set GUARDRAIL_REPORT=<path> to also write a JSON report of answers, tool calls and tokens.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import load_dotenv

from agent.grounding import (
    collect_tool_numbers,
    extract_numbers,
    eligibility_claims,
    flagged_values_without_caveat,
    is_supported,
    unsourced_programs,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"),
                       reason="ANTHROPIC_API_KEY not set; live guardrail tests skipped"),
]

# Cost caps for the test run.
TEST_MAX_TOKENS = 4096
TEST_MAX_TOOL_CALLS = 5

def _most_unreliable_house_district(metric: str) -> int:
    """House district whose flagged value for `metric` has the largest MOE/estimate.
    Chosen from the data at run time so the question always targets a flagged value."""
    from agent.tools import DATA_PATH
    recs = json.loads(DATA_PATH.read_text(encoding="utf-8"))["districts"]
    flagged = [r for r in recs if r["chamber"] == "house"
               and r["metrics"][metric]["low_reliability"]
               and r["metrics"][metric]["moe_to_estimate"] is not None]
    if not flagged:
        pytest.skip(f"no flagged House values for {metric}", allow_module_level=True)
    return max(flagged, key=lambda r: r["metrics"][metric]["moe_to_estimate"])["district"]


LOW_REL_DISTRICT = _most_unreliable_house_district("limited_english_household_rate")

QUESTIONS = [
    ("profile", "Give me a profile of House District 1."),
    ("single_value", "What is the median household income in Senate District 12?"),
    ("compare", "Compare poverty rates in House Districts 20, 21, and 22."),
    ("rank_top", "Which five House districts have the highest renter cost burden?"),
    ("rank_bottom", "Which three Senate districts have the youngest median age?"),
    ("context", "How does the veteran share in Senate District 24 compare to other Senate districts?"),
    ("reliability", "How many households receive SNAP in House District 45, and how reliable is that number?"),
    ("low_reliability", f"What share of households in House District {LOW_REL_DISTRICT} "
                        "are limited-English speaking?"),
    ("out_of_scope", "What is the unemployment rate in House District 10?"),
    ("invalid_district", "What's the median household income in House District 60?"),
]

REPORT: dict = {"questions": [], "totals": {}}


@pytest.fixture(scope="module")
def client():
    from agent.chat import make_client
    return make_client()


@pytest.fixture(scope="module", autouse=True)
def token_report():
    yield
    from agent.chat import Usage
    total = Usage()
    for q in REPORT["questions"]:
        u = Usage(**{k: q["usage"][k] for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "requests")})
        total.merge(u)
    REPORT["totals"] = {**total.__dict__, "total_tokens": total.total}
    from datetime import datetime, timezone
    from agent.chat import EFFORT, MODEL
    passed = sum(1 for q in REPORT["questions"]
                 if not q["unsupported_numbers"] and not q.get("flagged_values_missing_caveat"))
    REPORT["run"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL, "effort": EFFORT,
        "max_tokens_per_response": TEST_MAX_TOKENS,
        "max_tool_calls_per_question": TEST_MAX_TOOL_CALLS,
        "checks": [
            "Every number in the answer appears in that question's tool results "
            "(rounding to the stated precision allowed; numbers restated from the question allowed)",
            "Every low_reliability value stated in the answer has a caveat next to it",
            "Out-of-scope question is declined; invalid district gets the valid range, not a guess",
            "Answers with data cite the ACS dataset",
        ],
        "questions_passing_number_and_caveat_checks": f"{passed}/{len(REPORT['questions'])}",
    }
    print("\n\n=== Guardrail token usage ===")
    for q in REPORT["questions"]:
        print(f"  {q['id']:<17} tools={q['tool_calls']:<2} requests={q['usage']['requests']:<2} "
              f"tokens={q['usage']['total']:>7,}  unsupported={q['unsupported_numbers']}")
    print(f"  TOTAL: {total.total:,} tokens over {total.requests} requests "
          f"(input {total.input_tokens:,}, output {total.output_tokens:,}, "
          f"cache write {total.cache_creation_input_tokens:,}, "
          f"cache read {total.cache_read_input_tokens:,})")
    path = os.getenv("GUARDRAIL_REPORT")
    if path:
        ordered = {k: REPORT[k] for k in ("run", "totals", "questions")}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  report: {path}")


@pytest.mark.parametrize("qid,question", QUESTIONS, ids=[q[0] for q in QUESTIONS])
def test_every_number_comes_from_tools(client, qid, question):
    from agent.chat import MODEL, Agent

    agent = Agent(client, max_tokens=TEST_MAX_TOKENS, max_tool_calls=TEST_MAX_TOOL_CALLS)
    r = agent.ask(question)

    sources: set[Decimal] = set()
    for call in r.tool_calls:
        collect_tool_numbers(call.output, sources)
    # Restating a number from the question itself is not fabrication.
    sources.update(Decimal(n) for n in extract_numbers(question))

    stated = extract_numbers(r.text)
    unsupported = sorted({n for n in stated if not is_supported(n, sources)})

    REPORT["questions"].append({
        "id": qid, "question": question, "answer": r.text,
        "tools": [{"name": c.name, "input": c.input, "is_error": c.is_error} for c in r.tool_calls],
        "tool_calls": len(r.tool_calls), "models": r.models, "stop_reason": r.stop_reason,
        "numbers_checked": len(stated), "unsupported_numbers": unsupported,
        "usage": {**r.usage.__dict__, "total": r.usage.total},
    })

    assert r.text, "empty answer"
    assert r.stop_reason == "end_turn", f"stop_reason={r.stop_reason}"
    assert all(m == MODEL for m in r.models), f"served by {r.models}, expected {MODEL}"
    assert len(r.tool_calls) <= TEST_MAX_TOOL_CALLS
    assert not unsupported, (
        f"Numbers not found in tool results: {unsupported}\n--- answer ---\n{r.text}"
    )

    # Per-question behavior checks.
    if qid == "out_of_scope":
        assert re.search(r"(n['’]t|\bnot\b|\bno\b|\bcannot\b).{0,80}\b(available|include|cover|have|contain|give|provide)",
                         r.text, re.I | re.S), "should say the data can't answer this"
    if qid == "invalid_district":
        assert any(c.is_error for c in r.tool_calls) or not r.tool_calls
        assert "51" in r.text  # the valid range from the tool's error
    # Any flagged value the answer states must carry its caveat.
    missing = flagged_values_without_caveat(r.text, [c.output for c in r.tool_calls])
    REPORT["questions"][-1]["flagged_values_stated"] = missing["stated"]
    REPORT["questions"][-1]["flagged_values_missing_caveat"] = missing["missing"]
    assert not missing["missing"], (
        f"Low-reliability values stated without a caveat: {missing['missing']}\n--- answer ---\n{r.text}"
    )
    if qid == "low_reliability":
        assert missing["stated"], "the flagged value should be stated (with its caveat)"
    # Program facts must come from tool results; eligibility is never declared.
    outputs = [c.output for c in r.tool_calls]
    REPORT["questions"][-1]["unsourced_programs"] = unsourced_programs(r.text, outputs)
    REPORT["questions"][-1]["eligibility_claims"] = eligibility_claims(r.text)
    assert not REPORT["questions"][-1]["unsourced_programs"], (
        f"Programs named without a tool result: {REPORT['questions'][-1]['unsourced_programs']}\n{r.text}")
    assert not REPORT["questions"][-1]["eligibility_claims"], (
        f"Eligibility stated as fact: {REPORT['questions'][-1]['eligibility_claims']}\n{r.text}")
    if r.tool_calls and not all(c.is_error for c in r.tool_calls):
        assert re.search(r"ACS 2023 5-year", r.text), "answer with data must cite the dataset"
