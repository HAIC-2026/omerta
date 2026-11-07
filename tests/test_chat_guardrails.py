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
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest
from dotenv import load_dotenv

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

# A number: optional $, digits with optional thousands commas, optional decimals, optional %.
# Not part of a word or ID (B19013, C16002_004) and not part of a longer number.
NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:st|nd|rd|th)?(?![A-Za-z0-9_])"
)

REPORT: dict = {"questions": [], "totals": {}}


def extract_numbers(text: str) -> list[str]:
    """Numbers as normalized strings, keeping the stated precision ('12.10' stays '12.10')."""
    return [(m.group(1).replace(",", "") + (m.group(2) or "")) for m in NUM_RE.finditer(text)]


def collect_tool_numbers(obj, out: set[Decimal]) -> None:
    """Every number in tool output: numeric fields, plus numbers inside strings
    (citations, caveats, notes, URLs)."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(Decimal(repr(obj)))
    elif isinstance(obj, str):
        out.update(Decimal(n) for n in extract_numbers(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_tool_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_tool_numbers(v, out)


def _round(v: Decimal, places: int) -> Decimal:
    return v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def is_supported(stated: str, sources: set[Decimal]) -> bool:
    """True if `stated` equals a source number, or is a source number rounded to the stated
    precision. Fractions (e.g. moe_to_estimate 0.3712) may be stated as percents (37%).
    Whole numbers with trailing zeros (80,000) may be a source rounded to that place, as long
    as at least two significant digits remain."""
    a = Decimal(stated)
    places = len(stated.split(".")[1]) if "." in stated else 0
    candidates = set(sources) | {v * 100 for v in sources if abs(v) < 1}
    for v in candidates:
        if v == a or _round(v, places) == a:
            return True
        if places == 0 and a != 0:
            digits = str(int(a))
            trailing = len(digits) - len(digits.rstrip("0"))
            for k in range(1, min(trailing, len(digits) - 2) + 1):
                if _round(v, -k) == a:
                    return True
    return False


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
    missing = flagged_values_without_caveat(r.text, r.tool_calls)
    REPORT["questions"][-1]["flagged_values_stated"] = missing["stated"]
    REPORT["questions"][-1]["flagged_values_missing_caveat"] = missing["missing"]
    assert not missing["missing"], (
        f"Low-reliability values stated without a caveat: {missing['missing']}\n--- answer ---\n{r.text}"
    )
    if qid == "low_reliability":
        assert missing["stated"], "the flagged value should be stated (with its caveat)"
    if r.tool_calls and not all(c.is_error for c in r.tool_calls):
        assert re.search(r"ACS 2023 5-year", r.text), "answer with data must cite the dataset"


CAVEAT_RE = re.compile(r"low[- ]reliab|unreliab|not reliable|less reliable|reliability (is )?low|"
                       r"use (it )?with caution|treat (this|it|these)[^.]{0,40}approximate", re.I)


def _flagged_metrics(output, district=None):
    """Yield (district, metric dict) for every low_reliability metric view in a tool output."""
    if isinstance(output, dict):
        district = output.get("district", district)
        if output.get("low_reliability") is True and "value" in output:
            yield district, output
        for v in output.values():
            yield from _flagged_metrics(v, district)
    elif isinstance(output, list):
        for v in output:
            yield from _flagged_metrics(v, district)


def _sig_digits(stated: str) -> int:
    """Significant digits in a stated number. A one-digit number like a rank ('2nd') is too
    coarse to identify a specific flagged value (1.53 rounds to 2)."""
    return len(stated.replace(".", "").lstrip("0").rstrip("0" if "." not in stated else "")) or 1


def flagged_values_without_caveat(text: str, tool_calls) -> dict:
    """Find flagged values the answer states, and those with no caveat near them.

    A flagged value counts as stated when a number in the answer matches it (allowing
    rounding). Its caveat must appear in the same paragraph or the paragraph right after
    it; for a table, in the same row or a note after the table."""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    stated, missing = [], []
    for call in tool_calls:
        for district, m in _flagged_metrics(call.output):
            if m["value"] is None:
                continue
            src = {Decimal(repr(m["value"]))}
            for i, block in enumerate(blocks):
                lines = block.splitlines()
                hit_lines = [ln for ln in lines if any(
                    _sig_digits(n) >= 2 and is_supported(n, src) for n in extract_numbers(ln))]
                if not hit_lines:
                    continue
                label = f"{m.get('label', '?')} (district {district}) = {m['value']}"
                if label not in stated:
                    stated.append(label)
                is_table = all(ln.lstrip().startswith("|") for ln in hit_lines)
                if is_table:
                    covered = (any(CAVEAT_RE.search(ln) for ln in hit_lines)
                               or any(CAVEAT_RE.search(b) for b in blocks[i + 1:]))
                else:
                    # Same paragraph, or a caveat paragraph directly after it.
                    covered = any(CAVEAT_RE.search(b) for b in blocks[i:i + 2])
                if not covered and label not in missing:
                    missing.append(label)
    return {"stated": stated, "missing": missing}


# --- offline unit tests for the number checker itself -------------------------------

def test_checker_offline():
    src = {Decimal("79754.0"), Decimal("12.09"), Decimal("0.3712"), Decimal("2023")}
    assert is_supported("79754", src)
    assert is_supported("12.1", src)
    assert is_supported("12.09", src)
    assert is_supported("37", src)        # fraction stated as percent
    assert is_supported("80000", src)     # rounded to thousands
    assert not is_supported("100000", src)  # one significant digit: too loose
    assert not is_supported("12.2", src)
    assert not is_supported("5000", src)
    assert extract_numbers("Table B19013, $79,754 (± 6,803), 12.1%, 3rd, C16002_004") == \
        ["79754", "6803", "12.1", "3"]


def test_caveat_checker_offline():
    from agent.chat import ToolCall
    out = {"district": 7, "metrics": {
        "limited_english_household_rate": {"label": "LEP", "value": 6.43, "moe": 4.27,
                                           "low_reliability": True},
        "median_age": {"label": "Age", "value": 42.1, "moe": 1.8, "low_reliability": False}}}
    calls = [ToolCall("get_district_profile", {}, out, False)]

    ok = "About 6.4% of households are limited-English. This is a low-reliability estimate."
    assert flagged_values_without_caveat(ok, calls) == {"stated": ["LEP (district 7) = 6.43"], "missing": []}

    bad = "Median age is 42.1.\n\nAbout 6.43% of households are limited-English."
    assert flagged_values_without_caveat(bad, calls)["missing"] == ["LEP (district 7) = 6.43"]

    # A caveat paragraph directly after the value counts...
    nxt = "6.43% are limited-English.\n\n**Low reliability:** treat this figure as approximate."
    assert not flagged_values_without_caveat(nxt, calls)["missing"]
    # ...but one further away does not.
    far = ("6.43% are limited-English.\n\nMedian age is 42.1.\n\n"
           "A low-reliability note about something else.")
    assert flagged_values_without_caveat(far, calls)["missing"]
    # ...but a note after a table does.
    table = "| Metric | Value |\n|---|---|\n| LEP | 6.43% |\n\n* LEP is a low-reliability estimate."
    assert not flagged_values_without_caveat(table, calls)["missing"]

    # A one-digit number (a rank like "2nd") doesn't count as stating 1.53.
    rank_out = {"district": 3, "label": "LEP", "value": 1.53, "low_reliability": True}
    rank_calls = [ToolCall("rank_districts", {}, {"results": [rank_out]}, False)]
    assert flagged_values_without_caveat("| 2nd | SD 16 | 11.59% |", rank_calls)["stated"] == []
    assert flagged_values_without_caveat("It is 1.5%.", rank_calls)["missing"]

    # Unflagged values never need a caveat.
    assert flagged_values_without_caveat("Median age is 42.1.", calls) == {"stated": [], "missing": []}
