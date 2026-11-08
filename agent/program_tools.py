"""Program-matching tools over data/programs.json.

Framing rule: these tools describe programs and point out which published requirements a
household's stated facts do or don't line up with. They never decide eligibility. Every
response says people "may qualify" and must confirm with the agency.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent import tools

PROGRAMS_PATH = Path(__file__).resolve().parent.parent / "data" / "programs.json"

FACT_KEYS = ("pregnant_or_postpartum", "child_under_5", "child_under_13", "has_children",
             "age_60_plus", "veteran")
ISLANDS = ("Oahu", "Hawaii", "Maui", "Kauai", "Molokai", "Lanai")
# A younger-child fact implies the broader ones.
IMPLIES = {"child_under_5": ("child_under_13", "has_children"), "child_under_13": ("has_children",)}

STATUS_MATCH = "may_qualify_confirm_with_agency"
STATUS_UNKNOWN = "check_requirement_with_agency"
STATUS_MISMATCH = "stated_facts_do_not_match_a_published_requirement"


@lru_cache(maxsize=None)
def load_programs(path: str | None = None) -> dict:
    p = Path(path) if path else PROGRAMS_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["index"] = {pr["id"]: pr for pr in raw["programs"]}
    raw["needs"] = sorted({n for pr in raw["programs"] for n in pr["needs"]})
    return raw


def _program_citation(pr: dict) -> str:
    return (f"{pr['agency']}, {pr['name']}, {pr['source_url']} "
            f"(verified {pr.get('date_verified') or 'date unknown'})")


def _program_view(pr: dict, household_size: int | None = None) -> dict:
    out = {
        "id": pr["id"],
        "name": pr["name"],
        "agency": pr["agency"],
        "serves": pr["serves"],
        "eligibility_factors": pr["eligibility_factors"],
        "application": {k: v for k, v in pr["application"].items() if k != "quotes"},
        "source_url": pr["source_url"],
        "date_verified": pr.get("date_verified"),
        "citation": _program_citation(pr),
    }
    if pr.get("time_sensitive"):
        out["time_sensitive"] = pr["time_sensitive"]
    inc = pr.get("income_standards")
    if inc:
        tables = []
        for t in inc["tables"]:
            entry = {"title": t["title"], "columns": t["columns"]}
            if household_size is not None:
                key = str(household_size)
                if key in t["rows"]:
                    entry["household_size"] = household_size
                    entry["values"] = t["rows"][key]
                else:
                    entry["note"] = ("The published table lists sizes "
                                     f"{min(t['rows'], key=int)}-{max(t['rows'], key=int)}; "
                                     "for larger households it adds a fixed amount per member.")
                    entry["each_additional_member"] = t["each_additional_member"]
                    entry["largest_listed"] = {max(t["rows"], key=int): t["rows"][max(t["rows"], key=int)]}
            else:
                entry["rows"] = t["rows"]
                entry["each_additional_member"] = t["each_additional_member"]
            tables.append(entry)
        out["income_standards"] = {"note": inc["note"], "tables": tables}
    return out


def _screen(pr: dict, facts: dict, island: str | None) -> dict:
    """Compare stated facts with the program's published requirements. Never a decision."""
    notes: list[str] = []
    status = STATUS_MATCH
    sc = pr.get("screening") or {}

    def factor(i):
        return pr["eligibility_factors"][i]["factor"]

    if "islands_only" in sc:
        allowed = sc["islands_only"]["islands"]
        if island is None:
            status = STATUS_UNKNOWN
            notes.append(f"Requirement to check: {factor(sc['islands_only']['factor_index'])}")
        elif island not in allowed:
            status = STATUS_MISMATCH
            notes.append(f"Published requirement: {factor(sc['islands_only']['factor_index'])} "
                         f"The household is on {island}.")
    if "min_age" in sc:
        v = facts.get("age_60_plus")
        if v is None:
            status = STATUS_UNKNOWN if status == STATUS_MATCH else status
            notes.append(f"Requirement to check: {factor(sc['min_age']['factor_index'])}")
        elif v is False:
            status = STATUS_MISMATCH
            notes.append(f"Published requirement: {factor(sc['min_age']['factor_index'])}")
    if "requires_any" in sc:
        keys = sc["requires_any"]["facts"]
        vals = [facts.get(k) for k in keys]
        if any(v is True for v in vals):
            pass
        elif all(v is False for v in vals):
            status = STATUS_MISMATCH
            notes.append(f"Published requirement: {factor(sc['requires_any']['factor_index'])}")
        else:
            status = STATUS_UNKNOWN if status == STATUS_MATCH else status
            notes.append(f"Requirement to check: {factor(sc['requires_any']['factor_index'])}")
    notes.append("Only the agency can determine eligibility. Confirm with the agency before relying on this.")
    return {"status": status, "notes": notes}


def _err(msg: str, **valid) -> dict:
    return {"error": msg, **valid}


