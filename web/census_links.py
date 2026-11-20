"""Human-readable source links for the Sources panel's "Data lookups".

The raw Census API query a metric came from returns JSON (or the Census error page) in a
browser, so each lookup's primary link is the data.census.gov page for the ACS table, showing
every district in that chamber. The API query stays as a secondary link.

Links are built only from a tool output's own table IDs, chamber and district numbers; nothing
here changes the output or the numbers.
"""

from __future__ import annotations

import re
from typing import Any

# The one place the data.census.gov link format lives: the table page for an ACS 5-year
# detailed table, for all State House (620) or State Senate (610) districts in Hawaiʻi (15).
CENSUS_TABLE_LINKS = {
    "url_template": "https://data.census.gov/table/ACSDT5Y2024.{table}?g={geo}",
    "acs_year": 2024,  # must match the template and data/districts.json (checked by a test)
    "chamber_geo": {
        "house": "040XX00US15$6200000",
        "senate": "040XX00US15$6100000",
    },
    "chamber_names": {"house": "House", "senate": "Senate"},
}

API_PREFIX = "https://api.census.gov/"
MAX_DISTRICTS_IN_LABEL = 6
_TABLE_ID = re.compile(r"[BC]\d{5}[A-Z]?")
_KEY_PARAM = re.compile(r"(?i)([?&])key=[^&#]*&?")


def census_table_url(table_id: str, chamber: str) -> str:
    """data.census.gov page for one ACS table, all districts in one chamber."""
    if not _TABLE_ID.fullmatch(table_id):
        raise ValueError(f"not an ACS table id: {table_id!r}")
    return CENSUS_TABLE_LINKS["url_template"].format(
        table=table_id, geo=CENSUS_TABLE_LINKS["chamber_geo"][chamber])


def census_table_label(table_id: str, chamber: str, districts: list[int] | tuple[int, ...] = ()) -> str:
    label = f"Census table {table_id} — all {CENSUS_TABLE_LINKS['chamber_names'][chamber]} districts"
    ds = list(districts)
    if len(ds) == 1:
        label += f" (see District {ds[0]})"
    elif 1 < len(ds) <= MAX_DISTRICTS_IN_LABEL:
        label += f" (see Districts {', '.join(str(d) for d in ds)})"
    return label


def census_table_links(output: Any) -> list[dict]:
    """One link per (ACS table, chamber) among the Census metric values in a tool output, in
    the order they appear, each with the districts whose values the lookup returned."""
    found: dict[tuple[str, str], list[int]] = {}

    def walk(o: Any, chamber: Any, district: Any) -> None:
        if isinstance(o, dict):
            chamber = o.get("chamber", chamber)
            district = o.get("district", district)
            table, url = o.get("table_id"), o.get("source_url")
            if (isinstance(table, str) and _TABLE_ID.fullmatch(table)
                    and isinstance(url, str) and url.startswith(API_PREFIX)
                    and chamber in CENSUS_TABLE_LINKS["chamber_geo"]):
                ds = found.setdefault((table, chamber), [])
                if isinstance(district, int) and not isinstance(district, bool) and district not in ds:
                    ds.append(district)
            for v in o.values():
                walk(v, chamber, district)
        elif isinstance(o, list):
            for v in o:
                walk(v, chamber, district)

    walk(output, None, None)
    return [{"table_id": t, "chamber": c, "districts": ds,
             "url": census_table_url(t, c), "label": census_table_label(t, c, ds)}
            for (t, c), ds in found.items()]


def strip_api_key(url: str) -> str:
    """A Census API URL with any key= parameter removed (the stored URLs carry none)."""
    if not url.startswith(API_PREFIX) or "key=" not in url.lower():
        return url
    return _KEY_PARAM.sub(lambda m: m.group(1), url).rstrip("?&")
