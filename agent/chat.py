"""Chat agent over agent/tools.py: Claude narrates, the tools supply every number.

Interactive CLI:
    python -m agent.chat

Programmatic use:
    from agent.chat import Agent
    agent = Agent()
    result = agent.ask("What is the median household income in Senate District 12?")
    print(result.text)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic
from dotenv import load_dotenv

from agent import legislator_tools, program_tools, tools

# ---------------------------------------------------------------------------
# Config: defaults here, overridable with environment variables (or .env).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


MODEL = os.getenv("CHAT_MODEL", "claude-sonnet-5-5")
EFFORT = os.getenv("CHAT_EFFORT", "medium")  # low | medium | high | xhigh | max
MAX_TOKENS = _env_int("CHAT_MAX_TOKENS", 8000)  # per response; includes thinking
MAX_TOOL_CALLS = _env_int("CHAT_MAX_TOOL_CALLS", 10)  # per user question
# On a safety-classifier decline, the API re-runs the request on a fallback model
# chosen by refusal category. Set CHAT_SERVER_FALLBACK=0 to disable.
USE_SERVER_FALLBACK = os.getenv("CHAT_SERVER_FALLBACK", "1") != "0"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You answer questions from Hawaiʻi state legislators and their staff about the socio-economic \
profile of Hawaiʻi State House and State Senate districts. Your only data source is the set of \
tools you have, which return U.S. Census Bureau American Community Survey (ACS) estimates, \
district context from the State of Hawaiʻi 2021 Reapportionment Commission final report, \
assistance program facts quoted from official agency web pages, and the legislator roster from \
the Hawaiʻi State Legislature website (capitol.hawaii.gov).

Numbers
- Every number you state must appear in a tool result from this conversation. This includes \
values, margins of error, ranks, district counts, years and thresholds.
- Never do arithmetic on tool output. Do not compute differences, ratios, sums, averages, \
"X times higher", percentage-point gaps, or confidence ranges (value minus/plus MOE). When you \
want to give a range, use the tool's `lower_bound` and `upper_bound` fields. If a user wants a comparison, show the values side \
by side and let them read it.
- Report values as given, or rounded to fewer decimals. Use digits (for example $79,754 or \
12.1%), not words like "about 80 thousand".
- If you need a number you don't have, call a tool. If no tool provides it, say so.
- Percent-unit metrics are already percentages: a value of 12.09 means 12.09%.
- Give margins of error as "± MOE". Their confidence level is in the tool's \
`moe_confidence_level` field; to describe how large an MOE is relative to its estimate, use \
`moe_percent_of_estimate` (already a percent) rather than `moe_to_estimate`.
- Write values and MOEs using the tool's `value_display`, `moe_display` and `range_display` \
strings exactly as given, so decimals are consistent (for example "12.10%" with "± 2.10").

Sources
- Cite every figure with source, table, year and boundary vintage, using the tool's \
`citation` string (for example: {CITATION_EXAMPLE}). One citation line per table used is \
enough, placed right after the \
figures it covers.
- When a value has low_reliability true, include its `caveat` in plain words next to that \
value. Do not drop or soften it.
- When values are close and their margins of error overlap, say the difference may not be \
meaningful.

Tone
- Nonpartisan and plain-language, written for legislators and staff. Short paragraphs or \
small tables.
- Describe what the data shows. Do not make policy recommendations, judge whether a value is \
good or bad, or speculate about causes.
- Use correct Hawaiian spelling with ʻokina and kahakō for places: Hawaiʻi, Oʻahu, Kauaʻi, \
Maui, Molokaʻi, Lānaʻi, Niʻihau, Kahoʻolawe, Mānoa, Kāneʻohe, Waiʻanae, Hāmākua. Census \
place names in tool output may lack these marks; write them correctly in your prose.

District context (get_district_profile → district_context)
- `areas` lists the communities and islands a district covers, from the official boundary \
descriptions in the 2021 Reapportionment Commission final report. Name only areas in that \
list, keep "a portion of" where the list says so, and cite its `citation` (document and \
page). If a district has no `areas`, don't describe its geography.
- `reapportionment_population` is the permanent resident population the Commission used to \
draw the district. When it differs a lot from the ACS total population, you may point out the \
gap, using only `acs_vs_reapportionment` (its `display` string and values) and the report's \
own statements in `population_basis` (for example that non-permanent military and student \
residents were removed). The Armed Forces metrics (`armed_forces`, `armed_forces_rate`) can \
add context. Say the two figures measure different things (see the tool's `note`); don't \
present the difference as a count of military members or students, and don't speculate \
beyond what the report states.

Legislators (find_district_by_legislator, get_district_profile → legislator)
- Name a legislator only if this turn's tool results contain that name. Never name a \
legislator from general knowledge, and don't guess who represents a district.
- Write names as the tool gives them (`name_first_last` or `name_as_written`); don't add \
or change spellings.
- To find a legislator's district, call find_district_by_legislator. If it returns \
`ambiguous`, list the candidates (name, chamber, district) and ask which one the user means; \
don't choose. If it returns an error, don't just say "not found": say the name isn't in the \
roster, that the roster is `as_listed_note`, and (when `roster_may_predate_election` is true) \
that it may predate the election, so a newly elected legislator may not be listed yet. Don't \
suggest similar names.
- Whenever you name a legislator, include the tool's `as_listed_note` word for word in the \
same sentence or the next one (for example: "Stanley Chang represents Senate District 9, as \
listed on capitol.hawaii.gov on <date>, a listing that does not reflect the Nov 3, 2026 \
general election."). Cite the tool's `citation`.
- If the site lists a member whose term ended (`term_ended`, or `serving` false), say so with \
the site's status text; the serving member is the one in `serving`.
- Don't mention or guess party affiliation; the data doesn't include it.

Assistance programs (find_programs, district_program_context)
- Describe a program only with facts from these tools: its name, agency, who it serves, \
eligibility factors, income figures, how to apply and source. Don't add programs, \
requirements, dollar amounts, deadlines or phone numbers from general knowledge. If a program \
isn't in the tool results, say the tool doesn't cover it.
- Never say someone is eligible or qualifies. Say they "may qualify" and must confirm with the \
agency. Pass on each program's screening notes, time-sensitive notes (such as closed waiting \
lists, with the verification date) and published-requirement mismatches.
- Cite each program with its `citation` (agency, page URL, verification date).
- With district_program_context, district measures show where needs may be concentrated; say \
they don't show who qualifies. Compare a district with the `median_of_districts_display` only.
- Ask for household facts only if they would change the answer, and only use facts the user \
actually stated.

Limits
- The tools know a district's geography only through `district_context.areas`, and its \
legislator only through the roster. Don't guess anything beyond that.
- If a question can't be answered with this data (for example unemployment, crime, school \
results, trends over time, sub-district areas, or a district number that doesn't exist), say \
so clearly, then say what data would answer it (for example a specific ACS table, the Hawaiʻi \
DBEDT, or the state legislature's district maps). Don't fill gaps with general knowledge.
- Whenever you tell the user what data is or isn't available (including when declining a \
question), call list_available_metrics first and name measures and programs only with the \
`label` and `name` strings it returns, exactly as written. Never paraphrase, shorten or \
rename a measure (for example, don't turn a label into "SNAP participation").
- If a tool returns an error, tell the user what was wrong (for example the valid district \
range from the error) instead of guessing what they meant.
"""


