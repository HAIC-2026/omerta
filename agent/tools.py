"""Tool layer for the district-profile agent.

Pure, read-only functions over data/districts.json. The LLM never produces
numbers itself: every figure it narrates must come from one of these returns.

Conventions for every tool:
- Success returns a dict with a top-level ``citation`` string.
- Each metric value that is flagged low_reliability carries a ``caveat`` string.
- Invalid input returns ``{"error": "<message>", ...}`` with the valid options;
  inputs are never fuzzy-matched or guessed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "districts.json"

CHAMBERS = ("house", "senate")
ORDERS = ("desc", "asc")
PUBLISHER = "U.S. Census Bureau"


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def load_data(path: str | None = None) -> dict:
    """Load and index the districts file. Cached; treat the result as read-only."""
    p = Path(path) if path else DATA_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    index = {(r["chamber"], r["district"]): r for r in raw["districts"]}
    metrics = sorted({m for r in raw["districts"] for m in r["metrics"]})
    return {"metadata": raw["metadata"], "districts": raw["districts"],
            "index": index, "metrics": metrics}


def _districts_in(chamber: str) -> list[int]:
    return sorted(d for (c, d) in load_data()["index"] if c == chamber)


# ---------------------------------------------------------------------------
# Validation (strict: no guessing)
# ---------------------------------------------------------------------------

def _error(message: str, **valid: Any) -> dict:
    return {"error": message, **valid}


def _check_chamber(chamber: Any) -> tuple[str | None, dict | None]:
    if isinstance(chamber, str) and chamber.strip().lower() in CHAMBERS:
        return chamber.strip().lower(), None
    return None, _error(f"Invalid chamber {chamber!r}. Use 'house' or 'senate'.",
                        valid_chambers=list(CHAMBERS))


def _check_district(chamber: str, district: Any) -> tuple[int | None, dict | None]:
    valid = _districts_in(chamber)
    num = None
    if isinstance(district, int) and not isinstance(district, bool):
        num = district
    elif isinstance(district, str) and district.strip().isdigit():
        num = int(district.strip())
    if num is None or num not in valid:
        return None, _error(
            f"Invalid {chamber} district {district!r}. Hawaii {chamber} districts are "
            f"{valid[0]}-{valid[-1]}.",
            valid_districts=valid,
        )
    return num, None


def _check_metric(metric: Any) -> tuple[str | None, dict | None]:
    metrics = load_data()["metrics"]
    if isinstance(metric, str) and metric in metrics:
        return metric, None
    return None, _error(f"Unknown metric {metric!r}.", valid_metrics=metrics)


# ---------------------------------------------------------------------------
# Citation / caveat helpers
# ---------------------------------------------------------------------------

def _boundary_phrase(vintage: str) -> str:
    # "2022 districts" -> "2022 district boundaries"
    year = vintage.split()[0] if vintage else ""
    return f"{year} district boundaries" if year.isdigit() else vintage


def _citation(tables: list[str]) -> str:
    meta = load_data()["metadata"]
    tables = sorted(set(tables))
    label = "Table" if len(tables) == 1 else "Tables"
    return (f"{PUBLISHER}, {meta['dataset']}, {label} {', '.join(tables)}; "
            f"{_boundary_phrase(meta['boundary_vintage'])}")


def _year(dataset: str) -> str | None:
    for tok in dataset.split():
        if tok.isdigit() and len(tok) == 4:
            return tok
    return None


def _caveat(m: dict) -> str | None:
    if not m.get("low_reliability"):
        return None
    rule = load_data()["metadata"].get("low_reliability_rule", "MOE / estimate above threshold")
    ratio = m.get("moe_to_estimate")
    detail = (f"the margin of error is {ratio:.0%} of the estimate"
              if isinstance(ratio, (int, float)) else "the estimate is zero or near zero")
    return (f"Low reliability: {detail} (rule: {rule}). Treat this figure as approximate "
            "and avoid drawing firm conclusions from small differences.")


def _bounds(value, moe) -> dict:
    """Confidence interval value ± MOE, so the narrating model never computes it.
    All metrics here are non-negative, so the lower bound is floored at 0 (Census guidance)."""
    if value is None or moe is None:
        return {"lower_bound": None, "upper_bound": None}
    return {"lower_bound": round(max(value - moe, 0), 2), "upper_bound": round(value + moe, 2)}


def _metric_view(m: dict) -> dict:
    """Public shape of one metric value."""
    out = {
        "label": m["label"],
        "unit": m["unit"],
        "value": m["value"],
        "moe": m["moe"],
        "moe_to_estimate": m.get("moe_to_estimate"),
        "moe_percent_of_estimate": (round(m["moe_to_estimate"] * 100, 1)
                                    if isinstance(m.get("moe_to_estimate"), (int, float)) else None),
        "moe_confidence_level": load_data()["metadata"].get("moe_confidence_level"),
        **_bounds(m["value"], m["moe"]),
        "low_reliability": m["low_reliability"],
        "table_id": m["table_id"],
        "dataset": m["dataset"],
        "year": _year(m["dataset"]),
        "boundary_vintage": m["boundary_vintage"],
        "source_url": m["source_url"],
        "citation": _citation([m["table_id"]]),
    }
    caveat = _caveat(m)
    if caveat:
        out["caveat"] = caveat
    if m.get("notes"):
        out["notes"] = m["notes"]
    return out


def _district_label(rec: dict) -> dict:
    return {"chamber": rec["chamber"], "district": rec["district"], "name": rec["name"]}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def get_district_profile(chamber: str, district: int) -> dict:
    """All metrics for one district, each with value, MOE, reliability and source."""
    chamber, err = _check_chamber(chamber)
    if err:
        return err
    district, err = _check_district(chamber, district)
    if err:
        return err
    rec = load_data()["index"][(chamber, district)]
    metrics = {k: _metric_view(v) for k, v in rec["metrics"].items()}
    return {
        **_district_label(rec),
        "metrics": metrics,
        "low_reliability_metrics": sorted(k for k, v in metrics.items() if v["low_reliability"]),
        "citation": _citation([v["table_id"] for v in metrics.values()]),
    }


def compare_districts(chamber: str, districts: list[int], metric: str) -> dict:
    """One metric side by side for several districts in the same chamber."""
    chamber, err = _check_chamber(chamber)
    if err:
        return err
    metric, err = _check_metric(metric)
    if err:
        return err
    if not isinstance(districts, (list, tuple)) or not districts:
        return _error("districts must be a non-empty list of district numbers.",
                      valid_districts=_districts_in(chamber))
    nums: list[int] = []
    for d in districts:
        num, err = _check_district(chamber, d)
        if err:
            return err
        if num not in nums:
            nums.append(num)

    index = load_data()["index"]
    rows = [{**_district_label(index[(chamber, n)]),
             **_metric_view(index[(chamber, n)]["metrics"][metric])} for n in nums]
    first = index[(chamber, nums[0])]["metrics"][metric]
    return {
        "chamber": chamber,
        "metric": metric,
        "label": first["label"],
        "unit": first["unit"],
        "districts": rows,
        "note": "Differences smaller than the margins of error may not be meaningful.",
        "citation": _citation([first["table_id"]]),
    }


def rank_districts(chamber: str, metric: str, top_n: int = 5, order: str = "desc") -> dict:
    """Top (order='desc') or bottom (order='asc') districts in a chamber for a metric."""
    chamber, err = _check_chamber(chamber)
    if err:
        return err
    metric, err = _check_metric(metric)
    if err:
        return err
    if not isinstance(order, str) or order.lower() not in ORDERS:
        return _error(f"Invalid order {order!r}. Use 'desc' (highest first) or 'asc' (lowest first).",
                      valid_orders=list(ORDERS))
    order = order.lower()
    total = len(_districts_in(chamber))
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= total:
        return _error(f"top_n must be an integer from 1 to {total} for the {chamber}.")

    index = load_data()["index"]
    recs = [index[(chamber, d)] for d in _districts_in(chamber)]
    with_value = [r for r in recs if r["metrics"][metric]["value"] is not None]
    without = [r["district"] for r in recs if r["metrics"][metric]["value"] is None]
    # Stable sort: ties keep district-number order.
    ranked = sorted(with_value, key=lambda r: r["metrics"][metric]["value"],
                    reverse=(order == "desc"))
    rows = [{"rank": i, **_district_label(r), **_metric_view(r["metrics"][metric])}
            for i, r in enumerate(ranked[:top_n], start=1)]
    sample = recs[0]["metrics"][metric]
    out = {
        "chamber": chamber,
        "metric": metric,
        "label": sample["label"],
        "unit": sample["unit"],
        "order": order,
        "districts_ranked": len(ranked),
        "results": rows,
        "note": "Rankings use point estimates; adjacent ranks with overlapping margins of "
                "error may not differ meaningfully.",
        "citation": _citation([sample["table_id"]]),
    }
    if without:
        out["excluded_no_estimate"] = without
    return out


def statewide_context(metric: str, chamber: str | None = None) -> dict:
    """Min, max and median of district values for a metric, per chamber.

    Pass chamber='house' or 'senate' for one chamber; omit for both.
    The median is the median of district estimates, not a statewide estimate.
    """
    metric, err = _check_metric(metric)
    if err:
        return err
    if chamber is None:
        chambers = list(CHAMBERS)
    else:
        c, err = _check_chamber(chamber)
        if err:
            return err
        chambers = [c]

    index = load_data()["index"]
    by_chamber = {}
    table_id = label = unit = None
    for c in chambers:
        recs = [index[(c, d)] for d in _districts_in(c)]
        vals = [(r, r["metrics"][metric]) for r in recs if r["metrics"][metric]["value"] is not None]
        table_id, label, unit = (recs[0]["metrics"][metric][k] for k in ("table_id", "label", "unit"))
        if not vals:
            by_chamber[c] = {"districts_with_estimate": 0}
            continue
        lo = min(vals, key=lambda x: x[1]["value"])
        hi = max(vals, key=lambda x: x[1]["value"])
        by_chamber[c] = {
            "districts_with_estimate": len(vals),
            "min": {**_district_label(lo[0]), **_metric_view(lo[1])},
            "max": {**_district_label(hi[0]), **_metric_view(hi[1])},
            "median_of_districts": median(v["value"] for _, v in vals),
            "low_reliability_count": sum(1 for _, v in vals if v["low_reliability"]),
        }
    return {
        "metric": metric,
        "label": label,
        "unit": unit,
        "chambers": by_chamber,
        "note": "median_of_districts is the median of district estimates, not a statewide "
                "ACS estimate, and has no published margin of error.",
        "citation": _citation([table_id]),
    }
