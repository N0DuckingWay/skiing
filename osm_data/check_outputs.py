#!/usr/bin/env python3
"""Sanity-check the results of filter_near_runs.py.

Checks:
    counts      features per layer, runs per layer
    resorts     known resorts have runs (informational; only resorts inside the data count)
    zone        every run is inside the zone, and the zone edge is 10 mi from the nearest run
    exactness   random features: kept ones are within 10 mi of a run, dropped ones are not
    pass2       large areas found only by pass 2 made it into the final file
    states      state/province polygons around the runs are present
    refs        ways in near_runs.osm.pbf are not missing any nodes

Usage:
    python osm_data/check_outputs.py --data-dir "/path/to/osm_data/data"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import shapely

from common import DEFAULT_DATA_DIR, read_json

METERS_PER_MILE = 1609.344
# Allowed error in metres. Buffer circles are drawn with straight segments (~20 m sag)
# and UTM stretches distances by up to ~0.1% near zone edges.
TOLERANCE_M = 60

RESORTS = {
    "Vail": (39.606, -106.355),
    "Whistler": (50.108, -122.948),
    "Mont-Tremblant": (46.210, -74.585),
    "Jay Peak": (44.938, -72.505),
    "Stowe": (44.530, -72.781),
    "Killington": (43.620, -72.796),
    "Loon": (44.036, -71.621),
    "Bretton Woods": (44.258, -71.441),
}

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def info(name: str, detail: str) -> None:
    print(f"[info] {name}: {detail}")


def read_all_layers(gpkg: Path, **kwargs) -> dict[str, gpd.GeoDataFrame]:
    return {layer: pyogrio.read_dataframe(gpkg, layer=layer, **kwargs)
            for layer in pyogrio.list_layers(gpkg)[:, 0]}


class RunDistance:
    """Distance in metres from any geometry to the nearest run.

    Runs are grouped by UTM zone. A geometry's distance is the minimum over
    zones, each measured in that zone's projection. UTM never shrinks distances
    by more than 0.04%, so far-away zones can't make something look close.
    """

    def __init__(self, runs: np.ndarray):
        lon = shapely.get_x(shapely.centroid(runs))
        epsg = 32600 + np.clip(np.floor((lon + 180) / 6).astype(int) + 1, 1, 60)
        self.zones = []
        for code in np.unique(epsg):
            local = gpd.GeoSeries(runs[epsg == code], crs=4326).to_crs(int(code))
            self.zones.append((int(code), shapely.STRtree(np.asarray(local.array))))

    def __call__(self, geoms: np.ndarray) -> np.ndarray:
        best = np.full(len(geoms), np.inf)
        for code, tree in self.zones:
            local = np.asarray(gpd.GeoSeries(geoms, crs=4326).to_crs(code).array)
            idx, dist = tree.query_nearest(local, return_distance=True, all_matches=False)
            np.minimum.at(best, idx[0], dist)
        return best


def id_columns(gpkg: Path, layer: str) -> list[str]:
    fields = pyogrio.read_info(gpkg, layer=layer)["fields"]
    return [c for c in ("osm_id", "osm_way_id") if c in fields]


def feature_key(df) -> np.ndarray:
    """OSM identity of each row: osm_id, plus osm_way_id for areas made from closed ways."""
    key = df["osm_id"].fillna("").astype(str)
    if "osm_way_id" in df:
        key = key + "|" + df["osm_way_id"].fillna("").astype(str)
    return key.to_numpy()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--sample", type=int, default=1000, help="features sampled per layer (default: 1000)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    out = args.data_dir.resolve() / "out"
    work = args.data_dir.resolve() / "work"
    rng = np.random.default_rng(args.seed)
    manifest = read_json(out / "manifest.json") or {}
    miles = manifest.get("settings", {}).get("miles", 10.0)
    limit = miles * METERS_PER_MILE
    print(f"Checking {out}  (snapshot {manifest.get('input_snapshot', '?')}, {miles} mi)\n")

    # counts
    runs_layers = read_all_layers(out / "runs.gpkg")
    for layer, n in ((l, len(d)) for l, d in runs_layers.items()):
        info("runs", f"{layer}: {n}")
    for layer, c in manifest.get("layers", {}).items():
        info("features", f"{layer}: kept {c['kept']:,} of {c['read']:,}")
    runs = np.concatenate([np.asarray(d.geometry.array) for d in runs_layers.values() if len(d)])
    runs = runs[~shapely.is_missing(runs) & ~shapely.is_empty(runs)]
    report("counts", len(runs) > 0, f"{len(runs)} run geometries")
    distance = RunDistance(runs)

    # resorts
    run_tree = shapely.STRtree(runs)
    minx, miny, maxx, maxy = shapely.total_bounds(runs)
    for name, (lat, lon) in RESORTS.items():
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            continue
        near = run_tree.query(shapely.Point(lon, lat).buffer(0.03))  # ~3 km
        report(f"resort {name}", len(near) > 0, f"{len(near)} runs within ~3 km")

    # zone
    zone = gpd.read_file(out / "zone_10mi.gpkg").geometry.iloc[0]
    shapely.prepare(zone)
    outside = int((~shapely.covers(zone, runs)).sum())
    report("zone covers runs", outside == 0, f"{outside} runs outside the zone")
    rings = shapely.get_rings(shapely.get_parts(zone))
    pick = rng.integers(0, len(rings), args.sample)
    edge = shapely.line_interpolate_point(rings[pick], rng.random(args.sample), normalized=True)
    d = distance(edge)
    ok = bool(np.all((d >= limit - TOLERANCE_M) & (d <= limit + 1)))
    report("zone edge", ok, f"edge-to-nearest-run distance {d.min():,.0f}–{d.max():,.0f} m "
                            f"(target {limit:,.0f} m)")

    # exactness
    raw, final = work / "near_runs_raw.gpkg", out / "features_near_runs.gpkg"
    if raw.exists():
        for layer in pyogrio.list_layers(raw)[:, 0]:
            n = pyogrio.read_info(raw, layer=layer)["features"]
            if n == 0:
                continue
            fids = np.sort(rng.choice(np.arange(1, n + 1), min(args.sample, n), replace=False))
            sample = pyogrio.read_dataframe(raw, layer=layer, where=f"fid IN ({','.join(map(str, fids))})")
            geoms = np.asarray(sample.geometry.array)
            usable = ~shapely.is_missing(geoms) & ~shapely.is_empty(geoms)
            sample, geoms = sample[usable], geoms[usable]
            kept_rows = pyogrio.read_dataframe(final, layer=layer, read_geometry=False,
                                               columns=id_columns(final, layer))
            kept = np.isin(feature_key(sample), feature_key(kept_rows))
            d = distance(geoms)
            wrong_keep = int((kept & (d > limit + TOLERANCE_M)).sum())
            wrong_drop = int((~kept & (d < limit - TOLERANCE_M)).sum())
            report(f"exactness {layer}", wrong_keep == 0 and wrong_drop == 0,
                   f"{len(sample)} sampled, {int(kept.sum())} kept; "
                   f"{wrong_keep} kept but > {miles} mi, {wrong_drop} dropped but < {miles} mi")
    else:
        info("exactness", "skipped: temporary files were cleaned up "
             "(run filter_near_runs.py with --keep-work to keep them)")

    # pass2 + states
    polys = pyogrio.read_dataframe(final, layer="multipolygons")
    big_ids, pass1 = work / "big_ids.txt", work / "pass1.osm.pbf"
    if big_ids.exists() and pass1.exists() and big_ids.stat().st_size:
        res = subprocess.run(["osmium", "getid", "-v", "--verbose-ids", "-i", str(big_ids), str(pass1),
                              "-o", "/dev/null", "-f", "pbf", "--overwrite"], capture_output=True, text=True)
        missing_ways = set(re.findall(r"\d+", " ".join(re.findall(r"Missing way IDs:(.*)", res.stderr))))
        missing_rels = set(re.findall(r"\d+", " ".join(re.findall(r"Missing relation IDs:(.*)", res.stderr))))
        only2 = polys[polys["osm_id"].isin(missing_rels) | polys["osm_way_id"].isin(missing_ways)]
        report("pass2", len(missing_ways) + len(missing_rels) == 0 or len(only2) > 0,
               f"{len(missing_ways) + len(missing_rels)} large areas were missed by pass 1; "
               f"{len(only2)} of them are in the final multipolygons layer")
        cols = [c for c in ("name", "natural", "landuse", "boundary", "admin_level") if c in only2]
        if len(only2):
            print(only2[cols].fillna("").head(10).to_string(index=False))
        woods = only2[(only2["natural"] == "wood") | (only2["landuse"] == "forest")]
        if len(woods):
            covered = shapely.STRtree(runs).query(np.asarray(woods.geometry.array), predicate="intersects")
            info("pass2 woods", f"{len(woods)} forest/wood areas came only from pass 2; "
                                f"they touch {len(np.unique(covered[1]))} runs")
    else:
        info("pass2", "skipped: no pass 2 areas, or temporary files were cleaned up "
             "(run filter_near_runs.py with --keep-work to keep them)")

    states = polys[(polys["boundary"] == "administrative") & (polys["admin_level"] == "4")]
    report("states", len(states) > 0, f"{len(states)} state/province polygons: "
                                      + ", ".join(sorted(states["name"].dropna())[:15]))

    # refs
    res = subprocess.run(["osmium", "check-refs", str(out / "near_runs.osm.pbf")],
                         capture_output=True, text=True)
    m = re.search(r"Nodes in ways missing:\s*(\d+)", res.stdout + res.stderr)
    missing_nodes = int(m.group(1)) if m else -1
    report("refs", missing_nodes == 0, f"{missing_nodes} way nodes missing in near_runs.osm.pbf"
           if m else (res.stdout + res.stderr).strip()[:200])

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
