"""The ACS release upgrade: one year constant, documented boundary vintage, the archived
previous release, and the old-vs-new comparison report. Offline; reads only local files."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agent import tools

ROOT = Path(__file__).resolve().parent.parent
META = tools.load_data()["metadata"]
ARCHIVE = ROOT / "data" / "archive" / "districts_acs2023.json"
REPORT = ROOT / "reports" / "acs_2023_vs_2024.json"


def _pull_acs():
    spec = importlib.util.spec_from_file_location("pull_acs", ROOT / "scripts" / "pull_acs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_data_matches_the_single_year_constant():
    pa = _pull_acs()
    assert META["acs_year"] == pa.ACS_YEAR
    assert META["dataset"] == pa.DATASET_LABEL == f"ACS {pa.ACS_YEAR - 4}–{pa.ACS_YEAR} 5-year"
    assert META["api_endpoint"].endswith(f"/data/{pa.ACS_YEAR}/acs/acs5")
    income = tools.load_data()["districts"][0]["metrics"]["median_household_income"]
    assert f"{pa.ACS_YEAR} inflation-adjusted" in income["label"]


def test_all_districts_from_the_release():
    assert META["counts_by_chamber"] == {"house": 51, "senate": 25}
    for r in tools.load_data()["districts"]:
        assert f"({META['acs_year']})" in r["name"]  # Census names carry the SLD vintage


def test_boundary_vintage_is_documented():
    vd = META["boundary_vintage_documentation"]
    assert vd["stated_vintage"] == f"{META['acs_year']} Legislative Session Year"
    assert vd["source_url"].startswith("https://www.census.gov/")
    assert META["boundary_vintage"].startswith(str(META["acs_year"]))


def test_previous_release_archived():
    old = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    assert old["metadata"]["dataset"] == "ACS 2023 5-year"
    assert old["metadata"]["record_count"] == 76


def test_comparison_report_matches_both_files():
    old = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    o = {(r["chamber"], r["district"]): r["metrics"] for r in old["districts"]}
    n = {(r["chamber"], r["district"]): r["metrics"] for r in tools.load_data()["districts"]}
    changed = 0
    assert len(rep["districts"]) == 76
    for row in rep["districts"]:
        key = (row["chamber"], row["district"])
        for m, e in row["metrics"].items():
            assert e["old_value"] == o[key][m]["value"]
            assert e["new_value"] == n[key][m]["value"]
            assert e["new_low_reliability"] == n[key][m]["low_reliability"]
            changed += e["low_reliability_changed"]
    assert rep["summary"]["low_reliability_flags_changed"] == changed
    assert rep["new"]["dataset"] == META["dataset"]


def test_house_46_gap_uses_current_release():
    ctx = tools.get_district_profile("house", 46)["district_context"]["acs_vs_reapportionment"]
    current = tools.load_data()["index"][("house", 46)]["metrics"]["total_population"]["value"]
    old = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    old46 = next(r for r in old["districts"] if r["chamber"] == "house" and r["district"] == 46)
    assert ctx["acs_total_population"] == current
    assert current != old46["metrics"]["total_population"]["value"]
