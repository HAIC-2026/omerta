#!/usr/bin/env python3
"""Add the rent-burden band measures (ACS Table B25070) to data/districts.json.

Pulls only Table B25070 (every band, estimates and MOEs) for all State House and State
Senate districts, saves the raw rows to data/acs_b25070_raw.json, and adds the four band
measures defined in scripts/pull_acs.py to each district. No other measure is touched.

Usage:
    python scripts/add_rent_bands.py            # one live pull of B25070 (needs CENSUS_API_KEY)
    python scripts/add_rent_bands.py --offline  # rebuild from data/acs_b25070_raw.json, no network

Before anything is written, it fails loudly unless:
  - the pull has exactly the districts already in data/districts.json, with the same names;
  - the existing 30%+ measure, recomputed from the new pull, equals the stored one exactly
    (so the bands and the 30%+ total come from the same published figures);
  - in every district the four band shares add up to the 30%+ share (pull_acs.check_rent_bands).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pull_acs as pa  # noqa: E402

TABLE = "B25070"
BAND_LABELS = {
    "B25070_001": "Total",
    "B25070_002": "Less than 10.0 percent",
    "B25070_003": "10.0 to 14.9 percent",
    "B25070_004": "15.0 to 19.9 percent",
    "B25070_005": "20.0 to 24.9 percent",
    "B25070_006": "25.0 to 29.9 percent",
    "B25070_007": "30.0 to 34.9 percent",
    "B25070_008": "35.0 to 39.9 percent",
    "B25070_009": "40.0 to 49.9 percent",
    "B25070_010": "50.0 percent or more",
    "B25070_011": "Not computed",
}
BASE_VARS = list(BAND_LABELS)
RAW_PATH = pa.REPO_ROOT / "data" / "acs_b25070_raw.json"
COMPARE_FIELDS = ("value", "moe", "moe_to_estimate", "low_reliability",
                  "numerator_estimate", "denominator_estimate")


def pull_raw() -> dict:
    """One live pull: Table B25070, both chambers (the API takes one geography per request)."""
    pa.load_dotenv(pa.REPO_ROOT / ".env")
    api_key = os.getenv("CENSUS_API_KEY")
    if not api_key:
        raise pa.CensusError("CENSUS_API_KEY not set (add it to .env at the repo root).")
    raw = {
        "metadata": {
            "dataset": pa.DATASET_LABEL,
            "acs_year": pa.ACS_YEAR,
            "table_id": TABLE,
            "api_endpoint": pa.BASE_URL,
            "boundary_vintage": pa.BOUNDARY_VINTAGE,
            "state_fips": pa.STATE_FIPS,
            "variables": BAND_LABELS,
            "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_urls": {},
        },
        "rows": {},
    }
    with pa.requests.Session() as session:
        for chamber, geo in pa.CHAMBERS.items():
            print(f"Fetching {TABLE} for {chamber} districts ({geo}) ...")
            rows, urls = pa.fetch_chamber(session, api_key, geo, BASE_VARS)
            if len(urls) != 1:
                raise pa.CensusError(f"expected one request per chamber, made {len(urls)}")
            raw["metadata"]["source_urls"][chamber] = urls[0]
            raw["rows"][chamber] = {
                str(int(code)): {k: rec.get(k) for k in ["NAME"] + [b + s for b in BASE_VARS for s in "EM"]}
                for code, rec in sorted(rows.items()) if code.isdigit()
            }
    if api_key in json.dumps(raw):
        raise pa.CensusError("refusing to write: the API key appears in the pulled data")
    return raw


def band_metrics(raw: dict, record: dict) -> dict[str, dict]:
    """The four band measures for one district, built with the same code as every other rate."""
    chamber = record["chamber"]
    row = raw["rows"][chamber][str(record["district"])]
    labels = {b: f"Estimate!!Total:!!{name}" if b != "B25070_001" else "Estimate!!Total:"
              for b, name in BAND_LABELS.items()}
    url = raw["metadata"]["source_urls"][chamber]
    return {k: pa.build_metric(k, pa.METRICS[k], row, labels, url) for k in pa.RENT_BURDEN_BANDS}


def merge(data: dict, raw: dict) -> dict:
    """Return `data` with the band measures added after the 30%+ measure in every district."""
    meta = raw["metadata"]
    for field, want in (("dataset", data["metadata"]["dataset"]), ("acs_year", data["metadata"]["acs_year"]),
                        ("boundary_vintage", data["metadata"]["boundary_vintage"])):
        if meta[field] != want:
            raise pa.CensusError(f"pull {field} {meta[field]!r} does not match districts.json {want!r}")
    for chamber in pa.CHAMBERS:
        have = sorted(r["district"] for r in data["districts"] if r["chamber"] == chamber)
        got = sorted(int(d) for d in raw["rows"].get(chamber, {}))
        if have != got or len(got) != pa.EXPECTED_DISTRICTS[chamber]:
            raise pa.CensusError(f"{chamber}: pull has districts {got}, districts.json has {have}")

    problems = []
    for r in data["districts"]:
        row = raw["rows"][r["chamber"]][str(r["district"])]
        where = f"{r['chamber']} {r['district']}"
        if row["NAME"] != r["name"]:
            problems.append(f"{where}: name {row['NAME']!r} != {r['name']!r}")
        # The stored 30%+ measure must be reproduced exactly by the new pull.
        stored = r["metrics"][pa.RENT_BURDEN_TOTAL]
        again = pa.build_metric(pa.RENT_BURDEN_TOTAL, pa.METRICS[pa.RENT_BURDEN_TOTAL], row, {}, "")
        diff = [f for f in COMPARE_FIELDS if stored.get(f) != again.get(f)]
        if diff:
            problems.append(f"{where}: 30%+ measure differs from the stored one in {diff}")
        bands = band_metrics(raw, r)
        metrics = {}
        for k, v in r["metrics"].items():
            if k in bands:
                continue  # re-run: replace earlier band values
            metrics[k] = v
            if k == pa.RENT_BURDEN_TOTAL:
                metrics.update(bands)
        r["metrics"] = metrics
    if problems:
        raise pa.CensusError("B25070 pull does not match data/districts.json:\n  " + "\n  ".join(problems))
    pa.check_rent_bands(data["districts"])

    data["metadata"]["supplemental_pulls"] = [{
        "table_id": TABLE,
        "pulled_at": meta["pulled_at"],
        "raw_file": f"data/{RAW_PATH.name}",
        "measures_added": list(pa.RENT_BURDEN_BANDS),
        "note": "Only Table B25070 was pulled; every other measure is unchanged from the full pull.",
    }]
    return data


def main() -> int:
    offline = "--offline" in sys.argv[1:]
    try:
        if offline:
            raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        else:
            raw = pull_raw()
        data = merge(json.loads(pa.OUTPUT_PATH.read_text(encoding="utf-8")), raw)
    except pa.CensusError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not offline:
        RAW_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    pa.OUTPUT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    flagged = sum(1 for r in data["districts"] for k in pa.RENT_BURDEN_BANDS
                  if r["metrics"][k]["low_reliability"])
    print(f"Added {len(pa.RENT_BURDEN_BANDS)} band measures to {len(data['districts'])} districts "
          f"({flagged} band values flagged low_reliability). Band sums match the 30%+ share everywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