def find_programs(need: str, household: dict | None = None) -> dict:
    """Programs for a need, with how stated household facts line up with published requirements.

    need: one of the program needs, or "any".
    household (all optional): household_size (int), island (one of ISLANDS), and booleans
    pregnant_or_postpartum, child_under_5, child_under_13, has_children, age_60_plus, veteran.
    """
    data = load_programs()
    valid_needs = data["needs"] + ["any"]
    if not isinstance(need, str) or need not in valid_needs:
        return _err(f"Unknown need {need!r}.", valid_needs=valid_needs)
    household = household or {}
    if not isinstance(household, dict):
        return _err("household must be an object of facts.", valid_facts=["household_size", "island", *FACT_KEYS])
    unknown = set(household) - {"household_size", "island", *FACT_KEYS}
    if unknown:
        return _err(f"Unknown household facts: {sorted(unknown)}.",
                    valid_facts=["household_size", "island", *FACT_KEYS])
    size = household.get("household_size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 20):
        return _err("household_size must be a whole number from 1 to 20.")
    island = household.get("island")
    if island is not None and island not in ISLANDS:
        return _err(f"Unknown island {island!r}.", valid_islands=list(ISLANDS))
    facts: dict[str, bool | None] = {}
    for k in FACT_KEYS:
        v = household.get(k)
        if v is not None and not isinstance(v, bool):
            return _err(f"{k} must be true or false.")
        facts[k] = v
    for k, implied in IMPLIES.items():
        if facts.get(k) is True:
            for j in implied:
                facts[j] = True

    matches, other = [], []
    for pr in data["programs"]:
        if need != "any" and need not in pr["needs"]:
            continue
        view = _program_view(pr, size)
        view["screening"] = _screen(pr, facts, island)
        (other if view["screening"]["status"] == STATUS_MISMATCH else matches).append(view)

    return {
        "need": need,
        "household_facts_used": {k: v for k, v in {"household_size": size, "island": island, **facts}.items()
                                 if v is not None},
        "framing": data["metadata"]["framing"],
        "programs": matches,
        "programs_with_a_requirement_the_stated_facts_do_not_match": other,
        "date_verified": data["metadata"].get("date_verified"),
        "citation": ("Program information quoted from official agency web pages "
                     f"(verified {data['metadata'].get('date_verified')}); see each program's citation."),
    }


def district_program_context(chamber: str, district: int) -> dict:
    """Pair a district's relevant ACS measures with the programs those measures relate to.

    District data show where needs may be concentrated. They say nothing about whether any
    particular person qualifies.
    """
    chamber, err = tools._check_chamber(chamber)
    if err:
        return err
    district, err = tools._check_district(chamber, district)
    if err:
        return err
    rec = tools.load_data()["index"][(chamber, district)]
    ctx = tools.load_context()
    island = None
    if ctx and (chamber, district) in ctx["index"]:
        isl = ctx["index"][(chamber, district)].get("boundary_description", {}).get("islands") or []
        island = next((i for i in isl if i in ISLANDS), None)

    medians: dict[str, Any] = {}
    data = load_programs()
    rows = []
    acs_tables: set[str] = set()
    for pr in data["programs"]:
        metrics = {}
        for key in pr["district_metrics"]:
            m = rec["metrics"].get(key)
            if not m:
                continue
            view = tools._metric_view(m)
            if key not in medians:
                sc = tools.statewide_context(key, chamber)["chambers"][chamber]
                medians[key] = sc.get("median_of_districts")
            med = medians[key]
            view["median_of_districts_in_chamber"] = med
            view["median_of_districts_display"] = tools._fmt(med, m["unit"])
            metrics[key] = view
            acs_tables.add(m["table_id"])
        item = {"program": {k: v for k, v in _program_view(pr).items()
                            if k in ("id", "name", "agency", "serves", "application", "source_url",
                                     "date_verified", "citation", "time_sensitive")},
                "related_district_measures": metrics}
        sc = pr.get("screening") or {}
        if "islands_only" in sc:
            allowed = sc["islands_only"]["islands"]
            fac = pr["eligibility_factors"][sc["islands_only"]["factor_index"]]
            if island and island not in allowed:
                item["geographic_note"] = (f"Published requirement: {fac['factor']} This district is on "
                                           f"{island}, per the 2021 Reapportionment Commission's boundary "
                                           "description.")
            elif island is None:
                item["geographic_note"] = f"Published requirement: {fac['factor']}"
        rows.append(item)

    return {
        "chamber": chamber,
        "district": district,
        "name": rec["name"],
        "island_as_written": island,
        "pairs": rows,
        "note": ("District measures show where needs may be concentrated. They do not show whether any "
                 "household qualifies; only the agency can decide. The median is the median of district "
                 "estimates in this chamber, not a statewide ACS estimate."),
        "framing": data["metadata"]["framing"],
        "citation": (f"{tools._citation(sorted(acs_tables))}; program information quoted from official "
                     f"agency web pages (verified {data['metadata'].get('date_verified')})"),
    }


def list_available_metrics() -> dict:
    """Every measure the tools can report (id, full label, ACS table) and every assistance
    program they cover, so descriptions of what data is available come from the data files
    rather than from the model's own wording."""
    data = tools.load_data()
    seen: dict[str, dict] = {}
    for r in data["districts"]:
        for mid, m in r["metrics"].items():
            seen.setdefault(mid, {"id": mid, "label": m["label"], "table_id": m["table_id"]})
    progs = load_programs()["programs"]
    meta = data["metadata"]
    return {
        "metrics": [seen[k] for k in sorted(seen)],
        "programs": [{"id": p["id"], "name": p["name"], "agency": p["agency"]} for p in progs],
        "dataset": meta["dataset"],
        "boundary_vintage": meta["boundary_vintage"],
        "note": ("Use each metric's label and each program's name exactly as written here. Do not "
                 "shorten or paraphrase them."),
        "citation": (f"{tools._citation([m['table_id'] for m in seen.values()])}; program information "
                     "quoted from official agency web pages"),
    }
