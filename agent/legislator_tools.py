"""Legislator roster tools over data/legislators.json (from capitol.hawaii.gov).

Matching is exact on whole name words after normalizing case, ʻokina and kahakō: nothing is
fuzzy-matched, abbreviated or guessed. A query that fits more than one legislator returns the
candidates and no answer.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path

LEGISLATORS_PATH = Path(__file__).resolve().parent.parent / "data" / "legislators.json"
PUBLISHER = "Hawaiʻi State Legislature"

# ʻokina (U+02BB) and the characters commonly typed in its place.
OKINA_CHARS = "ʻ‘’ʼ'`"


def normalize_name(s: str) -> str:
    """Lowercase, without ʻokina, kahakō (or any other diacritic) and punctuation:
    'Kāneʻohe' -> 'kaneohe', 'Bass, Danielle M.M.' -> 'bass danielle m m'."""
    s = "".join(ch for ch in s if ch not in OKINA_CHARS)
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", s.casefold()))


def _tokens(s: str) -> list[str]:
    return normalize_name(s).split()


@lru_cache(maxsize=None)
def load_legislators(path: str | None = None) -> dict | None:
    """The roster file, or None if it hasn't been built."""
    p = Path(path) if path else LEGISLATORS_PATH
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _citation(meta: dict, source_url: str) -> str:
    return f"{PUBLISHER}, member listing ({source_url}), fetched {meta['date_fetched']}"


