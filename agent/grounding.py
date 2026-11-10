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
