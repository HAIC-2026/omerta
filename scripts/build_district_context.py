#!/usr/bin/env python3
"""Build data/district_context.json from the State of Hawaiʻi 2021 Reapportionment
Commission Final Report and Reapportionment Plan (Office of Elections).

For each of the 76 legislative districts it records, with document page citations:
  - the areas/communities the district covers, verbatim from Appendix D
    ("District Boundary Descriptions"), and
  - the permanent-resident (reapportionment) population used to draw the district,
    from the report's district tables (Tables 1-10),
plus the report's own statement of who was excluded from that population base.

Nothing numeric is typed in here: every figure is parsed from the PDF text and
cross-checked (island-unit tables vs statewide tables, district sums vs the stated
state total, census total minus extraction vs the adjusted total). Any district or
figure that fails a check is left out rather than guessed.

Usage:
    python scripts/build_district_context.py [--pdf PATH]

Without --pdf the report (~400 MB) is downloaded to a temporary directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

import requests
from pypdf import PdfReader

REPORT_URL = ("https://elections.hawaii.gov/wp-content/uploads/"
              "2021-Reapportionment-Commission-Final-Report-952-pages-February-25-2022.pdf")
SOURCE_PAGE_URL = "https://elections.hawaii.gov/about-us/boards-and-commissions/reapportionment/"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "district_context.json"

CHAMBER_WORDS = {"house": ("House", "Representative"), "senate": ("Senate", "Senatorial")}
ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)"


def norm(s: str) -> str:
    """Collapse whitespace; the PDF's undecodable apostrophes become ’."""
    return re.sub(r"\s+", " ", s.replace("�", "’")).strip()


def num(s: str) -> int:
    return int(s.replace(",", ""))


class Report:
    def __init__(self, path: Path):
        self.reader = PdfReader(str(path))
        self._cache: dict[int, str] = {}

    def text(self, pdf_page: int) -> str:
        if pdf_page not in self._cache:
            self._cache[pdf_page] = self.reader.pages[pdf_page - 1].extract_text() or ""
        return self._cache[pdf_page]

    def outline_pages(self) -> dict[str, int]:
        out: dict[str, int] = {}

        def walk(items):
            for it in items:
                if isinstance(it, list):
                    walk(it)
                    continue
                try:
                    out.setdefault(it.title.strip(), self.reader.get_destination_page_number(it) + 1)
                except Exception:
                    pass
        walk(self.reader.outline)
        return out

    def printed_label(self, pdf_page: int) -> str | None:
        """The page number printed on the page: 'D-030' in Appendix D, '15' or 'ii' in the body."""
        t = self.text(pdf_page)
        m = re.search(r"\b(D-\d{3})\b", t)
        if m:
            return m.group(1)
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        for ln in lines[:4]:
            if re.fullmatch(r"\d{1,3}|[ivxlc]{1,6}", ln):
                return ln
        return None


def find_pages(rep: Report, pattern: str, pages) -> list[int]:
    rx = re.compile(pattern, re.S)
    return [p for p in pages if rx.search(norm(rep.text(p)))]


# ---------------------------------------------------------------------------
# Population tables
# ---------------------------------------------------------------------------

TABLE_RE = re.compile(r"Table (\d+) - (.*?) Targets and Deviations(?:\s*\((House|Senate) Districts\))?")
ROW_RE = re.compile(r"^\s*(\d{1,2})\s+([\d,]{4,})\s+([\d,]{4,})\s+(-?\d+)\s+(-?\d+\.\d+)%", re.M)


def parse_tables(rep: Report, pages: list[int]) -> dict[int, dict]:
    """Parse 'Table N - <scope> Targets and Deviations (<chamber> Districts)' tables."""
    tables: dict[int, dict] = {}
    current = None
    for p in pages:
        t = rep.text(p)
        pos = 0
        heads = list(TABLE_RE.finditer(t))
        segments = []
        for i, h in enumerate(heads):
            if h.start() > pos:
                segments.append((current, t[pos:h.start()]))
            current = int(h.group(1))
            title = norm(h.group(2))
            word = h.group(3) or ("House" if title.startswith("House") else
                                  "Senate" if title.startswith("Senate") else None)
            chamber = {"House": "house", "Senate": "senate"}.get(word)
            unit = next((u for u in ("Oahu", "Hawaii", "Maui", "Kauai") if title.startswith(u)), None)
            tables[current] = {"number": current, "title": norm(h.group(0)), "chamber": chamber,
                               "island_unit": unit, "statewide": title.startswith(("House Statewide",
                               "Senate Statewide")), "rows": {}, "pages": []}
            pos = h.end()
        segments.append((current, t[pos:]))
        for tbl, seg in segments:
            if tbl is None:
                continue
            for r in ROW_RE.finditer(seg):
                d = int(r.group(1))
                tables[tbl]["rows"][d] = {
                    "target": num(r.group(2)), "population": num(r.group(3)),
                    "deviation": int(r.group(4)), "deviation_pct": float(r.group(5)), "pdf_page": p}
                if p not in tables[tbl]["pages"]:
                    tables[tbl]["pages"].append(p)
    return {n: t for n, t in tables.items() if t["chamber"] is not None}


