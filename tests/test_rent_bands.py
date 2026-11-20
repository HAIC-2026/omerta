"""Offline tests for the rent-burden bands (ACS Table B25070). No network calls: the raw rows
pulled from the Census API are in data/acs_b25070_raw.json."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from agent import chat, tools
from agent.grounding import check_answer_runtime
from agent.program_tools import list_available_metrics
from web.census_links import census_table_links

ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "data" / "acs_b25070_raw.json").read_text(encoding="utf-8"))
TOTAL = "renter_cost_burden_rate"
BANDS = {  # measure -> the one B25070 band it counts
    "renter_burden_30_to_34_rate": "B25070_007",
    "renter_burden_35_to_39_rate": "B25070_008",
    "renter_burden_40_to_49_rate": "B25070_009",
    "renter_severe_cost_burden_rate": "B25070_010",
}
SEVERE = "renter_severe_cost_burden_rate"


def _load(name):
    path = ROOT / "scripts" / f"{name}.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(path.parent))


def _records():
    return tools.load_data()["districts"]


# ------------------------------------------------------------------ the data

def test_every_district_has_the_four_bands_and_the_raw_pull_covers_them():
    recs = _records()
    assert len(recs) == 76
    for r in recs:
        assert set(BANDS) <= set(r["metrics"])
        row = RAW["rows"][r["chamber"]][str(r["district"])]
        assert row["NAME"] == r["name"]
        for i in range(1, 12):  # all eleven bands, estimates and MOEs
            assert f"B25070_{i:03d}E" in row and f"B25070_{i:03d}M" in row
    assert {c: len(v) for c, v in RAW["rows"].items()} == {"house": 51, "senate": 25}
    assert "key=" not in json.dumps(RAW)
    assert RAW["metadata"]["dataset"] == tools.load_data()["metadata"]["dataset"]


def test_band_shares_add_up_to_the_30_plus_share_in_every_district():
    for r in _records():
        m = r["metrics"]
        where = (r["chamber"], r["district"])
        total, bands = m[TOTAL], [m[k] for k in BANDS]
        # Same denominator (total minus not computed) and numerators that sum exactly.
        assert {b["denominator_estimate"] for b in bands} == {total["denominator_estimate"]}, where
        assert sum(b["numerator_estimate"] for b in bands) == total["numerator_estimate"], where
        # Each share is rounded to 2 decimals, so the sum may differ by rounding only.
        assert abs(sum(b["value"] for b in bands) - total["value"]) <= 0.025 + 1e-9, where
        row = RAW["rows"][r["chamber"]][str(r["district"])]
        assert total["denominator_estimate"] == float(row["B25070_001E"]) - float(row["B25070_011E"]), where


def test_the_sum_check_fails_loudly_on_bad_data():
    pa = _load("pull_acs")
    recs = copy.deepcopy(_records())
    pa.check_rent_bands(recs)  # the real data passes
    recs[0]["metrics"][SEVERE]["value"] += 1.0
    recs[0]["metrics"][SEVERE]["numerator_estimate"] += 50
    with pytest.raises(pa.CensusError, match="do not add up"):
        pa.check_rent_bands(recs)


def test_offline_rebuild_reproduces_the_committed_data_and_touches_nothing_else():
    arb = _load("add_rent_bands")
    committed = json.loads((ROOT / "data" / "districts.json").read_text(encoding="utf-8"))
    without = copy.deepcopy(committed)
    for r in without["districts"]:
        for k in BANDS:
            del r["metrics"][k]
    rebuilt = arb.merge(copy.deepcopy(without), RAW)
    assert rebuilt == committed
    for a, b in zip(without["districts"], rebuilt["districts"]):
        assert {k: v for k, v in b["metrics"].items() if k not in BANDS} == a["metrics"]
        # The bands sit right after the 30%+ measure.
        keys = list(b["metrics"])
        i = keys.index(TOTAL)
        assert keys[i + 1:i + 5] == list(BANDS)
    # A pull that disagrees with the stored 30%+ measure is refused.
    bad = copy.deepcopy(RAW)
    bad["rows"]["house"]["46"]["B25070_010E"] = "9999"
    with pytest.raises(arb.pa.CensusError, match="does not match"):
        arb.merge(copy.deepcopy(without), bad)


# ------------------------------------------------------------------ MOE math

def _expected(row: dict, band: str) -> tuple[float, float]:
    """Share and MOE written out from the Census formulas, independent of the build code:
    difference MOE = sqrt(a^2 + b^2); proportion MOE = sqrt(MOE_num^2 - p^2 * MOE_den^2) / den,
    with the ratio formula (+) when the radicand is negative."""
    num, moe_num = float(row[band + "E"]), float(row[band + "M"])
    den = float(row["B25070_001E"]) - float(row["B25070_011E"])
    moe_den = math.sqrt(float(row["B25070_001M"]) ** 2 + float(row["B25070_011M"]) ** 2)
    p = num / den
    rad = moe_num ** 2 - p ** 2 * moe_den ** 2
    if rad < 0:
        rad = moe_num ** 2 + p ** 2 * moe_den ** 2
    return round(100 * p, 2), round(100 * math.sqrt(rad) / den, 2)


@pytest.mark.parametrize("chamber,district", [("house", 46), ("senate", 4)])
def test_band_moe_math_on_two_districts(chamber, district):
    row = RAW["rows"][chamber][str(district)]
    metrics = tools.load_data()["index"][(chamber, district)]["metrics"]
    for key, band in BANDS.items():
        m = metrics[key]
        value, moe = _expected(row, band)
        assert (m["value"], m["moe"]) == (value, moe), key
        assert m["table_id"] == "B25070" and m["unit"] == "percent"
        assert m["variable_ids"] == [band + "E", band + "M", "B25070_001E", "B25070_001M",
                                     "B25070_011E", "B25070_011M"]
        assert "Census Bureau approximation" in m["moe_method"]
    # The same formulas reproduce the existing 30%+ measure (bands summed with sqrt of squares).
    total = metrics[TOTAL]
    num = sum(float(row[b + "E"]) for b in BANDS.values())
    moe_num = math.sqrt(sum(float(row[b + "M"]) ** 2 for b in BANDS.values()))
    den = float(row["B25070_001E"]) - float(row["B25070_011E"])
    moe_den = math.sqrt(float(row["B25070_001M"]) ** 2 + float(row["B25070_011M"]) ** 2)
    p = num / den
    assert total["value"] == round(100 * p, 2)
    assert total["moe"] == round(100 * math.sqrt(moe_num ** 2 - p ** 2 * moe_den ** 2) / den, 2)


def test_house_46_severe_burden_worked_by_hand():
    """3,152 of 9,187 - 339 = 8,848 renter households; MOEs 365, and sqrt(609^2 + 105^2)."""
    row = RAW["rows"]["house"]["46"]
    assert [row[k] for k in ("B25070_010E", "B25070_010M", "B25070_001E", "B25070_001M",
                             "B25070_011E", "B25070_011M")] == ["3152", "365", "9187", "609", "339", "105"]
    m = tools.load_data()["index"][("house", 46)]["metrics"][SEVERE]
    assert (m["numerator_estimate"], m["denominator_estimate"]) == (3152.0, 8848.0)
    assert (m["value"], m["moe"]) == (35.62, 3.29)


def test_bands_use_the_same_reliability_rule():
    flagged = 0
    for r in _records():
        for k in BANDS:
            m = r["metrics"][k]
            assert m["value"] is not None and m["moe"] is not None
            if m["value"] == 0:
                assert m["low_reliability"] is (m["moe"] > 0)
            else:
                assert m["moe_to_estimate"] == round(m["moe"] / m["value"], 4)
                assert m["low_reliability"] is (m["moe"] / m["value"] > 0.30)
            flagged += m["low_reliability"]
    assert flagged  # narrow bands are often unreliable; the flag must reach the tools
    view = next(tools._metric_view(r["metrics"][k]) for r in _records() for k in BANDS
                if r["metrics"][k]["low_reliability"])
    assert view["caveat"].startswith("Low reliability")


# ---------------------------------------------------------------- tool layer

def test_bands_are_in_the_profile_compare_rank_and_measure_list():
    p = tools.get_district_profile("house", 46)
    for k in BANDS:
        v = p["metrics"][k]
        assert v["value_display"].endswith("%") and v["moe_display"].startswith("± ")
        assert v["citation"] == tools._citation(["B25070"])
    cmp_ = tools.compare_districts("senate", [4, 8], SEVERE)
    assert [d["district"] for d in cmp_["districts"]] == [4, 8] and "error" not in cmp_
    listed = {m["id"]: m for m in list_available_metrics()["metrics"]}
    for k in BANDS:
        assert listed[k]["table_id"] == "B25070" and "not computed" in listed[k]["label"]
    assert "50%+" in listed[SEVERE]["label"] and "severe" in listed[SEVERE]["label"]
    # The model can ask for them: every band is in each metric tool's enum.
    schemas = {t["name"]: t for t in chat.build_tool_schemas()}
    for name in ("compare_districts", "rank_districts", "statewide_context"):
        assert set(BANDS) <= set(schemas[name]["input_schema"]["properties"]["metric"]["enum"])
    assert chat.run_tool("rank_districts", {"chamber": "house", "metric": SEVERE,
                                            "top_n": 1, "order": "desc"})["results"]


@pytest.mark.parametrize("chamber", ["house", "senate"])
def test_rank_districts_on_the_50_plus_share(chamber):
    out = tools.rank_districts(chamber, SEVERE, 5, "desc")
    assert "error" not in out and out["metric"] == SEVERE and out["unit"] == "percent"
    index = tools.load_data()["index"]
    everyone = sorted((index[(chamber, d)]["metrics"][SEVERE]["value"], -d)
                      for d in tools._districts_in(chamber))[::-1]
    assert [(r["value"], -r["district"]) for r in out["results"]] == everyone[:5]
    assert [r["rank"] for r in out["results"]] == [1, 2, 3, 4, 5]
    assert out["districts_ranked"] == len(everyone)
    assert out["citation"] == tools._citation(["B25070"])
    lowest = tools.rank_districts(chamber, SEVERE, 1, "asc")["results"][0]
    assert lowest["value"] == min(v for v, _ in everyone)


def test_band_lookups_link_the_data_census_gov_table():
    for chamber, geo in (("house", "040XX00US15$6200000"), ("senate", "040XX00US15$6100000")):
        links = census_table_links(tools.rank_districts(chamber, SEVERE, 3, "desc"))
        assert [link["url"] for link in links] == [
            f"https://data.census.gov/table/ACSDT5Y2024.B25070?g={geo}"]
    # The raw query behind the bands is the B25070-only pull, with no key.
    from web import app as webapp
    urls = webapp._source_urls(tools.compare_districts("house", [46], SEVERE))
    assert urls == [RAW["metadata"]["source_urls"]["house"]]
    assert "B25070_010E" in urls[0] and "B01003" not in urls[0] and "key=" not in urls[0]


# ------------------------------------------------------------------ grounding

def test_grounding_passes_a_band_answer_and_catches_a_wrong_one():
    out = tools.get_district_profile("house", 46)
    m = out["metrics"]
    assert not any(m[k]["low_reliability"] for k in BANDS)
    answer = (
        f"In House District 46, {m[SEVERE]['value_display']} ({m[SEVERE]['moe_display']}) of renter "
        "households pay 50% or more of income on gross rent.\n\n"
        f"The other bands: 30.0% to 34.9% is {m['renter_burden_30_to_34_rate']['value_display']}, "
        f"35.0% to 39.9% is {m['renter_burden_35_to_39_rate']['value_display']}, and 40.0% to 49.9% is "
        f"{m['renter_burden_40_to_49_rate']['value_display']}. Together, {m[TOTAL]['value_display']} "
        f"pay 30% or more.\n\n{m[SEVERE]['citation']}."
    )
    check = check_answer_runtime(answer, [out], ["How many renters pay 50%+ in House District 46?"])
    assert check["verified"], check["failures"]
    assert check["numbers_checked"] == check["numbers_supported"] > 10

    wrong = answer.replace(m[SEVERE]["value_display"], "36.62%")
    bad = check_answer_runtime(wrong, [out])
    assert not bad["verified"] and bad["unsupported_numbers"] == ["36.62"]


def test_grounding_requires_the_caveat_on_a_low_reliability_band():
    chamber, district, key = next((r["chamber"], r["district"], k) for r in _records() for k in BANDS
                                  if r["metrics"][k]["low_reliability"] and r["metrics"][k]["value"] >= 1)
    out = tools.compare_districts(chamber, [district], key)
    v = out["districts"][0]
    text = f"The share is {v['value_display']} ({v['moe_display']}). {v['citation']}."
    assert not check_answer_runtime(text, [out])["verified"]
    ok = check_answer_runtime(text + " This is a low-reliability estimate; treat it as approximate.", [out])
    assert ok["verified"], ok["failures"]


def test_no_agent_text_says_the_bands_are_unavailable():
    prompt = chat.SYSTEM_PROMPT.lower()
    for phrase in ("band", "breakdown", "50%+", "severe"):
        assert phrase not in prompt
