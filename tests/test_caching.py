"""Prompt caching for conversations: a cache_control breakpoint on the last message of every
request (plus the system prompt's), never stored in the history, and cache token counts in
the request log. Offline: the API client is a fake that records what it was sent."""

from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent import tools
from agent.chat import CACHE_CONTROL, Agent, with_cache_breakpoint
from web import app as webapp


def _markers(obj) -> int:
    """Number of cache_control markers anywhere in a request part."""
    if isinstance(obj, dict):
        return ("cache_control" in obj) + sum(_markers(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_markers(v) for v in obj)
    return 0


def _strip(obj):
    """The request with cache_control removed, in a comparable form (SDK blocks as dicts)."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    if isinstance(obj, SimpleNamespace):
        return _strip(vars(obj))
    return obj


# --- with_cache_breakpoint ---------------------------------------------------------------

def test_marks_a_plain_string_message():
    out = with_cache_breakpoint([{"role": "user", "content": "hi"}])
    assert out == [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": CACHE_CONTROL}]}]


def test_marks_only_the_last_block_of_the_last_message():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [SimpleNamespace(type="tool_use", id="t1")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a"},
                                     {"type": "tool_result", "tool_use_id": "t2", "content": "b"}]},
    ]
    before = copy.deepcopy(msgs)
    out = with_cache_breakpoint(msgs)
    assert out[-1]["content"][-1]["cache_control"] == CACHE_CONTROL
    assert "cache_control" not in out[-1]["content"][0]
    assert _markers(out) == 1
    assert _strip(msgs) == _strip(before) and _markers(msgs) == 0   # input not modified


def test_empty_history_is_left_alone():
    assert with_cache_breakpoint([]) == []


# --- the agent sends one moving breakpoint and stores none ------------------------------------

def _usage(read=0, write=0, inp=10):
    return SimpleNamespace(input_tokens=inp, output_tokens=5, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write)


class RecordingClient:
    def __init__(self, script):
        self.script, self.sent = list(script), []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.sent.append(copy.deepcopy(kw))
        return self.script.pop(0)


def _text(t, usage):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=t)], stop_reason="end_turn",
                           model="m", usage=usage)


def _tool(usage):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="tu_1", name="get_district_profile",
                                                    input={"chamber": "house", "district": 1})],
                           stop_reason="tool_use", model="m", usage=usage)


def test_every_request_has_the_system_marker_plus_one_on_the_last_message():
    client = RecordingClient([_tool(_usage(write=3000)), _text("a1", _usage(read=3000, write=900)),
                              _text("a2", _usage(read=3900, write=40))])
    agent = Agent(client)
    agent.ask("Population of House District 1?")
    agent.ask("And again?")
    assert len(client.sent) == 3
    for req in client.sent:
        assert _markers(req["system"]) == 1                  # system prompt cache kept
        assert _markers(req["messages"]) == 1                # exactly one moving marker
        assert _markers(req["system"]) + _markers(req["messages"]) + _markers(req["tools"]) <= 4
        assert "cache_control" in req["messages"][-1]["content"][-1]
    # The tool round-trip marks the tool_result block; the follow-up marks the new question.
    assert client.sent[1]["messages"][-1]["content"][-1]["type"] == "tool_result"
    assert client.sent[2]["messages"][-1]["content"][-1]["text"] == "And again?"
    # Nothing stored carries a marker, so markers never accumulate.
    assert _markers(agent.messages) == 0


def test_each_request_is_the_previous_one_plus_new_turns():
    """Prefix stability: apart from the moving marker, every request starts with the exact
    messages of the request before it, so the cache written last time can be read."""
    client = RecordingClient([_tool(_usage()), _text("a1", _usage()), _text("a2", _usage())])
    agent = Agent(client)
    agent.ask("Population of House District 1?")
    agent.ask("And again?")
    for prev, nxt in zip(client.sent, client.sent[1:]):
        a, b = _strip(prev["messages"]), _strip(nxt["messages"])
        assert b[:len(a)] == a
        assert _strip(prev["system"]) == _strip(nxt["system"])


def test_usage_keeps_cache_tokens_per_api_call():
    client = RecordingClient([_tool(_usage(write=3000)), _text("a1", _usage(read=3000, write=900))])
    r = Agent(client).ask("Population of House District 1?")
    assert r.usage.cache_creation_input_tokens == 3900 and r.usage.cache_read_input_tokens == 3000
    assert r.usage.per_call == [
        {"input": 10, "output": 5, "cache_read": 0, "cache_creation": 3000},
        {"input": 10, "output": 5, "cache_read": 3000, "cache_creation": 900},
    ]


# --- request log ------------------------------------------------------------------------

def test_request_log_has_cache_read_and_creation_tokens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    lines = []

    class H(logging.Handler):
        def emit(self, record):
            lines.append(json.loads(record.getMessage()))
    h = H()
    webapp.request_log.addHandler(h)
    pop = tools.get_district_profile("house", 1)["metrics"]["total_population"]["value_display"]
    client = RecordingClient([
        _tool(_usage(write=3000)), _text(f"House District 1 has {pop} people.", _usage(read=3000, write=900)),
        _text(f"Yes, {pop}.", _usage(read=3900, write=40, inp=8)),
    ])
    webapp.app.dependency_overrides[webapp.get_agent] = lambda: Agent(client)
    webapp.app.state.limiter = webapp.default_limiter()
    try:
        c = TestClient(webapp.app)
        sid = c.post("/chat", json={"question": "Population of House District 1?"}).json()["session_id"]
        c.post("/chat", json={"session_id": sid, "question": "Really?"})
    finally:
        webapp.app.dependency_overrides.clear()
        webapp.request_log.removeHandler(h)
    first, second = lines[-2], lines[-1]
    assert first["cache_creation_input_tokens"] == 3900 and first["cache_read_input_tokens"] == 3000
    assert [c["cache_read"] for c in first["api_calls"]] == [0, 3000]
    assert second["session_turn"] == 2
    assert second["cache_read_input_tokens"] == 3900 and second["cache_creation_input_tokens"] == 40
    assert second["api_calls"] == [{"input": 8, "output": 5, "cache_read": 3900, "cache_creation": 40}]
    assert second["cache_hit_ratio"] == round(3900 / (8 + 3900 + 40), 3)
