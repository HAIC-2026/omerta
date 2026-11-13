"""Checks that tie an answer's numbers back to tool output.

Used by the live guardrail tests (to fail answers with unsourced numbers or missing caveats)
and by the web app (to mark low-reliability values in the rendered answer).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Iterator

# A number: optional $, digits with optional thousands commas, optional decimals, optional
# ordinal suffix. Not part of a word or ID (B19013, C16002_004) or of a longer number.
NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:st|nd|rd|th)?(?![A-Za-z0-9_])"
)

CAVEAT_RE = re.compile(r"low[- ]reliab|unreliab|not reliable|less reliable|reliability (is )?low|"
                       r"use (it )?with caution|treat (this|it|these)[^.]{0,40}approximate", re.I)


def _normalize(m: re.Match) -> str:
    return m.group(1).replace(",", "") + (m.group(2) or "")


def extract_numbers(text: str) -> list[str]:
    """Numbers as normalized strings, keeping the stated precision ('12.10' stays '12.10')."""
    return [_normalize(m) for m in NUM_RE.finditer(text)]


_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"hundred": 100, "thousand": 1000}
_WORD = "|".join([*_UNITS, *_TENS, *_SCALES])
# A run of number words joined by spaces or hyphens ("sixty-five", "twenty one", "one hundred").
WORD_NUM_RE = re.compile(rf"\b(?:{_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_WORD}))*\b", re.I)


def _parse_number_words(phrase: str) -> int:
    total = current = 0
    for w in re.findall(rf"\b(?:{_WORD})\b", phrase.lower()):
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        else:
            total += (current or 1) * 1000
            current = 0
    return total + current


def word_numbers(text: str) -> list[int]:
    """Numbers written as words ("nineteen" -> 19, "sixty-five" -> 65, "twenty one" -> 21)."""
    return [_parse_number_words(m.group(0)) for m in WORD_NUM_RE.finditer(text)]


def collect_tool_numbers(obj, out: set[Decimal]) -> None:
    """Every number in tool output: numeric fields, plus numbers inside strings
    (citations, caveats, notes, URLs), including numbers written as words, so an answer
    may state "19" when the source quote says "nineteen"."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(Decimal(repr(obj)))
    elif isinstance(obj, str):
        out.update(Decimal(n) for n in extract_numbers(obj))
        out.update(Decimal(n) for n in word_numbers(obj))
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


def sig_digits(stated: str) -> int:
    """Significant digits in a stated number. A one-digit number like a rank ('2nd') is too
    coarse to identify a specific flagged value (1.53 rounds to 2)."""
    return len(stated.replace(".", "").lstrip("0").rstrip("0" if "." not in stated else "")) or 1


def flagged_metrics(output, chamber=None, district=None) -> Iterator[tuple[str | None, int | None, dict]]:
    """Yield (chamber, district, metric view) for every low_reliability metric in a tool output."""
    if isinstance(output, dict):
        chamber = output.get("chamber", chamber)
        district = output.get("district", district)
        if output.get("low_reliability") is True and "value" in output:
            yield chamber, district, output
        for v in output.values():
            yield from flagged_metrics(v, chamber, district)
    elif isinstance(output, list):
        for v in output:
            yield from flagged_metrics(v, chamber, district)


def _matches_value(stated: str, value) -> bool:
    return sig_digits(stated) >= 2 and is_supported(stated, {Decimal(repr(value))})


def flagged_values_without_caveat(text: str, outputs: Iterable[dict]) -> dict:
    """Find flagged values the answer states, and those with no caveat near them.

    A flagged value counts as stated when a number in the answer matches it (allowing
    rounding). Its caveat must appear in the same paragraph or the paragraph right after
    it; for a table, in the same row or a note after the table."""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    stated, missing = [], []
    for output in outputs:
        for _chamber, district, m in flagged_metrics(output):
            if m["value"] is None:
                continue
            for i, block in enumerate(blocks):
                hit_lines = [ln for ln in block.splitlines()
                             if any(_matches_value(n, m["value"]) for n in extract_numbers(ln))]
                if not hit_lines:
                    continue
                label = f"{m.get('label', '?')} (district {district}) = {m['value']}"
                if label not in stated:
                    stated.append(label)
                if all(ln.lstrip().startswith("|") for ln in hit_lines):
                    covered = (any(CAVEAT_RE.search(ln) for ln in hit_lines)
                               or any(CAVEAT_RE.search(b) for b in blocks[i + 1:]))
                else:
                    covered = any(CAVEAT_RE.search(b) for b in blocks[i:i + 2])
                if not covered and label not in missing:
                    missing.append(label)
    return {"stated": stated, "missing": missing}


