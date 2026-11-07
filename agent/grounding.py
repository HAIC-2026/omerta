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


def unsourced_programs(text: str, outputs: Iterable[dict]) -> list[str]:
    """Programs the answer names that no tool result this turn mentions.

    A program counts as sourced when a program tool returned it, or when one of its names
    appears anywhere in the tool output (e.g. "SNAP" in an ACS metric label)."""
    import json
    blob = json.dumps(list(outputs), ensure_ascii=False)
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
