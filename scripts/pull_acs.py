#!/usr/bin/env python3
"""Pull ACS 5-year socio-economic profiles for Hawaii State House and Senate districts.

Writes data/districts.json with one record per district. Every value comes from the
Census API at run time; nothing numeric is hardcoded except Census-defined codes
(FIPS state code, table/variable IDs, and the API's documented annotation sentinels).

Usage:
    python scripts/pull_acs.py

Requires CENSUS_API_KEY in a .env file at the repo root (or in the environment).
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

YEAR = "2023"
DATASET_PATH = f"{YEAR}/acs/acs5"
DATASET_LABEL = "ACS 2023 5-year"
BOUNDARY_VINTAGE = "2022 districts"
BASE_URL = f"https://api.census.gov/data/{DATASET_PATH}"
VARIABLES_URL = f"{BASE_URL}/variables.json"

STATE_FIPS = "15"  # Hawaii
CHAMBERS = {
    "house": "state legislative district (lower chamber)",
    "senate": "state legislative district (upper chamber)",
}

RELIABILITY_THRESHOLD = 0.30  # flag when MOE / estimate exceeds this
MAX_VARS_PER_CALL = 48  # API limit is 50 incl. NAME; leave headroom
TIMEOUT = 120

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "districts.json"

# Metric definitions. Each metric is one of:
#   "direct": the value is a single published estimate (count or median).
#   "rate":   100 * sum(numerator) / (sum(denominator_add) - sum(denominator_sub)).
# Variable IDs are given without the E/M suffix.
METRICS: dict[str, dict] = {
    "total_population": {
        "label": "Total population",
        "unit": "people",
        "table": "B01003",
        "kind": "direct",
        "var": "B01003_001",
    },
    "median_age": {
        "label": "Median age",
        "unit": "years",
        "table": "B01002",
        "kind": "direct",
        "var": "B01002_001",
    },
    "median_household_income": {
        "label": "Median household income (2023 inflation-adjusted dollars)",
        "unit": "dollars",
        "table": "B19013",
        "kind": "direct",
        "var": "B19013_001",
    },
    "poverty_rate": {
        "label": "Share of people with income below the poverty level "
        "(of population for whom poverty status is determined)",
        "unit": "percent",
        "table": "B17001",
        "kind": "rate",
        "numerator": ["B17001_002"],
        "denominator_add": ["B17001_001"],
        "denominator_sub": [],
    },
    "snap_households": {
        "label": "Households that received SNAP/food stamps in the past 12 months",
        "unit": "households",
        "table": "B22003",
        "kind": "direct",
        "var": "B22003_002",
    },
    "snap_household_rate": {
        "label": "Share of households that received SNAP/food stamps in the past 12 months",
        "unit": "percent",
        "table": "B22003",
        "kind": "rate",
        "numerator": ["B22003_002"],
        "denominator_add": ["B22003_001"],
        "denominator_sub": [],
    },
    "renter_cost_burden_rate": {
        "label": "Share of renter households paying 30%+ of income on gross rent "
        "(excludes households where the percentage is not computed)",
        "unit": "percent",
        "table": "B25070",
        "kind": "rate",
        # 30.0-34.9%, 35.0-39.9%, 40.0-49.9%, 50.0% or more
        "numerator": ["B25070_007", "B25070_008", "B25070_009", "B25070_010"],
        "denominator_add": ["B25070_001"],
        "denominator_sub": ["B25070_011"],  # Not computed
    },
    "veterans": {
        "label": "Civilian veterans (18 years and over)",
        "unit": "people",
        "table": "B21001",
        "kind": "direct",
        "var": "B21001_002",
    },
    "veteran_rate": {
        "label": "Share of civilian population 18+ who are veterans",
        "unit": "percent",
        "table": "B21001",
        "kind": "rate",
        "numerator": ["B21001_002"],
        "denominator_add": ["B21001_001"],
        "denominator_sub": [],
    },
    "armed_forces": {
        "label": "People in the Armed Forces (population 16 years and over)",
        "unit": "people",
        "table": "B23025",
        "kind": "direct",
        "var": "B23025_006",  # In labor force: Armed Forces
    },
    "armed_forces_rate": {
        "label": "Share of the population 16 years and over in the Armed Forces",
        "unit": "percent",
        "table": "B23025",
        "kind": "rate",
        "numerator": ["B23025_006"],
        "denominator_add": ["B23025_001"],  # Total population 16 years and over
        "denominator_sub": [],
    },
    "limited_english_household_rate": {
        "label": "Share of households that are limited English speaking",
        "unit": "percent",
        "table": "C16002",
        "kind": "rate",
        # Limited English speaking household: Spanish, Other Indo-European,
        # Asian and Pacific Island, Other languages
        "numerator": ["C16002_004", "C16002_007", "C16002_010", "C16002_013"],
        "denominator_add": ["C16002_001"],
        "denominator_sub": [],
    },
}

# Census API annotation sentinels (https://www.census.gov/data/developers/data-sets/acs-1year/notes-on-acs-estimate-and-annotation-values.html)
# For MOEs, -555555555 means the estimate is controlled and has no sampling error.
MOE_CONTROLLED = -555555555
ANNOTATION_VALUES = {
    -999999999, -888888888, -666666666, -555555555, -333333333, -222222222,
}


# ---------------------------------------------------------------------------
# HTTP helpers (never let the API key reach logs or exception messages)
# ---------------------------------------------------------------------------

class CensusError(RuntimeError):
    pass


def public_url(url: str, params: dict) -> str:
    """URL with all params except the key, safe to store and print."""
    safe = {k: v for k, v in params.items() if k != "key"}
    return f"{url}?{urlencode(safe, safe=':*(),')}"


def get_json(session: requests.Session, url: str, params: dict | None = None):
    params = params or {}
    shown = public_url(url, params) if params else url
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # The exception text may include the full URL with the key; drop it.
        raise CensusError(f"Request failed for {shown}: {type(exc).__name__}") from None
    if resp.status_code != 200:
        body = resp.text[:300].replace(params.get("key", "\0"), "<redacted>")
        raise CensusError(f"HTTP {resp.status_code} for {shown}: {body}")
    try:
        return resp.json()
    except ValueError:
        body = resp.text[:300].replace(params.get("key", "\0"), "<redacted>")
        raise CensusError(
            f"Non-JSON response for {shown} (an invalid API key returns HTML): {body}"
        ) from None


# ---------------------------------------------------------------------------
# Variable verification
# ---------------------------------------------------------------------------

def all_base_vars() -> list[str]:
    out: list[str] = []
    for m in METRICS.values():
        ids = [m["var"]] if m["kind"] == "direct" else (
            m["numerator"] + m["denominator_add"] + m["denominator_sub"]
        )
        for v in ids:
            if v not in out:
                out.append(v)
    return out


def verify_variables(session: requests.Session, base_vars: list[str]) -> dict[str, str]:
    """Check every E and M variable exists in variables.json; return E labels.

    variables.json lists estimates (…E) as keys; their MOEs (…M) appear in the
    estimate entry's comma-separated "attributes" field. Accept M either way.
    """
    print(f"Verifying variable IDs against {VARIABLES_URL} ...")
    catalog = get_json(session, VARIABLES_URL)["variables"]
    missing, labels = [], {}
    for base in base_vars:
        e_id, m_id = base + "E", base + "M"
        entry = catalog.get(e_id)
        if entry is None:
            missing.append(e_id)
            continue
        attrs = {a.strip() for a in entry.get("attributes", "").split(",") if a.strip()}
        if m_id not in catalog and m_id not in attrs:
            missing.append(m_id)
            continue
        labels[base] = entry.get("label", "")
        print(f"  ok  {e_id}/{m_id}  [{entry.get('concept', '')}] {labels[base]}")
    if missing:
        raise CensusError(f"Variables not found in {VARIABLES_URL}: {', '.join(missing)}")
    return labels


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_chamber(session, api_key, geo, base_vars):
    """Return ({geo_code: {var: raw_value, 'NAME': ...}}, [source URLs])."""
    full_vars = [b + s for b in base_vars for s in ("E", "M")]
    chunks = [full_vars[i:i + MAX_VARS_PER_CALL] for i in range(0, len(full_vars), MAX_VARS_PER_CALL)]
    rows_by_geo: dict[str, dict] = {}
    urls: list[str] = []
    for chunk in chunks:
        params = {
            "get": ",".join(["NAME"] + chunk),
            "for": f"{geo}:*",
            "in": f"state:{STATE_FIPS}",
            "key": api_key,
        }
        data = get_json(session, BASE_URL, params)
        urls.append(public_url(BASE_URL, params))
        header, *rows = data
        for row in rows:
            rec = dict(zip(header, row))
            code = rec[geo]
            rows_by_geo.setdefault(code, {}).update(rec)
    return rows_by_geo, urls


# ---------------------------------------------------------------------------
# Value parsing and MOE math
# ---------------------------------------------------------------------------

def parse_estimate(raw):
    if raw is None:
        return None
    v = float(raw)
    if v in ANNOTATION_VALUES:
        return None
    return v


def parse_moe(raw):
    """Returns (moe, note). Controlled estimates have MOE 0."""
    if raw is None:
        return None, "MOE not available"
    v = float(raw)
    if v == MOE_CONTROLLED:
        return 0.0, "Estimate is controlled; no sampling error"
    if v in ANNOTATION_VALUES or v < 0:
        return None, f"MOE not available (annotation {int(v)})"
    return v, None


def sum_with_moe(rec, ids):
    """Sum of estimates with MOE = sqrt(sum MOE_i^2) (Census approximation)."""
    est_total, moe_sq, notes = 0.0, 0.0, []
    for b in ids:
        e = parse_estimate(rec.get(b + "E"))
        m, note = parse_moe(rec.get(b + "M"))
        if e is None:
            return None, None, [f"{b}E unavailable"]
        if m is None:
            return e, None, [f"{b}M unavailable"]
        if note:
            notes.append(f"{b}: {note}")
        est_total += e
        moe_sq += m * m
    return est_total, math.sqrt(moe_sq), notes


def proportion_moe(num, moe_num, den, moe_den):
    """Census Bureau approximation for the MOE of a proportion p = num/den.

    MOE_p = (1/den) * sqrt(MOE_num^2 - p^2 * MOE_den^2)
    If the radicand is negative, use the ratio formula:
    MOE_p = (1/den) * sqrt(MOE_num^2 + p^2 * MOE_den^2)
    """
    p = num / den
    radicand = moe_num ** 2 - (p ** 2) * (moe_den ** 2)
    formula = "proportion"
    if radicand < 0:
        radicand = moe_num ** 2 + (p ** 2) * (moe_den ** 2)
        formula = "ratio (proportion radicand negative)"
    return math.sqrt(radicand) / den, formula


def reliability(value, moe):
    if value is None or moe is None:
        return None, None
    if value == 0:
        # MOE relative to a zero estimate is undefined; treat as unreliable unless MOE is 0.
        return (None, moe > 0)
    ratio = moe / abs(value)
    return round(ratio, 4), ratio > RELIABILITY_THRESHOLD


def build_metric(key, spec, rec, labels, source_url):
    out = {
        "label": spec["label"],
        "unit": spec["unit"],
        "value": None,
        "moe": None,
        "moe_to_estimate": None,
        "low_reliability": None,
        "table_id": spec["table"],
        "dataset": DATASET_LABEL,
        "boundary_vintage": BOUNDARY_VINTAGE,
        "source_url": source_url,
    }
    notes: list[str] = []

    if spec["kind"] == "direct":
        b = spec["var"]
        out["variable_ids"] = [b + "E", b + "M"]
        out["variable_labels"] = {b: labels.get(b, "")}
        value = parse_estimate(rec.get(b + "E"))
        moe, note = parse_moe(rec.get(b + "M"))
        if value is None:
            notes.append(f"Estimate unavailable (raw {rec.get(b + 'E')})")
            moe = None
        if note:
            notes.append(note)
        out["value"] = value
        out["moe"] = moe
        out["computation"] = "Published estimate"
    else:
        num_ids = spec["numerator"]
        dadd, dsub = spec["denominator_add"], spec["denominator_sub"]
        used = num_ids + dadd + dsub
        out["variable_ids"] = [b + s for b in used for s in ("E", "M")]
        out["variable_labels"] = {b: labels.get(b, "") for b in used}

        num, moe_num, n1 = sum_with_moe(rec, num_ids)
        den_a, moe_a, n2 = sum_with_moe(rec, dadd)
        den_s, moe_s, n3 = sum_with_moe(rec, dsub) if dsub else (0.0, 0.0, [])
        notes += n1 + n2 + n3

        den = moe_den = None
        if den_a is not None and den_s is not None:
            den = den_a - den_s
            if moe_a is not None and moe_s is not None:
                moe_den = math.sqrt(moe_a ** 2 + moe_s ** 2)

        def fmt(ids):
            return " + ".join(ids)

        desc = f"100 * ({fmt(num_ids)}) / ({fmt(dadd)}"
        desc += f" - ({fmt(dsub)}))" if dsub else ")"
        out["computation"] = desc
        out["numerator_estimate"] = num
        out["denominator_estimate"] = den

        if num is None or den is None:
            notes.append("Rate not computable: component estimate unavailable")
        elif den <= 0:
            notes.append("Rate not computable: denominator is zero")
        else:
            out["value"] = round(100 * num / den, 2)
            if moe_num is not None and moe_den is not None:
                moe_p, formula = proportion_moe(num, moe_num, den, moe_den)
                out["moe"] = round(100 * moe_p, 2)
                out["moe_method"] = (
                    f"Census Bureau approximation, {formula} formula; "
                    "sums/differences use sqrt(sum of squared MOEs)"
                )
            else:
                notes.append("MOE not computable: component MOE unavailable")

    ratio, low = reliability(out["value"], out["moe"])
    out["moe_to_estimate"] = ratio
    out["low_reliability"] = low
    if out["value"] == 0 and out["moe"]:
        notes.append("Estimate is zero; MOE/estimate undefined, flagged low reliability")
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("CENSUS_API_KEY")
    if not api_key:
        print("ERROR: CENSUS_API_KEY not set (add it to .env at the repo root).", file=sys.stderr)
        return 1

    base_vars = all_base_vars()
    with requests.Session() as session:
        try:
            labels = verify_variables(session, base_vars)
            records = []
            for chamber, geo in CHAMBERS.items():
                print(f"Fetching {chamber} districts ({geo}) ...")
                rows, urls = fetch_chamber(session, api_key, geo, base_vars)
                source_url = urls[0] if len(urls) == 1 else urls
                for code in sorted(rows, key=lambda c: (not c.isdigit(), c)):
                    rec = rows[code]
                    if not code.isdigit():
                        # e.g. "ZZZ" = area not in any defined district
                        print(f"  skipping non-district geography {code!r}: {rec.get('NAME')}")
                        continue
                    records.append({
                        "chamber": chamber,
                        "district": int(code),
                        "geoid": f"{STATE_FIPS}{code}",
                        "name": rec.get("NAME"),
                        "metrics": {
                            k: build_metric(k, spec, rec, labels, source_url)
                            for k, spec in METRICS.items()
                        },
                    })
                print(f"  {sum(1 for r in records if r['chamber'] == chamber)} {chamber} districts")
        except CensusError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    output = {
        "metadata": {
            "dataset": DATASET_LABEL,
            "api_endpoint": BASE_URL,
            "variables_catalog": VARIABLES_URL,
            "boundary_vintage": BOUNDARY_VINTAGE,
            "state_fips": STATE_FIPS,
            "low_reliability_rule": f"MOE / estimate > {RELIABILITY_THRESHOLD:.0%}",
            "moe_confidence_level": "90%",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "record_count": len(records),
            "counts_by_chamber": {
                c: sum(1 for r in records if r["chamber"] == c) for c in CHAMBERS
            },
        },
        "districts": records,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    flagged = sum(
        1 for r in records for m in r["metrics"].values() if m["low_reliability"]
    )
    print(f"Wrote {len(records)} records to {OUTPUT_PATH.relative_to(REPO_ROOT)} "
          f"({output['metadata']['counts_by_chamber']}); {flagged} metric values flagged low_reliability.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
