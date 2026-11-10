import json
import re

import pytest

from agent import tools
from agent.tools import (
    DATA_PATH,
    compare_districts,
    get_district_profile,
    rank_districts,
    statewide_context,
)

RAW = json.loads(DATA_PATH.read_text(encoding="utf-8"))
META = RAW["metadata"]
METRICS = sorted({m for r in RAW["districts"] for m in r["metrics"]})
COUNTS = META["counts_by_chamber"]
CITATION_RE = re.compile(
    r"^U\.S\. Census Bureau, ACS \d{4}–\d{4} 5-year, Tables? [A-Z]\d{5}(, [A-Z]\d{5})*; "
    r"\d{4} district boundaries$"
)


def raw_record(chamber, district):
    return next(r for r in RAW["districts"]
                if r["chamber"] == chamber and r["district"] == district)


def all_metric_dicts(obj):
    """Yield every nested dict that looks like a metric view (has a table_id)."""
    if isinstance(obj, dict):
        if "table_id" in obj and "low_reliability" in obj:
            yield obj
        for v in obj.values():
            yield from all_metric_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_metric_dicts(v)


# --- loading ---------------------------------------------------------------

def test_all_76_districts_load():
    data = tools.load_data()
    assert len(data["districts"]) == 76
    assert sum(1 for c, _ in data["index"] if c == "house") == 51
    assert sum(1 for c, _ in data["index"] if c == "senate") == 25
    assert tools._districts_in("house") == list(range(1, 52))
    assert tools._districts_in("senate") == list(range(1, 26))


@pytest.mark.parametrize("chamber,n", [("house", 51), ("senate", 25)])
def test_every_district_profile_loads(chamber, n):
    for d in range(1, n + 1):
        p = get_district_profile(chamber, d)
        assert "error" not in p
        assert set(p["metrics"]) == set(METRICS)


# --- values match the JSON exactly ------------------------------------------

def test_house_1_population_matches_json():
    expected = raw_record("house", 1)["metrics"]["total_population"]
    got = get_district_profile("house", 1)["metrics"]["total_population"]
    assert got["value"] == expected["value"]
    assert got["moe"] == expected["moe"]
    assert got["table_id"] == expected["table_id"]


def test_profile_fields_present():
    m = get_district_profile("house", 1)["metrics"]["median_household_income"]
    for key in ("value", "moe", "low_reliability", "source_url", "year",
                "boundary_vintage", "citation"):
        assert key in m
    assert m["year"] == str(META["acs_year"])
    assert META["dataset"] == f"ACS {META['acs_year'] - 4}–{META['acs_year']} 5-year"
    assert m["boundary_vintage"] == META["boundary_vintage"]


def test_citation_format_example():
    m = get_district_profile("house", 1)["metrics"]["median_household_income"]
    assert m["citation"] == (
        f"U.S. Census Bureau, {META['dataset']}, Table B19013; "
        f"{META['boundary_vintage'].split()[0]} district boundaries"
    )


def test_compare_values_match_json():
    out = compare_districts("senate", [1, 2, 3], "poverty_rate")
    assert [r["district"] for r in out["districts"]] == [1, 2, 3]
    for r in out["districts"]:
        assert r["value"] == raw_record("senate", r["district"])["metrics"]["poverty_rate"]["value"]


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_rank_is_sorted_and_matches_json(order):
    out = rank_districts("house", "median_household_income", top_n=51, order=order)
    vals = [r["value"] for r in out["results"]]
    assert vals == sorted(vals, reverse=(order == "desc"))
    top = out["results"][0]
    assert top["value"] == raw_record("house", top["district"])["metrics"]["median_household_income"]["value"]


def test_rank_top_n_respected():
    assert len(rank_districts("senate", "veterans", top_n=3)["results"]) == 3


def test_statewide_context_matches_json():
    out = statewide_context("median_age", chamber="senate")
    vals = [r["metrics"]["median_age"]["value"] for r in RAW["districts"] if r["chamber"] == "senate"]
    s = out["chambers"]["senate"]
    assert s["min"]["value"] == min(vals)
    assert s["max"]["value"] == max(vals)
    assert s["median_of_districts"] == sorted(vals)[len(vals) // 2]  # 25 districts: odd count
    assert set(statewide_context("median_age")["chambers"]) == {"house", "senate"}


# --- invalid input errors, never guesses -------------------------------------

@pytest.mark.parametrize("call", [
    lambda: get_district_profile("assembly", 1),
    lambda: get_district_profile("", 1),
    lambda: get_district_profile(None, 1),
    lambda: get_district_profile("house", 0),
    lambda: get_district_profile("house", 52),
    lambda: get_district_profile("senate", 26),
    lambda: get_district_profile("house", -1),
    lambda: get_district_profile("house", 1.5),
    lambda: get_district_profile("house", True),
    lambda: get_district_profile("house", "one"),
    lambda: get_district_profile("house", None),
    lambda: compare_districts("house", [1, 99], "poverty_rate"),
    lambda: compare_districts("house", [], "poverty_rate"),
    lambda: compare_districts("house", [1, 2], "poverty"),
    lambda: compare_districts("upper", [1, 2], "poverty_rate"),
    lambda: rank_districts("house", "income", 5, "desc"),
    lambda: rank_districts("house", "poverty_rate", 0, "desc"),
    lambda: rank_districts("house", "poverty_rate", 52, "desc"),
    lambda: rank_districts("senate", "poverty_rate", 26, "desc"),
    lambda: rank_districts("house", "poverty_rate", 5, "highest"),
    lambda: statewide_context("not_a_metric"),
    lambda: statewide_context("poverty_rate", chamber="lower"),
])
def test_invalid_inputs_return_error(call):
    out = call()
    assert set(out) - {"valid_chambers", "valid_districts", "valid_metrics", "valid_orders"} == {"error"}
    assert isinstance(out["error"], str) and out["error"]


def test_error_lists_valid_options():
    out = get_district_profile("senate", 30)
    assert out["valid_districts"] == list(range(1, 26))
    assert get_district_profile("house", "senate")["error"]


def test_accepted_input_normalization():
    # Case/whitespace on chamber and digit strings for district are exact, not guesses.
    a = get_district_profile(" House ", "7")
    b = get_district_profile("house", 7)
    assert a == b


# --- every response has a citation -------------------------------------------

def every_success_response():
    for chamber, n in COUNTS.items():
        for d in range(1, n + 1):
            yield get_district_profile(chamber, d)
        for metric in METRICS:
            yield compare_districts(chamber, list(range(1, n + 1)), metric)
            for order in ("desc", "asc"):
                yield rank_districts(chamber, metric, n, order)
            yield statewide_context(metric, chamber)
    for metric in METRICS:
        yield statewide_context(metric)


def test_every_response_has_citation():
    count = 0
    for resp in every_success_response():
        count += 1
        assert "error" not in resp
        assert CITATION_RE.match(resp["citation"]), resp["citation"]
        for m in all_metric_dicts(resp):
            assert CITATION_RE.match(m["citation"]), m["citation"]
            assert f"Table {m['table_id']};" in m["citation"]
    assert count > 0


def test_low_reliability_always_has_caveat():
    seen_flagged = 0
    for resp in every_success_response():
        for m in all_metric_dicts(resp):
            if m["low_reliability"]:
                seen_flagged += 1
                assert m.get("caveat")
            else:
                assert "caveat" not in m
    assert seen_flagged > 0  # the dataset has flagged values, so this path is exercised