# ---------------------------------------------------------------------------
# District descriptions (Appendix D)
# ---------------------------------------------------------------------------

def parse_descriptions(rep: Report, start: int, end: int) -> dict[tuple[str, int], dict]:
    chunks, offsets = [], []
    pos = 0
    for p in range(start, end + 1):
        t = norm(rep.text(p))
        offsets.append((pos, p))
        chunks.append(t)
        pos += len(t) + 1
    full = " ".join(chunks)

    def page_at(i: int) -> int:
        page = offsets[0][1]
        for off, p in offsets:
            if off <= i:
                page = p
        return page

    out = {}
    # One district's opening sentence: "The 46th Representative District comprises|includes ...
    # more particularly described". The body may not run into the next district's heading.
    head = rf"The {ORDINAL} (Representative|Senatorial) District (?=comprises|includes)"
    next_head = r"\d{1,2}(?:st|nd|rd|th) (?:Representative|Senatorial) District"
    rx = re.compile(head + rf"((?:(?!The {next_head} ).)*?)"
                    rf"(?:[,;]?\s*more particularly described|\.(?=\s*{next_head}|\s*$))")
    for m in rx.finditer(full):
        d, kind, body = int(m.group(1)), m.group(2), norm(m.group(3))
        chamber = "house" if kind == "Representative" else "senate"
        if (chamber, d) in out:
            continue
        sentence = norm(full[m.start():m.start(3)] + body)
        islands = re.findall(r"(?:portion of )?the island of (\w+)", body)
        many = re.search(r"the islands of (.+?)(?:;| and includes| and comprises| including|$)", body)
        if many:
            # Keep plain island names only; complex lists stay in the verbatim description.
            islands += [i for i in re.split(r",\s*(?:and\s+)?|\s+and\s+", many.group(1))
                        if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+)*", i) and i not in islands]
        # Community list: every "including ..." clause, up to ";" or "and includes".
        areas: list[str] = []
        for clause in re.findall(r"including (.+?)(?:;| and includes |$)", body):
            for a in re.split(r",\s*(?:and\s+)?|\s+and\s+", clause):
                a = a.strip().rstrip(".")
                if a and a not in areas:
                    areas.append(a)
        rep_d = re.search(r"includes (?:the )?((?:\d{1,2}(?:st|nd|rd|th),? (?:and )?)+)Representative Districts?", body)
        page = page_at(m.start())
        out[(chamber, d)] = {
            "description": sentence,
            "islands": islands or None,
            "areas": areas or None,
            # As listed in the Senate district's description; a House district may be
            # listed under more than one Senate district.
            "representative_districts_listed": (
                [int(x) for x in re.findall(r"(\d{1,2})(?:st|nd|rd|th)", rep_d.group(1))] if rep_d else None),
            "pdf_page": page,
            "page_label": rep.printed_label(page),
        }
    return out


# ---------------------------------------------------------------------------
# Population basis statements
# ---------------------------------------------------------------------------

