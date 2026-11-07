#!/usr/bin/env python3
"""Verify data/programs.json against the live official pages.

For every program, fetches each source page and checks that every quoted passage
(serves, agency, eligibility factors, income tables, application, time-sensitive notes)
appears on that page. Matching ignores only whitespace, curly-vs-straight quotes and
dash style. If every quote passes, stamps date_verified (today) on each program and
in metadata; otherwise prints the failures, leaves the file unchanged, and exits 1.

Usage:
    python scripts/verify_programs.py            # verify and stamp
    python scripts/verify_programs.py --check    # verify only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_PATH = REPO_ROOT / "data" / "programs.json"
UA = {"User-Agent": "HAIC-district-profiles/1.0 (program source verification)"}


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}
    BLOCK = {"p", "li", "br", "div", "h1", "h2", "h3", "h4", "h5", "tr", "td", "th", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def normalize(s: str) -> str:
    s = (s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
          .replace("–", "-").replace("—", "-").replace(" ", " ").replace("​", ""))
    return re.sub(r"\s+", " ", s).strip()


def page_text(url: str, session: requests.Session) -> str:
    r = session.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    p = _Text()
    p.feed(r.text)
    return normalize("".join(p.parts))


def quotes_by_url(prog: dict) -> dict[str, list[str]]:
    src = prog["source_url"]
    out: dict[str, list[str]] = {}

    def add(url, q):
        if q:
            out.setdefault(url, []).append(q)

    add(prog.get("agency_quote_source_url", src), prog.get("agency_quote"))
    add(src, prog["serves"]["quote"])
    for f in prog.get("eligibility_factors", []):
        add(src, f["quote"])
    for t in (prog.get("income_standards") or {}).get("tables", []):
        add(src, t["quote"])
    app = prog["application"]
    for q in app.get("quotes", []):
        add(app.get("quote_source_url", src), q)
    for n in prog.get("time_sensitive", []):
        add(n.get("source_url", src), n["quote"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify only; don't stamp dates")
    args = ap.parse_args()

    data = json.loads(PROGRAMS_PATH.read_text(encoding="utf-8"))
    allowed = set(data["metadata"]["allowed_source_domains"])
    failures: list[str] = []
    cache: dict[str, str] = {}
    with requests.Session() as s:
        for prog in data["programs"]:
            for url, quotes in quotes_by_url(prog).items():
                host = urlparse(url).hostname or ""
                if host.removeprefix("www.") not in allowed:
                    failures.append(f"{prog['id']}: source {url} is not an allowed official domain")
                    continue
                if url not in cache:
                    try:
                        cache[url] = page_text(url, s)
                    except requests.RequestException as exc:
                        failures.append(f"{prog['id']}: could not fetch {url} ({type(exc).__name__})")
                        cache[url] = ""
                        continue
                for q in quotes:
                    if normalize(q) not in cache[url]:
                        failures.append(f"{prog['id']}: quote not found on {url}: {q[:90]!r}")
            print(f"  checked {prog['id']}")

    if failures:
        print(f"\n{len(failures)} problem(s):", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1
    n_quotes = sum(len(q) for p in data["programs"] for q in quotes_by_url(p).values())
    print(f"All {n_quotes} quotes verified across {len(cache)} pages.")
    if not args.check:
        today = date.today().isoformat()
        data["metadata"]["date_verified"] = today
        for p in data["programs"]:
            p["date_verified"] = today
        PROGRAMS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Stamped date_verified = {today} in {PROGRAMS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