def _program_aliases() -> dict[str, list[str]]:
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "programs.json"
    if not p.exists():
        return {}
    progs = json.loads(p.read_text(encoding="utf-8"))["programs"]
    return {pr["id"]: [pr["name"], *pr.get("also_known_as", [])] for pr in progs}


def _alias_re(alias: str) -> re.Pattern:
    # Acronyms match case-sensitively; phrases case-insensitively. Whole words only.
    flags = 0 if alias.isupper() else re.I
    return re.compile(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", flags)


def _metric_measure_res(aliases: Iterable[str]) -> list[re.Pattern]:
    """Phrases that use a program name as the name of an ACS measure the tools define: the
    metric id read as words ("snap_households" -> "SNAP households", "SNAP household rate")
    and the metric's full label ("Households that received SNAP/food stamps ...")."""
    from agent.tools import load_data
    out = []
    for mid, m in load_data()["districts"][0]["metrics"].items():
        for a in aliases:
            if a.lower() in mid.split("_"):
                # "snap_household_rate" -> SNAP household(s) rate; acronym stays case-sensitive.
                parts = [re.escape(a) if w == a.lower() else
                         "(?i:households?)" if w.startswith("household") else f"(?i:{re.escape(w)})"
                         for w in mid.split("_")]
                out.append(re.compile(r"(?<![\w-])" + r"[\s-]+".join(parts) + r"(?![\w-])",
                                      0 if a.isupper() else re.I))
            if _alias_re(a).search(m["label"]):
                out.append(re.compile(re.escape(m["label"]), re.I))
    return out


def unsourced_programs(text: str, outputs: Iterable[dict]) -> list[str]:
    """Programs the answer names that no tool result this turn mentions.

    A program counts as sourced when a program tool returned it, or when one of its names
    appears anywhere in the tool output (e.g. "SNAP" in an ACS metric label). A program name
    used only inside the name of a measure the tools define ("SNAP households", "SNAP
    household rate") is not a program mention; any other use of the name still needs a
    tool result."""
    import json
    blob = json.dumps(list(outputs), ensure_ascii=False)
    all_aliases = [a for aliases in _program_aliases().values() for a in aliases]
    for rx in _metric_measure_res(all_aliases):
        text = rx.sub(" ", text)
    returned_ids = set(re.findall(r'"id": "([a-z0-9_]+)"', blob))
    missing = []
    for pid, aliases in _program_aliases().items():
        named = [a for a in aliases if _alias_re(a).search(text)]
        if not named or pid in returned_ids:
            continue
        if not any(_alias_re(a).search(blob) for a in named):
            missing.append(named[0])
    return missing


# "is eligible", "you qualify for" ... unless hedged ("may", "might", "could") or negated.
_CLAIM_RE = re.compile(r"\b(?:is|are|you're|you are|they're|they are|will be)\s+eligible\b|"
                       r"\bqualif(?:y|ies)\s+for\b|\bwill\s+qualify\b", re.I)
_HEDGE_RE = re.compile(r"\b(?:may|might|could|not|n't|whether|if|unless|only the agency|cannot|can't)\b", re.I)


def eligibility_claims(text: str) -> list[str]:
    """Sentences that state eligibility as fact rather than 'may qualify'."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n", text):
        for m in _CLAIM_RE.finditer(sent):
            before = sent[max(0, m.start() - 40):m.start()]
            if not _HEDGE_RE.search(before) and not _HEDGE_RE.search(m.group(0)):
                out.append(sent.strip())
                break
    return out


def stated_flagged_values(text: str, outputs: Iterable[dict]) -> list[dict]:
    """Low-reliability values that the answer actually states, with the exact substrings
    that state them (so a UI can mark them) and the tool's caveat."""
    found: dict[tuple, dict] = {}
    numbers = list(NUM_RE.finditer(text))
    for output in outputs:
        for chamber, district, m in flagged_metrics(output):
            if m["value"] is None:
                continue
            hits = [n.group(0) for n in numbers if _matches_value(_normalize(n), m["value"])]
            if not hits:
                continue
            key = (chamber, district, m.get("label"), m["value"])
            entry = found.setdefault(key, {
                "chamber": chamber, "district": district, "label": m.get("label"),
                "value": m["value"], "moe": m.get("moe"), "unit": m.get("unit"),
                "value_display": m.get("value_display"), "moe_display": m.get("moe_display"),
                "caveat": m.get("caveat"), "citation": m.get("citation"), "matched_text": [],
            })
            for h in hits:
                if h not in entry["matched_text"]:
                    entry["matched_text"].append(h)
    return list(found.values())


# ---------------------------------------------------------------------------
# Legislator names
# ---------------------------------------------------------------------------

# "Senator Chang", "Sen. Stanley Chang", "Representative Della Au Belatti", "Rep. Ratcliffe".
# Up to four capitalized name words after the title; "District" (as in "Senate District",
# "2nd Representative District") ends the name and is never part of it.
# Name words are separated by single spaces; only a one-letter initial may end in a period,
# so "Senator Chang. The ..." stops at "Chang".
_NAME_WORD = r"(?!Districts?\b)(?:[A-Z]\.|[A-Z][\wʻ‘’'\-]*)"
_TITLE_NAME_RE = re.compile(
    r"\b(?:Senators?|Sen\.|Representatives?|Rep\.|President|Speaker)[  ]+"
    rf"({_NAME_WORD}(?:[  ]{_NAME_WORD}){{0,3}})"
)


def _tool_legislator_names(outputs: Iterable[dict]) -> list[str]:
    names: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k in ("name_as_written", "name_first_last"):
                if isinstance(o.get(k), str):
                    names.append(o[k])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for out in outputs:
        walk(out)
    return names


def _name_mentions(text_norm: str, m: dict) -> bool:
    """True when the answer names this roster legislator by first and last name (either
    order). Surnames alone are not checked: many are also ordinary words or place names."""
    from agent.legislator_tools import normalize_name
    first_last = normalize_name(m["name_first_last"])
    last = normalize_name(m["name_as_written"].split(",")[0])
    first = normalize_name(m["name_as_written"].split(",")[-1]).split()
    forms = {first_last, f"{last} {first[0]}" if first else last}
    if first:
        forms.add(f"{first[0]} {last}")
    return any(re.search(rf"(?<![a-z0-9]){re.escape(f)}(?![a-z0-9])", text_norm) for f in forms if f)


def unsourced_legislators(text: str, outputs: Iterable[dict]) -> list[str]:
    """Legislator names in the answer that no tool result this turn contains.

    Two checks: any roster legislator named by first and last name must appear in the tool
    output; and any name after a title (Senator, Representative, ...) must be made only of
    words from legislator names in the tool output."""
    from agent.legislator_tools import load_legislators, normalize_name
    outputs = list(outputs)
    sourced = _tool_legislator_names(outputs)
    sourced_norm = {normalize_name(n) for n in sourced}
    sourced_words = {w for n in sourced_norm for w in n.split()}
    text_norm = normalize_name(text)
    missing: list[str] = []
    roster = load_legislators()
    for m in (roster or {}).get("legislators", []):
        if _name_mentions(text_norm, m) and not ({normalize_name(m["name_as_written"]),
                                                  normalize_name(m["name_first_last"])} & sourced_norm):
            if m["name_first_last"] not in missing:
                missing.append(m["name_first_last"])
    for t in _TITLE_NAME_RE.finditer(text):
        words = normalize_name(re.sub(r"['’]s\b", "", t.group(1))).split()
        if words and not all(w in sourced_words for w in words):
            name = t.group(1).strip()
            if name not in missing:
                missing.append(name)
    return missing


def legislators_stated(text: str, outputs: Iterable[dict]) -> list[str]:
    """Legislators from this turn's tool results that the answer names: by full name, or by
    surname (the surnames checked are only those of legislators the tools returned)."""
    from agent.legislator_tools import normalize_name
    text_norm = normalize_name(text)
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("name_as_written"), str) and isinstance(o.get("name_first_last"), str):
                last = normalize_name(o["name_as_written"].split(",")[0])
                if last and re.search(rf"(?<![a-z0-9]){re.escape(last)}(?![a-z0-9])", text_norm) \
                        and o["name_first_last"] not in found:
                    found.append(o["name_first_last"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for out in outputs:
        walk(out)
    return found


def _surname_in(text_norm: str, view: dict) -> bool:
    from agent.legislator_tools import normalize_name
    last = normalize_name(view["name_as_written"].split(",")[0])
    return bool(last and re.search(rf"(?<![a-z0-9]){re.escape(last)}(?![a-z0-9])", text_norm))


def both_listed_in_answer(text: str, outputs: Iterable[dict]) -> list[dict]:
    """Districts for which the site lists two members (tool key `both_listed`) and the answer
    names at least one of them. Each entry: the site's listing_statement, who the answer
    names, and who it leaves out (`missing`). The site doesn't say which of the two holds the
    seat, so an answer that names one must name the other too."""
    from agent.legislator_tools import normalize_name
    text_norm = normalize_name(text)
    found: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            pair = o.get("both_listed")
            if isinstance(pair, list) and len(pair) > 1 and isinstance(o.get("listing_statement"), str):
                named = [v["name_first_last"] for v in pair if _surname_in(text_norm, v)]
                if named and not any(f["listing_statement"] == o["listing_statement"] for f in found):
                    found.append({"listing_statement": o["listing_statement"], "named": named,
                                  "missing": [v["name_first_last"] for v in pair
                                              if v["name_first_last"] not in named]})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for out in outputs:
        walk(out)
    return found


def roster_note_for(text: str, outputs: Iterable[dict]) -> str | None:
    """The roster's as-listed note, when the answer names a legislator from the tools or a
    legislator lookup found no one (so the reader knows how fresh the roster is)."""
    outputs = list(outputs)
    note = None
    looked_up_missing = False

    def walk(o):
        nonlocal note, looked_up_missing
        if isinstance(o, dict):
            if isinstance(o.get("as_listed_note"), str):
                note = note or o["as_listed_note"]
                if "error" in o:
                    looked_up_missing = True
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for out in outputs:
        walk(out)
    if note and (looked_up_missing or legislators_stated(text, outputs)):
        return note
    return None


# ---------------------------------------------------------------------------
# One runtime check for every answer (used by /chat and by the guardrail tests)
# ---------------------------------------------------------------------------

def _roster_notes(outputs: Iterable[dict]) -> list[str]:
    notes: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            n = o.get("as_listed_note")
            if isinstance(n, str) and n not in notes:
                notes.append(n)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for out in outputs:
        walk(out)
    return notes


def check_answer_runtime(text: str, outputs: Iterable[dict], questions: Iterable[str] = ()) -> dict:
    """Check an answer against the tool results it may draw on (for /chat: every tool output
    in the conversation so far). Numbers the user wrote in `questions` may be restated.

    Returns the findings plus `failures`: plain-language problems, empty when verified.
      - every number must match a tool number (rounding to the stated precision allowed);
      - every low-reliability value stated must have its caveat nearby;
      - every program named must come from a tool result;
      - no sentence may state eligibility as fact;
      - every legislator named must come from a tool result, with the roster's as-listed note;
      - where the site lists two members for a district, an answer naming one names both.
    """
    text = text or ""
    outputs = list(outputs)
    sources: set[Decimal] = set()
    for o in outputs:
        collect_tool_numbers(o, sources)
    for q in questions:
        sources.update(Decimal(n) for n in extract_numbers(q or ""))
    stated = extract_numbers(text)
    unsupported = sorted({n for n in stated if not is_supported(n, sources)},
                         key=lambda n: (Decimal(n), n))
    caveats = flagged_values_without_caveat(text, outputs)
    programs = unsourced_programs(text, outputs)
    claims = eligibility_claims(text)
    names = unsourced_legislators(text, outputs)
    named = legislators_stated(text, outputs)
    notes = _roster_notes(outputs)
    note_missing = bool(named and notes and not any(n.lower() in text.lower() for n in notes))
    one_of_two = [b for b in both_listed_in_answer(text, outputs) if b["missing"]]

    failures: list[str] = []
    if not text.strip():
        failures.append("The answer is empty.")
    if unsupported:
        failures.append("Numbers not found in the tool results: " + ", ".join(unsupported) + ".")
    if caveats["missing"]:
        failures.append("Low-reliability values stated without their caveat: "
                        + "; ".join(caveats["missing"]) + ".")
    if programs:
        failures.append("Programs named that no tool result returned: " + ", ".join(programs) + ".")
    if claims:
        failures.append("Eligibility stated as fact (say \"may qualify\" and to confirm with the agency): "
                        + " | ".join(claims))
    if names:
        failures.append("Legislators named that no tool result returned: " + ", ".join(names) + ".")
    if note_missing:
        failures.append(f"Legislator named without the roster note \"{notes[0]}\": " + ", ".join(named) + ".")
    for b in one_of_two:
        failures.append("The site lists two members for this district and doesn't say which holds the "
                        f"seat; the answer names only {', '.join(b['named'])}. State both, as: "
                        f"\"{b['listing_statement']}\".")

    return {
        "verified": not failures,
        "numbers_checked": len(stated),
        "numbers_supported": len(stated) - sum(1 for n in stated if n in set(unsupported)),
        "unsupported_numbers": unsupported,
        "flagged_values_stated": caveats["stated"],
        "flagged_values_missing_caveat": caveats["missing"],
        "unsourced_programs": programs,
        "unsourced_legislators": names,
        "legislators_stated": named,
        "roster_note_missing": note_missing,
        "both_listed_named_one_only": [b["listing_statement"] for b in one_of_two],
        "eligibility_claims": claims,
        "failures": failures,
    }


# --- which tool results an answer draws on (for its Sources panel) --------------------------

_YEAR_RE = re.compile(r"(?:19|20)\d\d")


def _distinctive_numbers(text: str) -> list[str]:
    """Numbers in the answer specific enough to tie it to one tool result: not a year
    ("ACS 2020–2024") and not a bare small integer such as a district number ("District 12")
    unless it is a percent. Decimals, percents and numbers of 100 or more count."""
    out = []
    for m in NUM_RE.finditer(text):
        n = _normalize(m)
        after = text[m.end():m.end() + 9].lstrip().lower()
        if "." not in n and "," not in m.group(1) and _YEAR_RE.fullmatch(n):
            continue
        if "." not in n and int(n) < 100 and not after.startswith(("%", "percent")):
            continue
        out.append(n)
    return out


def supports_answer(text: str, output: dict) -> bool:
    """True if this one tool result supports a number or fact the answer states: a distinctive
    number it contains (rounding allowed), a legislator it returned whom the answer names, or a
    program it returned that the answer names."""
    import json
    text = text or ""
    if not isinstance(output, (dict, list)):
        return False
    nums: set[Decimal] = set()
    collect_tool_numbers(output, nums)
    if any(is_supported(n, nums) for n in _distinctive_numbers(text)):
        return True
    if legislators_stated(text, [output]):
        return True
    returned = set(re.findall(r'"id": "([a-z0-9_]+)"', json.dumps(output, ensure_ascii=False)))
    return any(_alias_re(a).search(text) for pid, aliases in _program_aliases().items()
               if pid in returned for a in aliases)
