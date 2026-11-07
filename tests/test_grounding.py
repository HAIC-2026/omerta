"""Offline tests for the number and caveat checkers in agent/grounding.py."""

from decimal import Decimal

from agent.grounding import (
    extract_numbers,
    flagged_values_without_caveat,
    is_supported,
    stated_flagged_values,
)


def test_number_checker():
    src = {Decimal("79754.0"), Decimal("12.09"), Decimal("0.3712"), Decimal("2023")}
    assert is_supported("79754", src)
    assert is_supported("12.1", src)
    assert is_supported("12.09", src)
    assert is_supported("37", src)        # fraction stated as percent
    assert is_supported("80000", src)     # rounded to thousands
    assert not is_supported("100000", src)  # one significant digit: too loose
    assert not is_supported("12.2", src)
    assert not is_supported("5000", src)
    assert extract_numbers("Table B19013, $79,754 (± 6,803), 12.1%, 3rd, C16002_004") == \
        ["79754", "6803", "12.1", "3"]


PROFILE = {"chamber": "house", "district": 7, "metrics": {
    "limited_english_household_rate": {"label": "LEP", "value": 6.43, "moe": 4.27,
                                       "low_reliability": True, "caveat": "Low reliability: ..."},
    "median_age": {"label": "Age", "value": 42.1, "moe": 1.8, "low_reliability": False}}}


def test_caveat_checker():
    outs = [PROFILE]
    ok = "About 6.4% of households are limited-English. This is a low-reliability estimate."
    assert flagged_values_without_caveat(ok, outs) == {"stated": ["LEP (district 7) = 6.43"], "missing": []}

    bad = "Median age is 42.1.\n\nAbout 6.43% of households are limited-English."
    assert flagged_values_without_caveat(bad, outs)["missing"] == ["LEP (district 7) = 6.43"]

    # A caveat paragraph directly after the value counts...
    nxt = "6.43% are limited-English.\n\n**Low reliability:** treat this figure as approximate."
    assert not flagged_values_without_caveat(nxt, outs)["missing"]
    # ...but one further away does not.
    far = ("6.43% are limited-English.\n\nMedian age is 42.1.\n\n"
           "A low-reliability note about something else.")
    assert flagged_values_without_caveat(far, outs)["missing"]
    # A note after a table counts.
    table = "| Metric | Value |\n|---|---|\n| LEP | 6.43% |\n\n* LEP is a low-reliability estimate."
    assert not flagged_values_without_caveat(table, outs)["missing"]

    # A one-digit number (a rank like "2nd") doesn't count as stating 1.53.
    rank_out = {"results": [{"district": 3, "label": "LEP", "value": 1.53, "low_reliability": True}]}
    assert flagged_values_without_caveat("| 2nd | SD 16 | 11.59% |", [rank_out])["stated"] == []
    assert flagged_values_without_caveat("It is 1.5%.", [rank_out])["missing"]

    # Unflagged values never need a caveat.
    assert flagged_values_without_caveat("Median age is 42.1.", outs) == {"stated": [], "missing": []}


def test_stated_flagged_values():
    text = "Median age is 42.1. About 6.4% are limited-English (low reliability)."
    found = stated_flagged_values(text, [PROFILE])
    assert len(found) == 1
    f = found[0]
    assert (f["chamber"], f["district"], f["label"], f["value"]) == ("house", 7, "LEP", 6.43)
    assert f["matched_text"] == ["6.4"]
    assert f["caveat"] == "Low reliability: ..."
    assert stated_flagged_values("Median age is 42.1.", [PROFILE]) == []
