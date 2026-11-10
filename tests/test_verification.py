"""Runtime grounding: check_answer_runtime, the one-retry loop in /chat, and Agent.revise.
Offline: the agent and the Anthropic client are fakes."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from agent import legislator_tools as lt
from agent import program_tools as pt
from agent import tools
from agent.chat import Agent, ToolCall, TurnResult
from agent.grounding import check_answer_runtime
from web import app as webapp


def profile(chamber="house", district=1):
    out = tools.get_district_profile(chamber, district)
    return ToolCall("get_district_profile", {"chamber": chamber, "district": district}, out, False), out


# --- check_answer_runtime -------------------------------------------------------------

def test_good_answer_is_verified_with_counts():
    c, out = profile()
    pop, inc = out["metrics"]["total_population"], out["metrics"]["median_household_income"]
    text = f"House District 1 has {pop['value_display']} people ({pop['moe_display']}); income {inc['value_display']}."
    r = check_answer_runtime(text, [out])
    assert r["verified"] and r["failures"] == []
    assert r["numbers_checked"] == r["numbers_supported"] == 4  # district number, pop, moe, income


def test_made_up_number_fails_and_is_counted():
    _, out = profile()
    r = check_answer_runtime("House District 1 has 987,654 people.", [out])
    assert not r["verified"]
    assert r["numbers_checked"] == 2 and r["numbers_supported"] == 1
    assert r["unsupported_numbers"] == ["987654"]
    assert "987654" in r["failures"][0]


def test_numbers_from_the_question_may_be_restated():
    assert check_answer_runtime("House District 60 doesn't exist.", [], ["What about House District 60?"])["verified"]
    assert not check_answer_runtime("House District 60 doesn't exist.", [])["verified"]


@pytest.mark.parametrize("text,needle", [
    ("Your family is eligible for SNAP.", "Eligibility stated as fact"),
    ("You may qualify for WIC; confirm with the agency.", "Programs named"),
    ("Ask Senator Jane Madeup.", "Legislators named"),
    ("", "empty"),
])
def test_each_check_reports_a_plain_failure(text, needle):
    snap = pt.find_programs("food")
    r = check_answer_runtime(text, [snap] if "SNAP" in text else [])
    assert not r["verified"]
    assert any(needle in f for f in r["failures"]), r["failures"]


def test_flagged_value_without_caveat_fails():
    data = tools.load_data()
    d = next(r["district"] for r in data["districts"] if r["chamber"] == "house"
             and r["metrics"]["limited_english_household_rate"]["low_reliability"])
    _, out = profile("house", d)
    m = out["metrics"]["limited_english_household_rate"]
    bad = check_answer_runtime(f"In House District {d}, {m['value_display']} of households are limited-English.", [out])
    good = check_answer_runtime(f"In House District {d}, {m['value_display']} of households are limited-English. "
                                "This is a low-reliability estimate.", [out])
    assert any("caveat" in f for f in bad["failures"]) and good["verified"]


def test_legislator_without_roster_note_fails():
    out = lt.find_district_by_legislator("Stanley Chang")
    bare = check_answer_runtime("Stanley Chang represents Senate District 9.", [out])
    good = check_answer_runtime(f"Stanley Chang represents Senate District 9, {out['as_listed_note']}.", [out])
    assert bare["roster_note_missing"] and not bare["verified"]
    assert good["verified"], good["failures"]


# --- /chat retry ------------------------------------------------------------------------

class ScriptedAgent:
    """ask() returns the first scripted answer; each revise() returns the next."""

    def __init__(self, *answers, calls=None, revise_exc=None):
        self.answers, self.calls, self.revise_exc = list(answers), calls or [], revise_exc
        self.messages: list = []
        self.revisions: list[list[str]] = []

    def ask(self, q):
        return TurnResult(text=self.answers.pop(0), tool_calls=list(self.calls), stop_reason="end_turn")

    def revise(self, failures):
        self.revisions.append(failures)
        if self.revise_exc:
            raise self.revise_exc
        return TurnResult(text=self.answers.pop(0), tool_calls=[], stop_reason="end_turn")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    webapp.app.state.limiter = webapp.default_limiter()
    yield TestClient(webapp.app)
    webapp.app.dependency_overrides.clear()


def chat(client, agent, q="How many people live in House District 1?"):
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: agent
    return client.post("/chat", json={"question": q}).json()


def test_verified_answer_is_not_retried(client):
    c, out = profile()
    a = ScriptedAgent(f"House District 1 has {out['metrics']['total_population']['value_display']} people.", calls=[c])
    v = chat(client, a)["verification"]
    assert v == {"verified": True, "numbers_checked": 2, "numbers_supported": 2, "retried": False, "issues": []}
    assert a.revisions == []


def test_failed_answer_is_retried_once_with_the_failures(client):
    c, out = profile()
    good = f"House District 1 has {out['metrics']['total_population']['value_display']} people."
    a = ScriptedAgent("House District 1 has 987,654 people.", good, calls=[c])
    body = chat(client, a)
    assert body["answer"] == good
    assert body["verification"]["verified"] is True and body["verification"]["retried"] is True
    assert len(a.revisions) == 1 and "987654" in a.revisions[0][0]


def test_still_failing_after_retry_returns_unverified_with_issues(client):
    c, _ = profile()
    a = ScriptedAgent("House District 1 has 987,654 people.", "House District 1 has 987,655 people.", calls=[c])
    body = chat(client, a)
    v = body["verification"]
    assert v["verified"] is False and v["retried"] is True
    assert body["answer"].endswith("987,655 people.")
    assert any("987655" in i for i in v["issues"])
    assert len(a.revisions) == 1  # only one retry


def test_retry_api_failure_keeps_first_answer_unverified(client):
    c, _ = profile()
    exc = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    a = ScriptedAgent("House District 1 has 987,654 people.", calls=[c], revise_exc=exc)
    body = chat(client, a)
    assert body["answer"] == "House District 1 has 987,654 people."
    assert body["verification"]["verified"] is False and body["verification"]["retried"] is True


# --- Agent.revise keeps the history clean ---------------------------------------------------

def _resp(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           model="m", usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                            cache_creation_input_tokens=0,
                                                            cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, *texts):
        self.texts, self.seen = list(texts), []
        create = self.create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    def create(self, **kw):
        self.seen.append([m["role"] for m in kw["messages"]])
        return _resp(self.texts.pop(0))


def test_revise_drops_the_rejected_answer_and_feedback():
    fc = FakeClient("bad answer 987,654", "fixed answer")
    a = Agent(fc)
    a.ask("question?")
    r = a.revise(["Numbers not found in the tool results: 987654."])
    assert r.text == "fixed answer"
    # The model saw: question, rejected answer, feedback.
    assert fc.seen[-1] == ["user", "assistant", "user"]
    # Kept history: question -> revised answer.
    assert [m["role"] for m in a.messages] == ["user", "assistant"]
    assert a.messages[0]["content"] == "question?"
    assert a.messages[1]["content"][0].text == "fixed answer"


def test_server_keeps_tool_use_and_tool_result_blocks_between_turns(monkeypatch):
    """Real Agent, fake API: turn 2 is sent turn 1's full conversation from the server."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    pop = tools.get_district_profile("house", 1)["metrics"]["total_population"]["value_display"]
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    tool_use = SimpleNamespace(type="tool_use", id="tu_1", name="get_district_profile",
                               input={"chamber": "house", "district": 1})
    script = [
        SimpleNamespace(content=[tool_use], stop_reason="tool_use", model="m", usage=usage),
        SimpleNamespace(content=[SimpleNamespace(type="text", text=f"House District 1 has {pop} people.")],
                        stop_reason="end_turn", model="m", usage=usage),
        SimpleNamespace(content=[SimpleNamespace(type="text", text=f"Yes, {pop}.")],
                        stop_reason="end_turn", model="m", usage=usage),
    ]
    sent = []

    def create(**kw):
        sent.append(list(kw["messages"]))
        return script.pop(0)
    fake = SimpleNamespace(messages=SimpleNamespace(create=create),
                           beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: Agent(fake)
    webapp.app.state.limiter = webapp.default_limiter()
    try:
        c = TestClient(webapp.app)
        first = c.post("/chat", json={"question": "Population of House District 1?"}).json()
        assert first["verification"]["verified"]
        second = c.post("/chat", json={"session_id": first["session_id"], "question": "Really?"}).json()
    finally:
        webapp.app.dependency_overrides.clear()
    history = sent[-1]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant", "user"]
    assert history[1]["content"][0].type == "tool_use"
    assert history[2]["content"][0]["type"] == "tool_result" and history[2]["content"][0]["tool_use_id"] == "tu_1"
    assert history[4]["content"] == "Really?"
    # Turn 2 made no tool call, but its number is verified against turn 1's tool output.
    assert second["verification"]["verified"] and second["tool_results"] == []