def _date_words(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


def as_listed_note(meta: dict | None = None) -> str | None:
    """The freshness phrase shown with every legislator name, e.g. "as listed on
    capitol.hawaii.gov on November 13, 2026, while the site was being updated after the
    Nov 3, 2026 general election". The election clause depends on the roster's
    election_status: 'updating-after-election' (the site has started listing new members),
    'pre-election' ("a listing that does not reflect the Nov 3, 2026 general election"), or
    none for any other status.

    It never says the results hadn't "taken effect": under Article III, section 4 of the Hawaiʻi
    Constitution a legislator's term begins on general election day."""
    if meta is None:
        roster = load_legislators()
        if roster is None:
            return None
        meta = roster["metadata"]
    note = f"as listed on capitol.hawaii.gov on {_date_words(meta['date_fetched'])}"
    if meta.get("election_status") == "updating-after-election":
        note += ", while the site was being updated after the Nov 3, 2026 general election"
    elif meta.get("election_status") == "pre-election":
        note += ", a listing that does not reflect the Nov 3, 2026 general election"
    return note


# Roster states in which a newly elected legislator may be missing from the site's listing.
MAY_MISS_NEW_MEMBERS = {"pre-election": "The roster may predate the election",
                        "updating-after-election": "The site was still being updated after the election"}

BOTH_LISTED_NOTE = ("The site lists two members for this district, one with a term-end date that had "
                    "not passed when the roster was fetched, and doesn't say which of them holds the "
                    "seat. Name both, with the status text exactly as written (see listing_statement). "
                    "Don't pick one or say who currently represents the district.")


def listing_statement(rows: list[dict]) -> str:
    """Both members the site lists for a district, with its own status text:
    'capitol.hawaii.gov lists Joe Gedeon (Term of Office Ended: 11/16/2026) and James Wataru'."""
    parts = [m["name_first_last"] + (f" ({'; '.join(m['status_as_written'])})" if m["status_as_written"] else "")
             for m in rows]
    return "capitol.hawaii.gov lists " + " and ".join(parts)


def _roster_context(meta: dict, chamber: str) -> dict:
    return {
        "as_listed_note": as_listed_note(meta),
        "roster_date_fetched": meta["date_fetched"],
        "membership_as_stated_by_site": meta["membership_as_stated_by_site"][chamber],
        "election_status": meta.get("election_status"),
        "election_note": meta.get("election_note"),
    }


def _view(m: dict, meta: dict) -> dict:
    return {
        "name_as_written": m["name_as_written"],
        "name_first_last": m["name_first_last"],
        "chamber": m["chamber"],
        "district": m["district"],
        "serving": m["serving"],
        "both_listed": bool(m.get("both_listed")),
        "status_as_written": m["status_as_written"],
        "profile_url": m["profile_url"],
        "source_url": m["source_url"],
        "date_fetched": m["date_fetched"],
        "citation": _citation(meta, m["source_url"]),
    }


def find_district_by_legislator(name: str) -> dict:
    """The chamber and district a legislator represents, matched by name.

    Every word of the query must be a whole word of the legislator's name, ignoring case,
    ʻokina, kahakō and punctuation, in any order ("Chang", "Stanley Chang", "chang, stanley").
    One match returns it; several return the candidates; none returns an error."""
    roster = load_legislators()
    if roster is None:
        return {"error": "The legislator roster is not available."}
    if not isinstance(name, str) or not _tokens(name):
        return {"error": "name must be a legislator's name, e.g. a surname or first and last name."}
    meta = roster["metadata"]
    q = _tokens(name)
    hits = [m for m in roster["legislators"] if all(t in _tokens(m["name_as_written"]) for t in q)]
    if not hits:
        why = MAY_MISS_NEW_MEMBERS.get(meta.get("election_status"))
        predates = f"{why}, so a newly elected legislator may not be listed yet. " if why else ""
        return {
            "query": name,
            "error": (f"No legislator in the roster matches {name!r}. The roster is "
                      f"{as_listed_note(meta)}. {predates}Names are matched on whole words only "
                      "(ignoring case, ʻokina and kahakō); misspellings and partial names are not guessed."),
            # True whenever the site's listing may not yet include a newly elected member.
            "roster_may_predate_election": why is not None,
            "as_listed_note": as_listed_note(meta),
            "roster_date_fetched": meta["date_fetched"],
            "source_urls": meta["source_urls"],
        }
    # Several partial matches, but the query is exactly one legislator's full name: that one.
    if len(hits) > 1:
        exact = [m for m in hits if sorted(q) == sorted(_tokens(m["name_as_written"]))
                 or q == _tokens(m["name_first_last"])]
        if len(exact) == 1:
            hits = exact
    if len(hits) > 1:
        return {
            "query": name,
            "ambiguous": True,
            "message": (f"{len(hits)} legislators match {name!r}. Ask the user which one they mean; "
                        "do not pick one."),
            "candidates": [_view(m, meta) for m in hits],
            "as_listed_note": as_listed_note(meta),
        }
    m = hits[0]
    out = {"query": name, "match": _view(m, meta), **_roster_context(meta, m["chamber"])}
    if m.get("both_listed"):
        current = district_legislators(m["chamber"], m["district"])
        out["both_listed"] = current["both_listed"]
        out["listing_statement"] = current["listing_statement"]
        out["note"] = BOTH_LISTED_NOTE
    elif not m["serving"]:
        current = district_legislators(m["chamber"], m["district"])
        out["note"] = ("The site lists this legislator with a term that has ended "
                       f"({'; '.join(m['status_as_written'])}). See current_member_of_district.")
        out["current_member_of_district"] = current.get("serving", [])
    out["citation"] = _citation(meta, m["source_url"])
    return out


def district_legislators(chamber: str, district: int) -> dict:
    """Who the roster lists for one district: the serving member, plus anyone listed for the
    same district whose term ended. When the site lists two members and the end date printed
    for one hasn't passed, `serving` is empty and both are in `both_listed` with a
    `listing_statement`: neither is picked."""
    roster = load_legislators()
    if roster is None:
        return {}
    meta = roster["metadata"]
    rows = [m for m in roster["legislators"] if m["chamber"] == chamber and m["district"] == district]
    if not rows:
        return {}
    both = [m for m in rows if m.get("both_listed")]
    out = {
        "serving": [_view(m, meta) for m in rows if m["serving"] and not m.get("both_listed")],
        "term_ended": [_view(m, meta) for m in rows if not m["serving"]],
        **_roster_context(meta, chamber),
        "note": "Names are as written on capitol.hawaii.gov. Party affiliation is not recorded.",
        "citation": _citation(meta, rows[0]["source_url"]),
    }
    if both:
        out["both_listed"] = [_view(m, meta) for m in both]
        out["listing_statement"] = listing_statement(both)
        out["both_listed_note"] = BOTH_LISTED_NOTE
    return out
