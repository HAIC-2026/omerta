"""Live guardrail suite: real questions through the agent on Sonnet. Every answer must pass
tests/guardrail_checks.check_answer (numbers traced to tool results, caveats present,
programs sourced, no eligibility claims, plus per-question checks).

These call the Anthropic API and cost money, so they only run when ANTHROPIC_API_KEY is set:

    python -m pytest -m live -s

Set GUARDRAIL_REPORT=<path> to write a JSON report of answers, tool calls, checks and tokens.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from agent import tools
from tests.guardrail_checks import (MAX_TOOL_CALLS, check_answer, dumps, latency_summary, questions,
                                    report_entry)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"),
                       reason="ANTHROPIC_API_KEY not set; live guardrail tests skipped"),
]

TEST_MAX_TOKENS = 4096  # per response (cost cap)
QUESTIONS = questions()
REPORT: dict = {"questions": []}


@pytest.fixture(scope="module")
def client():
    from agent.chat import make_client
    return make_client()


@pytest.fixture(scope="module", autouse=True)
def token_report():
    yield
    from agent.chat import EFFORT, MODEL, Usage
    total = Usage()
    for q in REPORT["questions"]:
        total.merge(Usage(**{k: q["usage"][k] for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "requests")}))
    passed = sum(1 for q in REPORT["questions"] if q["passed"])
    run = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL, "effort": EFFORT,
        "dataset": tools.load_data()["metadata"]["dataset"],
        "max_tokens_per_response": TEST_MAX_TOKENS, "max_tool_calls_per_question": MAX_TOOL_CALLS,
        "checks": [
            "Every number in the answer appears in that question's tool results (rounding to the stated "
            "precision allowed; numbers restated from the question allowed)",
            "Every low-reliability value stated has its caveat next to it",
            "Every program named comes from a tool result; eligibility is never stated as fact; program "
            "answers use 'may qualify / confirm with the agency' framing and cite the agency source",
            f"Answers with Census data cite {tools.load_data()['metadata']['dataset']!r}",
            "Per question: House 46 gap states and cites the reapportionment population without calling the "
            "gap a count of military members; program questions call the program tools; out-of-scope is "
            "declined; invalid district gets the valid range",
        ],
        "questions_passing": f"{passed}/{len(REPORT['questions'])}",
    }
    latency = latency_summary([q["latency_ms"] for q in REPORT["questions"] if "latency_ms" in q])
    totals = {**{k: v for k, v in total.__dict__.items() if k != "per_call"},
              "total_tokens": total.total, "latency_ms": latency}
    print("\n\n=== Guardrail results ===")
    for q in REPORT["questions"]:
        print(f"  {q['id']:<18} {'PASS' if q['passed'] else 'FAIL'}  tools={q['tool_calls']:<2} "
              f"requests={q['usage']['requests']:<2} tokens={q['usage']['total']:>7,}"
              + ("" if q["passed"] else f"  {q['failures']}"))
    print(f"  {run['questions_passing']} passed. TOTAL: {total.total:,} tokens over {total.requests} requests "
          f"(input {total.input_tokens:,}, output {total.output_tokens:,}, cache write "
          f"{total.cache_creation_input_tokens:,}, cache read {total.cache_read_input_tokens:,})")
    print(f"  Latency per question: p50 {latency['p50_ms']} ms, p95 {latency['p95_ms']} ms "
          f"(min {latency['min_ms']}, max {latency['max_ms']}, n={latency['count']})")
    path = os.getenv("GUARDRAIL_REPORT")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(dumps({"run": run, "totals": totals, "questions": REPORT["questions"]}),
                              encoding="utf-8")
        print(f"  report: {path}")


@pytest.mark.parametrize("qid,question", QUESTIONS, ids=[q[0] for q in QUESTIONS])
def test_guardrails(client, qid, question):
    from agent.chat import MODEL, Agent

    agent = Agent(client, max_tokens=TEST_MAX_TOKENS, max_tool_calls=MAX_TOOL_CALLS)
    t0 = time.perf_counter()
    r = agent.ask(question)
    latency_ms = round((time.perf_counter() - t0) * 1000)
    findings = check_answer(qid, question, r, MODEL)
    REPORT["questions"].append({**report_entry(qid, question, r, findings), "latency_ms": latency_ms})
    assert not findings["failures"], f"{findings['failures']}\n--- answer ---\n{r.text}"