def sentence_with(text: str, start_pattern: str, end_pattern: str) -> str | None:
    m = re.search(start_pattern + r".*?" + end_pattern, norm(text), re.S)
    return m.group(0) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, help="local copy of the final report PDF")
    args = ap.parse_args()

    pdf = args.pdf
    if pdf is None:
        tmp = Path(tempfile.mkdtemp())
        pdf = tmp / "final_report.pdf"
        print(f"Downloading {REPORT_URL} ...")
        with requests.get(REPORT_URL, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(pdf, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    rep = Report(pdf)
    n_pages = len(rep.reader.pages)
    outline = rep.outline_pages()
    problems: list[str] = []

    # --- report identity (title page) --------------------------------------------
    title = norm(rep.text(1))
    if "2021 REAPPORTIONMENT COMMISSION" not in title or "FINAL REPORT" not in title:
        print("ERROR: page 1 is not the 2021 Reapportionment Commission final report", file=sys.stderr)
        return 1

    # --- population basis --------------------------------------------------------
    body_pages = range(1, outline.get("Appendix A Cover Sheet", 40))
    basis_pages = find_pages(rep, r"extracted [\d,]+ non- ?permanent residents", body_pages)
    method_pages = find_pages(rep, r"non-permanent military and student residents were extracted", body_pages)
    if not basis_pages or not method_pages:
        print("ERROR: could not find the population-base statements", file=sys.stderr)
        return 1
    bp, mp = basis_pages[0], method_pages[0]
    base_stmt = sentence_with(rep.text(bp), r"The Commission approved on", r"island units\.")
    method_stmt = sentence_with(rep.text(mp), r"Briefly, non-permanent military", r"resided\.")
    m = re.search(r"extracted ([\d,]+) non- ?permanent residents from the U\.S\. Census population of "
                  r"Hawaii, resulting in an adjusted population of ([\d,]+)", norm(rep.text(bp)))
    c = re.search(r"unadjusted Census population of ([\d,]+)", norm(rep.text(bp)))
    extracted, adjusted = num(m.group(1)), num(m.group(2))
    census_total = num(c.group(1)) if c else None
    if census_total is not None and census_total - extracted != adjusted:
        problems.append("census total minus extraction does not equal the adjusted total")

    # --- tables ------------------------------------------------------------------
    titled = find_pages(rep, r"Targets and Deviations", body_pages)
    # Tables continue onto following pages without repeating their title.
    table_pages = list(range(min(titled), max(titled) + 2))
    tables = parse_tables(rep, table_pages)
    biu = {(t["chamber"], d): (t, r) for t in tables.values() if not t["statewide"]
           for d, r in t["rows"].items()}
    sw = {(t["chamber"], d): (t, r) for t in tables.values() if t["statewide"]
          for d, r in t["rows"].items()}

    # --- descriptions ------------------------------------------------------------
    d_start = outline.get("1st Representative District")
    d_end = outline.get("Congressional Reapportionment Plan")
    if not d_start or not d_end:
        print("ERROR: Appendix D bookmarks not found", file=sys.stderr)
        return 1
    descs = parse_descriptions(rep, d_start, d_end)

    # --- per-district records with cross-checks ------------------------------------
    districts = []
    for chamber in ("house", "senate"):
        nums = sorted({d for (c_, d) in list(biu) + list(sw) + list(descs) if c_ == chamber})
        for d in nums:
            rec: dict = {"chamber": chamber, "district": d}
            b, s = biu.get((chamber, d)), sw.get((chamber, d))
            if b and s and b[1]["population"] == s[1]["population"] \
                    and b[1]["population"] - b[1]["target"] == b[1]["deviation"] \
                    and s[1]["population"] - s[1]["target"] == s[1]["deviation"]:
                bt, br = b
                st, sr = s
                rec["reapportionment"] = {
                    "permanent_resident_population": sr["population"],
                    "statewide_target": sr["target"],
                    "statewide_deviation": sr["deviation"],
                    "statewide_deviation_pct": sr["deviation_pct"],
                    "island_unit": bt["island_unit"],
                    "island_unit_target": br["target"],
                    "island_unit_deviation_pct": br["deviation_pct"],
                    "sources": [
                        {"table": st["title"], "pdf_page": sr["pdf_page"],
                         "page_label": rep.printed_label(sr["pdf_page"])},
                        {"table": bt["title"], "pdf_page": br["pdf_page"],
                         "page_label": rep.printed_label(br["pdf_page"])},
                    ],
                }
            else:
                problems.append(f"{chamber} {d}: population not verified (tables disagree or missing)")
            if (chamber, d) in descs:
                rec["boundary_description"] = descs[(chamber, d)]
            else:
                problems.append(f"{chamber} {d}: boundary description not found")
            districts.append(rec)

    for chamber in ("house", "senate"):
        pops = [r["reapportionment"]["permanent_resident_population"]
                for r in districts if r["chamber"] == chamber and "reapportionment" in r]
        if sum(pops) != adjusted:
            problems.append(f"{chamber}: district populations sum to {sum(pops)}, report total is {adjusted}")

    output = {
        "source": {
            "title": "State of Hawaii 2021 Reapportionment Commission Final Report and Reapportionment Plan",
            "publisher": "State of Hawaiʻi 2021 Reapportionment Commission (published by the Office of Elections)",
            "submitted_to": "Thirty-First Legislature, Regular Session 2022",
            "url": REPORT_URL,
            "listing_url": SOURCE_PAGE_URL,
            "pdf_pages": n_pages,
            "date_in_filename": "February 25, 2022",
            "note": "Page citations give the printed page label and the page number in the PDF file. "
                    "Place names are quoted as written in the report (without ʻokina or kahakō).",
        },
        "population_basis": {
            "name": "Permanent resident population (reapportionment population base)",
            "census_population": census_total,
            "non_permanent_residents_extracted": extracted,
            "permanent_resident_population": adjusted,
            "statement": base_stmt,
            "statement_source": {"pdf_page": bp, "page_label": rep.printed_label(bp)},
            "exclusion_statement": method_stmt,
            "exclusion_source": {"pdf_page": mp, "page_label": rep.printed_label(mp)},
        },
        "districts": districts,
        "verification": {
            "checks": [
                "island-unit table population equals statewide table population for each district",
                "population minus target equals the stated deviation in both tables",
                "district populations sum to the stated permanent resident total for each chamber",
                "census population minus non-permanent residents extracted equals the adjusted total",
            ],
            "unverified": problems,
        },
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    ok_pop = sum(1 for r in districts if "reapportionment" in r)
    ok_desc = sum(1 for r in districts if "boundary_description" in r)
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}: {len(districts)} districts, "
          f"{ok_pop} with verified population, {ok_desc} with boundary description.")
    for p in problems:
        print("  UNVERIFIED:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
