#!/usr/bin/env python3
"""Build data/legislators.json from the Hawaiʻi State Legislature website (capitol.hawaii.gov).

One source only: the official House and Senate member listings. For each member it records
the name as written on the site, chamber, district, official profile URL, the listing URL it
came from, and the fetch date. Party affiliation is deliberately not recorded: the site's
"(D)"/"(R)" suffix is removed from the name.

Status lines the site prints on a member's card ("Appointed 08/09/2026", "Term of Office
Ended: 06/30/2026") are kept verbatim. A member whose stated term-end date is before the
fetch date is marked serving=false; everyone else the site lists is serving=true.

The site's own statement of which membership it shows ("(Showing Senators for the 2026
regular session)") is stored verbatim. Every district must end up with exactly one serving
member, with one exception that is recorded rather than resolved: when the site lists two
members for a district, one with no end date and one whose printed "Term of Office Ended"
date is still in the future, both are kept with both_listed=true and neither is picked.
Anything else ambiguous makes the script fail rather than write a guess.

Usage:
    python scripts/fetch_legislators.py
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://www.capitol.hawaii.gov"
LISTINGS = {
    "house": f"{BASE}/legislature/legislators.aspx?chamber=H",
    "senate": f"{BASE}/legislature/legislators.aspx?chamber=S",
}
EXPECTED = {"house": 51, "senate": 25}
CHAMBER_WORD = {"house": "House", "senate": "Senate"}
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "legislators.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; hawaii-district-profiles data build)"}
TIMEOUT = 60

# A trailing party marker such as "(D)", "(R)", "(I)", "(G)", "(L)", "(N)" or "(NP)".
PARTY_SUFFIX = re.compile(r"\s*\((?:[A-Z]{1,3})\)\s*$")
STATUS_RE = re.compile(r"(Term of Office Ended:\s*\d{2}/\d{2}/\d{4}|Appointed\s+\d{2}/\d{2}/\d{4}"
                       r"|Resigned[^<]{0,40}|Vacant[^<]{0,40}|Deceased[^<]{0,40})")
ENDED_RE = re.compile(r"Term of Office Ended:\s*(\d{2})/(\d{2})/(\d{4})")


def text_of(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


SUFFIXES = {"Jr", "Sr", "II", "III", "IV"}


def name_first_last(name: str) -> str:
    """'Chang, Stanley' -> 'Stanley Chang'; 'Alcos III, David' -> 'David Alcos III'.
    A mechanical reordering of the site's 'Last, First' form."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 1:
        return name
    # "Richards, III, Herbert M. \"Tim\"" and "Ihara, Jr., Les": the middle part is a suffix.
    if len(parts) == 3 and parts[1].rstrip(".") in SUFFIXES:
        return f"{parts[2]} {parts[0]} {parts[1]}"
    return f"{', '.join(parts[1:])} {parts[0]}"


def parse_listing(chamber: str, page: str, source_url: str, fetched: date) -> tuple[list[dict], str]:
    shown = re.search(r"\(Showing [^)]*\)", page)
    if not shown:
        raise RuntimeError(f"{source_url}: no '(Showing ...)' statement found; page layout changed?")
    cards = page.split("legislator-list")[1:]
    if not cards:
        raise RuntimeError(f"{source_url}: no member cards found; page layout changed?")
    out = []
    for card in cards:
        link = re.search(r"href='(/legislature/memberpage\.aspx\?member=\d+&(?:amp;)?year=\d+)'", card)
        name = re.search(r'<h2 class="namecard"><strong>(.*?)</strong>', card, re.S)
        dist = re.search(r'<div class="district">(.*?)<br', card, re.S)
        if not (link and name and dist):
            raise RuntimeError(f"{source_url}: could not parse a member card")
        dist_text = text_of(dist.group(1))
        m = re.fullmatch(rf"{CHAMBER_WORD[chamber]} District (\d+)", dist_text)
        if not m:
            raise RuntimeError(f"{source_url}: unexpected district text {dist_text!r}")
        name_raw = text_of(name.group(1))
        name_as_written = PARTY_SUFFIX.sub("", name_raw)
        # The card ends where the next card's container begins.
        status = [s.strip() for s in STATUS_RE.findall(text_of(card))]
        ended = ENDED_RE.search(" ".join(status))
        end_date = date(int(ended.group(3)), int(ended.group(1)), int(ended.group(2))) if ended else None
        out.append({
            "name_as_written": name_as_written,
            "name_first_last": name_first_last(name_as_written),
            "chamber": chamber,
            "district": int(m.group(1)),
            "profile_url": BASE + html.unescape(link.group(1)),
            "status_as_written": status,
            "term_end_as_listed": end_date.isoformat() if end_date else None,
            "serving": not (end_date and end_date < fetched),
            "both_listed": False,
            "source_url": source_url,
            "date_fetched": fetched.isoformat(),
        })
    return out, shown.group(0)


GENERAL_ELECTION_2026 = date(2026, 11, 3)


