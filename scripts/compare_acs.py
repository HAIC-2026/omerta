#!/usr/bin/env python3
"""Compare two ACS district pulls, metric by metric.

Writes reports/acs_<old>_vs_<new>.json: for every district and metric, the old and new
value, MOE and low_reliability flag, plus counts of how many low_reliability flags changed.
Pure file comparison; no network.

Usage:
    python scripts/compare_acs.py [--old data/archive/districts_acs2023.json] [--new data/districts.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def year_of(meta: dict) -> str:
    if meta.get("acs_year"):
        return str(meta["acs_year"])
    # Older files carry the year only in the endpoint, e.g. .../data/2023/acs/acs5
    return meta["api_endpoint"].split("/data/")[1].split("/")[0]


def compare(old: dict, new: dict) -> dict:
    o_idx = {(r["chamber"], r["district"]): r for r in old["districts"]}
    n_idx = {(r["chamber"], r["district"]): r for r in new["districts"]}
    if set(o_idx) != set(n_idx):
        raise ValueError(f"district sets differ: only old {sorted(set(o_idx) - set(n_idx))}, "
                         f"only new {sorted(set(n_idx) - set(o_idx))}")
    rows, changes = [], {"became_low_reliability": 0, "no_longer_low_reliability": 0,
                         "other_flag_change": 0}
    by_metric: dict[str, dict] = {}
    for key in sorted(o_idx, key=lambda k: (k[0] != "house", k[1])):
        om, nm = o_idx[key]["metrics"], n_idx[key]["metrics"]
        if set(om) != set(nm):
            raise ValueError(f"{key}: metric sets differ")
        metrics = {}
        for m in sorted(om):
            a, b = om[m], nm[m]
            flag_changed = a["low_reliability"] != b["low_reliability"]
            entry = {
                "old_value": a["value"], "new_value": b["value"],
                "change": (round(b["value"] - a["value"], 2)
                           if a["value"] is not None and b["value"] is not None else None),
                "old_moe": a["moe"], "new_moe": b["moe"],
                "old_low_reliability": a["low_reliability"],
                "new_low_reliability": b["low_reliability"],
                "low_reliability_changed": flag_changed,
            }
            metrics[m] = entry
            s = by_metric.setdefault(m, {"flags_changed": 0, "became_low_reliability": 0,
                                         "no_longer_low_reliability": 0})
            if flag_changed:
                s["flags_changed"] += 1
                if b["low_reliability"] is True and not a["low_reliability"]:
                    kind = "became_low_reliability"
                elif a["low_reliability"] is True and not b["low_reliability"]:
                    kind = "no_longer_low_reliability"
                else:
                    kind = "other_flag_change"
                changes[kind] += 1
                if kind in s:
                    s[kind] += 1
        rows.append({"chamber": key[0], "district": key[1], "metrics": metrics})

    def flagged(d):
        return sum(1 for r in d["districts"] for v in r["metrics"].values() if v["low_reliability"])

    return {
        "old": {"dataset": old["metadata"]["dataset"], "boundary_vintage": old["metadata"]["boundary_vintage"],
                "low_reliability_values": flagged(old)},
        "new": {"dataset": new["metadata"]["dataset"], "boundary_vintage": new["metadata"]["boundary_vintage"],
                "low_reliability_values": flagged(new)},
        "summary": {
            "districts_compared": len(rows),
            "metric_values_compared": sum(len(r["metrics"]) for r in rows),
            "low_reliability_flags_changed": sum(changes.values()),
            **changes,
            "by_metric": by_metric,
        },
        "note": "Values are point estimates from overlapping 5-year periods (four of five years are "
                "shared), so year-to-year changes are small and mostly within the margins of error. "
                "Dollar figures are in each release's own inflation-adjusted dollars.",
        "districts": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, default=REPO_ROOT / "data" / "archive" / "districts_acs2023.json")
    ap.add_argument("--new", type=Path, default=REPO_ROOT / "data" / "districts.json")
    args = ap.parse_args()
    old, new = load(args.old), load(args.new)
    out = compare(old, new)
    path = REPO_ROOT / "reports" / f"acs_{year_of(old['metadata'])}_vs_{year_of(new['metadata'])}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    s = out["summary"]
    print(f"Wrote {path.relative_to(REPO_ROOT)}: {s['metric_values_compared']} values, "
          f"{s['low_reliability_flags_changed']} low_reliability flags changed "
          f"(+{s['became_low_reliability']} / -{s['no_longer_low_reliability']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
