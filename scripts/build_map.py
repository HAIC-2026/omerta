#!/usr/bin/env python3
"""Build data/district_shapes.json (simplified SVG paths) for the district map.

Source: U.S. Census Bureau cartographic boundary files, 2022 state legislative districts
(cb_2022_15_sldl_500k / cb_2022_15_sldu_500k). These are generalized from TIGER/Line and
clipped to the shoreline, which is what a map needs (raw TIGER/Line polygons extend over
open water). Legislative session year (LSY) 2022 matches the ACS "2022 districts" vintage.

Geometry is projected (equirectangular, cos-latitude scaled), simplified with
Douglas-Peucker, and rounded so the file stays small. The Northwestern Hawaiian Islands
(west of 161.5°W, part of House District 51 / Senate District 25) are left out of the
drawing so the main islands stay legible; the output records that.

Usage:
    python scripts/build_map.py [--src DIR]
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import tempfile
import zipfile
from pathlib import Path

import requests
import shapefile  # pyshp

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "data" / "district_shapes.json"
BASE = "https://www2.census.gov/geo/tiger/GENZ2022/shp/"
FILES = {"house": ("cb_2022_15_sldl_500k.zip", "SLDLST"), "senate": ("cb_2022_15_sldu_500k.zip", "SLDUST")}
WEST_CUTOFF = -161.5          # drop parts entirely west of this longitude (NW Hawaiian Islands)
LAT0 = 20.6                    # projection reference latitude
TOLERANCE = 0.0012             # Douglas-Peucker tolerance in projected degrees (~130 m)
MIN_RING_AREA = 2e-6           # drop slivers/specks smaller than this (projected deg²)
WIDTH = 1000.0                 # SVG viewBox width


def project(lon, lat):
    return ((lon + 160.0) * math.cos(math.radians(LAT0)), -(lat - LAT0))


def dp(points, tol):
    """Douglas-Peucker (iterative)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = points[a], points[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            x0, y0 = points[i]
            d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm
            if d > best:
                best, idx = d, i
        if best > tol and idx > 0:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def dp_ring(points, tol):
    """Simplify a closed ring: split at the point farthest from the start, simplify both halves."""
    if points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 4:
        return points
    x0, y0 = points[0]
    k = max(range(len(points)), key=lambda i: (points[i][0] - x0) ** 2 + (points[i][1] - y0) ** 2)
    a = dp(points[:k + 1], tol)
    b = dp(points[k:] + [points[0]], tol)
    return a[:-1] + b[:-1]


def area(ring):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def rings_of(shape):
    parts = list(shape.parts) + [len(shape.points)]
    for a, b in zip(parts, parts[1:]):
        yield shape.points[a:b]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, help="directory containing the two cb_2022 zip files")
    args = ap.parse_args()
    src = args.src or Path(tempfile.mkdtemp())

    projected: dict[str, dict[int, list]] = {}
    dropped: dict[str, list[int]] = {}
    n_in = n_out = 0
    for chamber, (fname, field) in FILES.items():
        path = src / fname
        if not path.exists():
            print(f"Downloading {BASE + fname}")
            r = requests.get(BASE + fname, timeout=120)
            r.raise_for_status()
            path.write_bytes(r.content)
        with zipfile.ZipFile(path) as z:
            stem = fname[:-4]
            rdr = shapefile.Reader(shp=io.BytesIO(z.read(stem + ".shp")),
                                   shx=io.BytesIO(z.read(stem + ".shx")),
                                   dbf=io.BytesIO(z.read(stem + ".dbf")))
            names = [f[0] for f in rdr.fields[1:]]
            projected[chamber] = {}
            for sr in rdr.iterShapeRecords():
                rec = dict(zip(names, sr.record))
                if rec.get("LSY") != "2022" or rec.get("STATEFP") != "15":
                    print(f"ERROR: unexpected record {rec}", file=sys.stderr)
                    return 1
                d = int(rec[field])
                rings = []
                for ring in rings_of(sr.shape):
                    n_in += len(ring)
                    if max(p[0] for p in ring) < WEST_CUTOFF:
                        dropped.setdefault(chamber, [])
                        if d not in dropped[chamber]:
                            dropped[chamber].append(d)
                        continue
                    pts = dp_ring([project(*p) for p in ring], TOLERANCE)
                    if len(pts) >= 4 and area(pts) >= MIN_RING_AREA:
                        rings.append(pts)
                projected[chamber][d] = rings

    xs = [x for c in projected.values() for rs in c.values() for r in rs for x, _ in r]
    ys = [y for c in projected.values() for rs in c.values() for r in rs for _, y in r]
    minx, miny = min(xs), min(ys)
    scale = WIDTH / (max(xs) - minx)
    height = (max(ys) - miny) * scale

    def fmt(v):
        return f"{v:.1f}".rstrip("0").rstrip(".")

    out_ch: dict[str, dict] = {}
    for chamber, dists in projected.items():
        out_ch[chamber] = {}
        for d, rings in sorted(dists.items()):
            path_parts, bx, by = [], [], []
            for r in rings:
                pts = [((x - minx) * scale, (y - miny) * scale) for x, y in r]
                n_out += len(pts)
                bx += [p[0] for p in pts]
                by += [p[1] for p in pts]
                path_parts.append("M" + "L".join(f"{fmt(x)},{fmt(y)}" for x, y in pts) + "Z")
            if not path_parts:
                print(f"ERROR: {chamber} {d} has no drawable geometry", file=sys.stderr)
                return 1
            out_ch[chamber][str(d)] = {
                "d": "".join(path_parts),
                "bbox": [round(min(bx), 1), round(min(by), 1), round(max(bx), 1), round(max(by), 1)],
            }

    data = {
        "source": {
            "name": "U.S. Census Bureau, 2022 cartographic boundary files, state legislative districts (1:500,000)",
            "files": [BASE + f for f, _ in FILES.values()],
            "vintage": "2022 districts (legislative session year 2022)",
            "note": "Generalized from TIGER/Line and clipped to the shoreline; further simplified for display.",
        },
        "view_box": [0, 0, WIDTH, round(height, 1)],
        "not_drawn": {
            "description": "Northwestern Hawaiian Islands (west of 161.5°W) are not drawn at this scale.",
            "districts": dropped,
        },
        "chambers": out_ch,
    }
    OUT.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(REPO_ROOT)}: {OUT.stat().st_size / 1024:.0f} KB; "
          f"{n_in} source points -> {n_out} drawn; "
          f"{ {c: len(v) for c, v in out_ch.items()} } districts; not drawn: {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
