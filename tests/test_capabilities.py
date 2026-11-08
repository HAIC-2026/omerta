"""Offline tests for list_available_metrics: its output must match the data files exactly, so
the agent's description of what data is available can't drift from what the tools return."""

from __future__ import annotations

import json
from pathlib import Path

from agent.chat import SYSTEM_PROMPT, ToolCall, TurnResult, build_tool_schemas, run_tool
from agent.program_tools import list_available_metrics
from tests.guardrail_checks import check_answer

DATA = Path(__file__).resolve().parent.parent / "data"
MODEL = "claude-sonnet-5-5"


def raw(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def test_metrics_match_districts_json():
    districts = raw("districts.json")["districts"]
    expected = {}
    for r in districts:
        for mid, m in r["metrics"].items():
            entry = {"id": mid, "label": m["label"], "table_id": m["table_id"]}
            # Every district must describe a metric the same way, or there is no single label to give.
            assert expected.setdefault(mid, entry) == entry, mid
    out = list_available_metrics()
    assert out["metrics"] == [expected[k] for k in sorted(expected)]
    assert out["dataset"] == raw("districts.json")["metadata"]["dataset"]
    assert out["boundary_vintage"] == raw("districts.json")["metadata"]["boundary_vintage"]


def test_programs_match_programs_json():
    progs = raw("programs.json")["programs"]
    assert list_available_metrics()["programs"] == [
        {"id": p["id"], "name": p["name"], "agency": p["agency"]} for p in progs]


def test_registered_as_a_tool_and_required_by_prompt():
    schema = next(s for s in build_tool_schemas() if s["name"] == "list_available_metrics")
    assert schema["input_schema"]["properties"] == {}
    assert run_tool("list_available_metrics", {}) == list_available_metrics()
    assert "call list_available_metrics" in SYSTEM_PROMPT
    assert "Never paraphrase" in SYSTEM_PROMPT


def test_out_of_scope_answer_using_exact_labels_passes_unchanged_checker():
    out = list_available_metrics()
    c = ToolCall("list_available_metrics", {}, out, False)
    labels = "\n".join(f"- {m['label']}" for m in out["metrics"])
    ans = ("I can't give the unemployment rate for House District 10; my tools don't include it. "
           f"They cover these measures:\n\n{labels}\n\nACS Table S2301 would answer it.")
    r = TurnResult(text=ans, tool_calls=[c], stop_reason="end_turn", models=[MODEL])
    f = check_answer("out_of_scope", "What is the unemployment rate in House District 10?", r, MODEL)
    assert f["failures"] == [], f["failures"]