# The citation example is built from the data, so it always names the loaded ACS release.
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{CITATION_EXAMPLE}", tools._citation(["B19013"]))


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

def build_tool_schemas() -> list[dict]:
    metrics = tools.load_data()["metrics"]
    chamber = {"type": "string", "enum": list(tools.CHAMBERS),
               "description": "'house' (State House, lower chamber) or 'senate' (State Senate, upper chamber)."}
    metric = {"type": "string", "enum": metrics,
              "description": "Metric key. Rates are percentages (0-100)."}
    program_needs = program_tools.load_programs()["needs"]
    return [
        {
            "name": "get_district_profile",
            "description": (
                "Get every metric for one Hawaiʻi legislative district: value, margin of error, "
                "low_reliability flag and caveat, ACS table, year, boundary vintage, source URL "
                "and citation. Also returns district_context from the 2021 Reapportionment "
                "Commission final report: the areas/communities the district covers, its "
                "reapportionment (permanent resident) population, and a side-by-side with the "
                "ACS population. Also returns legislator: who the capitol.hawaii.gov roster lists "
                "for the district, with the roster's fetch date and election note. Use this for "
                "'tell me about district N', 'what areas does it cover', 'who represents it' and "
                "'why is its population so large' questions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "chamber": chamber,
                    "district": {"type": "integer", "description": "District number."},
                },
                "required": ["chamber", "district"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "compare_districts",
            "description": (
                "Show one metric side by side for two or more districts in the same chamber. "
                "Returns each district's value, margin of error, reliability and citation."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "chamber": chamber,
                    "districts": {"type": "array", "items": {"type": "integer"},
                                  "description": "District numbers to compare."},
                    "metric": metric,
                },
                "required": ["chamber", "districts", "metric"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "rank_districts",
            "description": (
                "Rank the districts in one chamber by a metric. order='desc' gives the highest "
                "values first, 'asc' the lowest first. top_n is how many to return."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "chamber": chamber,
                    "metric": metric,
                    "top_n": {"type": "integer", "description": "Number of districts to return."},
                    "order": {"type": "string", "enum": list(tools.ORDERS)},
                },
                "required": ["chamber", "metric", "top_n", "order"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "statewide_context",
            "description": (
                "Get the minimum, maximum and median of district values for a metric, per "
                "chamber, to put one district's value in context. The median is the median of "
                "district estimates, not a statewide ACS estimate. chamber='both' returns both "
                "chambers."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": metric,
                    "chamber": {"type": "string", "enum": [*tools.CHAMBERS, "both"]},
                },
                "required": ["metric", "chamber"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "find_programs",
            "description": (
                "Find Hawaiʻi assistance programs for a need, with facts quoted from official agency "
                "pages: who the program serves, key eligibility factors, published income figures "
                "(only where the agency publishes them), how to apply, and source. Optional household "
                "facts are compared with published requirements; the result never decides "
                "eligibility. Use for 'what help is there for...' questions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "need": {"type": "string", "enum": program_needs + ["any"],
                             "description": "The kind of help needed, or 'any'."},
                    "household": {
                        "type": "object",
                        "description": "Only facts the user actually stated. Leave out anything unknown.",
                        "properties": {
                            "household_size": {"type": "integer"},
                            "island": {"type": "string", "enum": list(program_tools.ISLANDS)},
                            **{k: {"type": "boolean"} for k in program_tools.FACT_KEYS},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["need"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "district_program_context",
            "description": (
                "For one district, pair its ACS measures that relate to each assistance program "
                "(for example SNAP receipt and poverty for SNAP, veterans for veterans services, "
                "renter cost burden for housing vouchers) with the program's official description "
                "and the median of districts in the chamber. Use for 'which programs matter most "
                "for district N' questions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "chamber": chamber,
                    "district": {"type": "integer", "description": "District number."},
                },
                "required": ["chamber", "district"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "find_district_by_legislator",
            "description": (
                "Find the chamber and district a Hawaiʻi state legislator represents, from the "
                "member roster on capitol.hawaii.gov. Matches whole name words, ignoring case, "
                "ʻokina and kahakō; never guesses. Returns one match, or 'ambiguous' with the "
                "candidates when several legislators fit, or an error when none does. Use for "
                "'which district does <name> represent' questions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The legislator's name as the user gave it (surname, or first and last name)."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "list_available_metrics",
            "description": (
                "List every measure the tools can report (metric id, full label as written in the "
                "data, ACS table ID) and every assistance program they cover (id, name, agency). "
                "Call this whenever you tell the user what data is or isn't available, and use the "
                "labels and names exactly as returned."
            ),
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": True,
        },
    ]


def _statewide(metric: str, chamber: str = "both") -> dict:
    return tools.statewide_context(metric, None if chamber == "both" else chamber)


TOOL_FUNCS: dict[str, Callable[..., dict]] = {
    "get_district_profile": tools.get_district_profile,
    "compare_districts": tools.compare_districts,
    "rank_districts": tools.rank_districts,
    "statewide_context": _statewide,
    "find_programs": program_tools.find_programs,
    "district_program_context": program_tools.district_program_context,
    "list_available_metrics": program_tools.list_available_metrics,
    "find_district_by_legislator": legislator_tools.find_district_by_legislator,
}


def run_tool(name: str, args: dict) -> dict:
    func = TOOL_FUNCS.get(name)
    if func is None:
        return {"error": f"Unknown tool {name!r}."}
    try:
        return func(**args)
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    input: dict
    output: dict
    is_error: bool


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    requests: int = 0
    # One entry per API call: {input, output, cache_read, cache_creation}. Shows whether each
    # call read the conversation so far from cache.
    per_call: list[dict] = field(default_factory=list)

    def add(self, u: Any) -> None:
        call = {k: getattr(u, f, 0) or 0 for k, f in (
            ("input", "input_tokens"), ("output", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"), ("cache_creation", "cache_creation_input_tokens"))}
        self.requests += 1
        self.input_tokens += call["input"]
        self.output_tokens += call["output"]
        self.cache_creation_input_tokens += call["cache_creation"]
        self.cache_read_input_tokens += call["cache_read"]
        self.per_call.append(call)

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_input_tokens + self.cache_read_input_tokens)

    def merge(self, other: "Usage") -> None:
        for f in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens", "requests"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.per_call = other.per_call + self.per_call if other is not self else self.per_call


# Prompt caching. Two breakpoints per request (the API allows four):
#   1. the system prompt (stable for every request), and
#   2. the last content block of the last message, so each request writes the whole
#      conversation so far and the next request (a tool round-trip or a follow-up
#      question) reads it back from cache, including earlier tool results.
# The marker is added to a copy of the messages at send time and never stored, so the
# history keeps exactly one moving marker instead of accumulating one per turn.
CACHE_CONTROL = {"type": "ephemeral"}


def with_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """A shallow copy of `messages` whose last message's last content block carries
    cache_control. Stored messages are not modified."""
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content, "cache_control": CACHE_CONTROL}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = [*content[:-1], {**content[-1], "cache_control": CACHE_CONTROL}]
    else:
        return list(messages)  # nothing markable (the last message is always ours, so rare)
    return [*messages[:-1], {**last, "content": blocks}]


@dataclass
class TurnResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    models: list[str] = field(default_factory=list)
    hit_tool_limit: bool = False


class ServiceNotConfigured(RuntimeError):
    """The Anthropic API key is missing."""


def make_client() -> anthropic.Anthropic:
    load_dotenv(REPO_ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ServiceNotConfigured("ANTHROPIC_API_KEY not set (add it to .env at the repo root).")
    return anthropic.Anthropic()  # reads the key from the environment


class Agent:
    """Multi-turn conversation. Each ask() runs the tool loop to a final answer."""

    def __init__(self, client: anthropic.Anthropic | None = None, *, model: str = MODEL,
                 max_tokens: int = MAX_TOKENS, max_tool_calls: int = MAX_TOOL_CALLS,
                 effort: str = EFFORT) -> None:
        self.client = client or make_client()
        self.model = model
        self.max_tokens = max_tokens
        self.max_tool_calls = max_tool_calls
        self.effort = effort
        self.tools = build_tool_schemas()
        self.messages: list[dict] = []
        self.usage = Usage()

    def _create(self, *, allow_tools: bool):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            tools=self.tools,
            tool_choice={"type": "auto"} if allow_tools else {"type": "none"},
            output_config={"effort": self.effort},
            messages=with_cache_breakpoint(self.messages),
        )
        if USE_SERVER_FALLBACK:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def ask(self, question: str) -> TurnResult:
        """Run one question to a final answer. On an API error the conversation is
        rolled back to before the question, so it can be retried."""
        checkpoint = len(self.messages)
        try:
            return self._ask(question)
        except Exception:
            del self.messages[checkpoint:]
            raise

    def revise(self, failures: list[str]) -> TurnResult:
        """Ask the model once to fix an answer that failed verification. The feedback and the
        rejected answer are removed from the conversation afterwards, so the history reads as
        question -> tool calls -> revised answer."""
        rejected = len(self.messages) - 1  # the final assistant message that failed
        feedback = (
            "Automatic verification of your last answer found these problems:\n"
            + "\n".join(f"- {f}" for f in failures)
            + "\n\nRewrite the whole answer so it fixes every problem. Use only numbers, names, "
            "programs and caveats that appear in the tool results in this conversation (call a tool "
            "if you need a value you don't have), and follow all the earlier rules. Reply with the "
            "corrected answer only; don't mention this check."
        )
        result = self.ask(feedback)
        if rejected >= 0 and self.messages[rejected]["role"] == "assistant":
            del self.messages[rejected:rejected + 2]
        return result

    def _ask(self, question: str) -> TurnResult:
        # Stored as a text block, the same shape it is sent in, so earlier turns render
        # byte-identically on every later request (prompt-cache prefix match).
        self.messages.append({"role": "user", "content": [{"type": "text", "text": question}]})
        result = TurnResult(text="")
        calls_left = self.max_tool_calls

        while True:
            response = self._create(allow_tools=calls_left > 0)
            result.usage.add(response.usage)
            result.models.append(response.model)
            result.stop_reason = response.stop_reason
            # Append the full content (thinking, fallback and tool_use blocks included).
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if calls_left <= 0:
                    result.hit_tool_limit = True
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                        "content": "Tool-call limit for this question reached. Answer with "
                                   "the tool results you already have, and say what you "
                                   "could not look up.",
                    })
                    continue
                calls_left -= 1
                args = block.input if isinstance(block.input, dict) else {}
                output = run_tool(block.name, args)
                is_error = "error" in output
                result.tool_calls.append(ToolCall(block.name, args, output, is_error))
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(output, ensure_ascii=False),
                    "is_error": is_error,
                })
            # All results for one assistant turn go back in a single user message.
            self.messages.append({"role": "user", "content": tool_results})

        result.text = "\n".join(
            b.text for b in response.content if b.type == "text"
        ).strip()
        if response.stop_reason == "refusal":
            result.text = result.text or "The model declined to answer this request."
        elif response.stop_reason == "max_tokens":
            result.text += "\n\n[Answer cut off at the response length limit.]"
        self.usage.merge(result.usage)
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        agent = Agent()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Hawaiʻi district profiles ({agent.model}). Ask a question, or type 'quit'.")
    while True:
        try:
            q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in {"quit", "exit", "q"}:
            break
        try:
            r = agent.ask(q)
        except anthropic.AuthenticationError:
            print("ERROR: the Anthropic API key was rejected.", file=sys.stderr)
            return 1
        except anthropic.RateLimitError:
            print("Rate limited; wait a moment and try again.", file=sys.stderr)
            continue
        except anthropic.APIStatusError as exc:
            print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError:
            print("Network error reaching the Anthropic API.", file=sys.stderr)
            continue
        print(f"\nagent> {r.text}")
        names = ", ".join(c.name for c in r.tool_calls) or "none"
        print(f"\n[tools: {names} | tokens this turn: {r.usage.total:,}]")
    print(f"Session tokens: {agent.usage.total:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
