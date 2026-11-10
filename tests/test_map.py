"""Offline tests for the district map data (shapes + /api/map)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent import tools
from web import app as webapp

SHAPES = json.loads(webapp.SHAPES_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def client():
    return TestClient(webapp.app)


def test_shapes_cover_every_district_and_are_small():
    for chamber in tools.CHAMBERS:
        assert sorted(map(int, SHAPES["chambers"][chamber])) == tools._districts_in(chamber)
        for d, g in SHAPES["chambers"][chamber].items():
            assert g["d"].startswith("M") and g["d"].endswith("Z")
            x0, y0, x1, y1 = g["bbox"]
            assert x1 > x0 and y1 > y0
    assert webapp.SHAPES_PATH.stat().st_size < 150_000
    assert "2022" in SHAPES["source"]["vintage"]


def test_api_map_values_match_tools(client):
    r = client.get("/api/map", params={"chamber": "house", "metric": "poverty_rate"})
    assert r.status_code == 200
    j = r.json()
    assert len(j["districts"]) == 51
    for row in j["districts"]:
        m = tools.get_district_profile("house", row["district"])["metrics"]["poverty_rate"]
        assert row["value"] == m["value"]
        assert row["value_display"] == m["value_display"] and row["moe_display"] == m["moe_display"]
        assert row["low_reliability"] == m["low_reliability"]
        assert row["path"] and row["island_group"] in {"oahu", "hawaii", "maui", "kauai"}
    meta = tools.load_data()["metadata"]
    assert j["citation"] == (f"U.S. Census Bureau, {meta['dataset']}, Table B17001; "
                             f"{meta['boundary_vintage'].split()[0]} district boundaries")


def test_island_groups_are_sensible(client):
    j = client.get("/api/map", params={"chamber": "senate", "metric": "median_age"}).json()
    groups = {r["district"]: r["island_group"] for r in j["districts"]}
    assert groups[8] == "kauai"
    assert sum(1 for g in groups.values() if g == "oahu") == 17  # report: 17 Senate seats on Oʻahu


@pytest.mark.parametrize("params", [{"chamber": "assembly"}, {"metric": "income"}, {"chamber": ""}])
def test_api_map_invalid(client, params):
    r = client.get("/api/map", params=params)
    assert r.status_code == 422 and r.json()["code"] == "invalid_request"


def test_map_page_served(client):
    r = client.get("/map")
    assert r.status_code == 200 and "Hawaiʻi District Map" in r.text
    assert "map-table" in r.text and 'for="map-metric"' in r.text