def election_status(shown: dict[str, str], fetched: date, members: list[dict] = ()) -> dict:
    """Pre- or post-election, as the site shows it. The 2026 regular session ran before the
    November 3, 2026 general election, so a listing labeled for that session is pre-election
    membership, unless the site has started replacing members: it then prints term-end dates
    from the day before the election onward (a term begins on election day) next to newly
    listed members. That state is 'updating-after-election': the site doesn't say the update
    is complete, so the roster is not called post-election. Any other label is recorded as
    'unstated' for a person to review."""
    pre = all("2026 regular session" in s for s in shown.values())
    turnover = [m for m in members if m.get("term_end_as_listed")
                and date.fromisoformat(m["term_end_as_listed"]) >= GENERAL_ELECTION_2026 - timedelta(days=1)]
    if fetched >= GENERAL_ELECTION_2026 and pre and turnover:
        n = len({(m["chamber"], m["district"]) for m in turnover})
        return {"election_status": "updating-after-election",
                "election_note": "The 2026 general election was held on November 3, 2026. Both listings "
                                 "still say they show members for the 2026 regular session, but the site "
                                 f"now lists a new member in {n} district{'s' if n != 1 else ''} next to a "
                                 "member whose term it says ended on or after 11/02/2026. The site doesn't "
                                 "say the update is complete. This roster is the membership as the site "
                                 "lists it on the fetch date, not certified election results."}
    if fetched >= GENERAL_ELECTION_2026 and pre:
        return {"election_status": "pre-election",
                "election_note": "The 2026 general election was held on November 3, 2026. The site does "
                                 "not mention it: both listings say they show members for the 2026 regular "
                                 "session, which predates the election. This roster is the membership as the "
                                 "site lists it on the fetch date, not the election results."}
    return {"election_status": "unstated",
            "election_note": "The site's listing statements are recorded verbatim in "
                             "membership_as_stated_by_site; check them before describing this roster as "
                             "pre- or post-election."}


def check_districts(members: list[dict]) -> list[str]:
    """Check that every district resolves without a guess; return the problems (empty = fine).

    A district must have exactly one serving member. The one exception is recorded, not
    resolved: two serving members where exactly one carries a printed term-end date (which,
    since they count as serving, has not passed). Both get both_listed=true. Everything else
    (no serving member, two with no end date, two with end dates, three or more) is a problem."""
    problems = []
    for chamber, n in EXPECTED.items():
        for d in range(1, n + 1):
            serving = [m for m in members if m["chamber"] == chamber and m["district"] == d and m["serving"]]
            ending = [m for m in serving if m["term_end_as_listed"]]
            if len(serving) == 2 and len(ending) == 1:
                for m in serving:
                    m["both_listed"] = True
                continue
            if len(serving) != 1:
                problems.append(f"{chamber} {d}: {len(serving)} serving members "
                                f"({[m['name_as_written'] for m in serving]})")
        extra = {m["district"] for m in members if m["chamber"] == chamber} - set(range(1, n + 1))
        if extra:
            problems.append(f"{chamber}: unexpected districts {sorted(extra)}")
    return problems


def main() -> int:
    fetched = datetime.now(timezone.utc).date()
    members, shown = [], {}
    with requests.Session() as s:
        for chamber, url in LISTINGS.items():
            print(f"Fetching {url} ...")
            r = s.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            rows, shown[chamber] = parse_listing(chamber, r.text, url, fetched)
            members += rows
            print(f"  {len(rows)} members listed; site says {shown[chamber]!r}")

    problems = check_districts(members)
    if problems:
        for p in problems:
            print("ERROR:", p, file=sys.stderr)
        return 1

    # Within a district: serving first; of two both-listed members, the one with the end date first.
    members.sort(key=lambda m: (m["chamber"] != "house", m["district"], not m["serving"],
                                not m["term_end_as_listed"] if m["both_listed"] else False))
    output = {
        "metadata": {
            "source": "Hawaiʻi State Legislature website (capitol.hawaii.gov), House and Senate member listings",
            "source_urls": list(LISTINGS.values()),
            "date_fetched": fetched.isoformat(),
            "date_fetched_timezone": "UTC",
            "membership_as_stated_by_site": shown,
            **election_status(shown, fetched, members),
            "serving_rule": "serving=false only when the site prints 'Term of Office Ended: <date>' "
                            "with a date before the fetch date.",
            "both_listed_rule": "both_listed=true when the site lists two members for a district, one "
                                "with no end date and one whose printed 'Term of Office Ended' date is "
                                "after the fetch date. Both are kept; neither is picked as the member.",
            "both_listed_districts": [list(k) for k in sorted(
                {(m["chamber"], m["district"]) for m in members if m["both_listed"]},
                key=lambda k: (k[0] != "house", k[1]))],
            "fields_not_recorded": ["party affiliation"],
            "name_note": "name_as_written is exactly as on the site minus its party suffix; "
                         "name_first_last reorders 'Last, First' to 'First Last'.",
            "counts": {c: {"listed": sum(1 for m in members if m["chamber"] == c),
                           "serving": sum(1 for m in members if m["chamber"] == c and m["serving"]),
                           "districts_with_one_serving_member": len(
                               {m["district"] for m in members
                                if m["chamber"] == c and m["serving"] and not m["both_listed"]}),
                           "districts_with_two_listed": len(
                               {m["district"] for m in members if m["chamber"] == c and m["both_listed"]})}
                       for c in EXPECTED},
        },
        "legislators": members,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}: {output['metadata']['counts']}")
    for m in members:
        if m["status_as_written"]:
            print(f"  {m['chamber']} {m['district']}: {m['name_as_written']} {m['status_as_written']} "
                  f"serving={m['serving']} both_listed={m['both_listed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
