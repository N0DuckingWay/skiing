#!/usr/bin/env python3
"""Keep downhill ski runs plus every OSM feature with any part within 10 miles of one.

Reads the region files kept current by download_osm.py and writes, under <data-dir>/out/:
    runs.osm.pbf               downhill runs (raw OSM format)
    runs.gpkg                  downhill runs as map layers
    zone_10mi.gpkg             the 10-mile zone around all runs
    near_runs.osm.pbf          everything near runs (raw OSM; reaches slightly past 10 mi)
    features_near_runs.gpkg    every feature with any part within 10 mi, as map layers
    manifest.json              OSM snapshot, settings and counts

Steps:
    merged     merge the US and Canada files (temporary), so border features stay whole
    runs       pull downhill runs out of the merged file
    zones      draw the exact 10-mile zone and a coarse 11-mile zone around runs
    pass1      take everything with a point inside the coarse zone
    areas      pick out candidate large areas
    big_ids    find large areas that overlap the zone without a point inside it
               (national forests, big forests, states around inland resorts)
    pass2      pull those large areas out complete
    near       combine passes 1 and 2
    raw_gpkg   convert to GeoPackage layers
    features   trim to exactly 10 miles
    runs_gpkg  convert runs to GeoPackage layers

A result counts as up to date if it is newer than the downloaded data, so
rerunning only does what's missing. Temporary files are deleted as soon as
every step that needs them has finished (--keep-work keeps them).

Usage:
    python osm_data/filter_near_runs.py --data-dir "/path/to/osm_data/data"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pyogrio
import shapely
from shapely.errors import GEOSException

from common import (
    DEFAULT_DATA_DIR,
    OSMCONF,
    finish,
    human_size,
    log,
    now_iso,
    read_json,
    require_tools,
    run,
    setup_logging,
    snapshot_time,
    tmp_path,
    write_json,
)

METERS_PER_MILE = 1609.344

# Which runs define the zones.
RUN_FILTER = "nwr/piste:type=downhill"

# Candidate large areas for pass 2: every multipolygon/boundary relation, plus
# closed ways with area-type keys. Buildings are left out on purpose: they are
# small, so pass 1 already catches any that are near a run.
AREA_FILTERS = [
    "r/type=multipolygon,boundary",
    "a/landuse,natural,landcover,leisure,boundary,place,military,water,waterway,"
    "aeroway,tourism,historic,man_made,amenity,glacier",
]

SIMPLIFY_DEG = 0.001        # coarse-zone simplification; moves edges by at most ~111 m
SIMPLIFY_SLACK_M = 250      # safety allowance for that simplification
TILE_DEG = 0.25             # zone is cut into tiles this size so intersect tests stay fast
BATCH_ROWS = 20_000         # rows per batch when streaming osmium export
CHUNK_ROWS = 500_000        # rows per chunk when trimming GeoPackage layers


@dataclass
class Paths:
    data: Path
    sources: list[Path]     # downloaded region files (or a single --input file)

    @classmethod
    def build(cls, data_dir: Path, input_pbf: Path | None) -> "Paths":
        data_dir = data_dir.resolve()
        if input_pbf:
            sources = [input_pbf.resolve()]
        else:
            manifest = read_json(data_dir / "raw" / "manifest.json")
            if not manifest:
                sys.exit(f"No downloaded data in {data_dir / 'raw'}. Run osm_data/download_osm.py first.")
            sources = [data_dir / "raw" / e["file"] for e in manifest["regions"]]
        missing = [str(s) for s in sources if not s.exists()]
        if missing:
            sys.exit(f"Missing input: {', '.join(missing)}. Run osm_data/download_osm.py first.")
        p = cls(data=data_dir, sources=sources)
        p.work.mkdir(parents=True, exist_ok=True)
        p.out.mkdir(parents=True, exist_ok=True)
        return p

    work = property(lambda s: s.data / "work")
    out = property(lambda s: s.data / "out")

    @property
    def merged(self) -> Path:
        """The single file every step reads: the merged regions, or the only source."""
        return self.sources[0] if len(self.sources) == 1 else self.work / "merged.osm.pbf"

    # final outputs
    runs_pbf = property(lambda s: s.out / "runs.osm.pbf")
    runs_gpkg = property(lambda s: s.out / "runs.gpkg")
    zone_gpkg = property(lambda s: s.out / "zone_10mi.gpkg")
    near_pbf = property(lambda s: s.out / "near_runs.osm.pbf")
    features = property(lambda s: s.out / "features_near_runs.gpkg")
    manifest = property(lambda s: s.out / "manifest.json")
    # intermediates
    zone_coarse = property(lambda s: s.work / "zone_coarse.geojson")
    tiles = property(lambda s: s.work / "zone_10mi_tiles.gpkg")
    pass1 = property(lambda s: s.work / "pass1.osm.pbf")
    areas = property(lambda s: s.work / "areas.osm.pbf")
    big_ids = property(lambda s: s.work / "big_ids.txt")
    pass2 = property(lambda s: s.work / "pass2.osm.pbf")
    near_raw = property(lambda s: s.work / "near_runs_raw.gpkg")


# ---------------------------------------------------------------- helpers

def update_manifest(p: Paths, **values) -> None:
    data = read_json(p.manifest) or {}
    data.update(values)
    data["updated"] = now_iso()
    write_json(p.manifest, data)


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def stream_export(pbf: Path, extra_args: list, stderr=None):
    """Stream `osmium export -f pg` output in batches.

    Each line is: hex WKB geometry <tab> object type <tab> OSM id <tab> tags JSON.
    Yields (geometries, types, ids) per batch.
    """
    cmd = [str(c) for c in ["osmium", "export", pbf, "-f", "pg", "-a", "type,id", "-o", "-", *extra_args]]
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1 << 20)
    hexes, types, ids = [], [], []
    try:
        for line in proc.stdout:
            geom, otype, oid, _ = line.split("\t", 3)
            hexes.append(geom)
            types.append(otype)
            ids.append(oid)
            if len(hexes) >= BATCH_ROWS:
                yield shapely.from_wkb(np.asarray(hexes, dtype=object)), types, ids
                hexes, types, ids = [], [], []
        if hexes:
            yield shapely.from_wkb(np.asarray(hexes, dtype=object)), types, ids
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd)


def as_multipolygon(geom) -> shapely.MultiPolygon:
    """Keep only the polygon parts of a geometry, as one MultiPolygon."""
    parts = shapely.get_parts(geom)
    polys = [g for g in parts if g.geom_type == "Polygon"]
    polys += [q for g in parts if g.geom_type == "MultiPolygon" for q in g.geoms]
    return shapely.MultiPolygon(polys)


def buffer_union(geoms: np.ndarray, meters: float):
    """Union of buffers around `geoms` (EPSG:4326), measured in true metres.

    Each run is buffered in the UTM zone of its centroid. A single projection
    for the whole continent would distort distances far from its centre.
    """
    lon = shapely.get_x(shapely.centroid(geoms))
    epsg = 32600 + np.clip(np.floor((lon + 180) / 6).astype(int) + 1, 1, 60)  # northern UTM
    pieces = []
    for code in np.unique(epsg):
        local = gpd.GeoSeries(geoms[epsg == code], crs=4326).to_crs(int(code))
        buffers = np.asarray(local.buffer(meters, resolution=16).array)  # 16 segments per quarter circle
        # GEOS sometimes leaves a small spurious hole inside one run's buffer.
        # A real hole would need a run that circles an area 2x the buffer wide,
        # which no ski run does, so keep only each buffer's outer ring.
        filled = shapely.polygons(shapely.get_exterior_ring(shapely.get_parts(buffers)))
        merged = shapely.union_all(filled)
        pieces.append(gpd.GeoSeries([merged], crs=int(code)).to_crs(4326).iloc[0])
    return as_multipolygon(shapely.make_valid(shapely.union_all(pieces)))


def make_tiles(zone, size: float) -> np.ndarray:
    """Cut the zone into grid tiles so each intersect test touches few vertices."""
    out = []
    for part in shapely.get_parts(zone):
        x0, y0, x1, y1 = part.bounds
        xs = np.arange(math.floor(x0 / size) * size, x1, size)
        ys = np.arange(math.floor(y0 / size) * size, y1, size)
        gx, gy = (a.ravel() for a in np.meshgrid(xs, ys))
        boxes = shapely.box(gx, gy, gx + size, gy + size)
        shapely.prepare(part)
        boxes = boxes[shapely.intersects(part, boxes)]
        out.append(shapely.intersection(part, boxes))
    tiles = shapely.get_parts(np.concatenate(out))
    keep = (shapely.get_type_id(tiles) == 3) & (shapely.area(tiles) > 0)  # 3 = Polygon
    return tiles[keep]


def load_tiles(p: Paths) -> shapely.STRtree:
    tiles = gpd.read_file(p.tiles)
    return shapely.STRtree(np.asarray(tiles.geometry.array))


def intersecting(tree: shapely.STRtree, geoms: np.ndarray) -> np.ndarray:
    """Indices of `geoms` that intersect any zone tile."""
    if len(geoms) == 0:
        return np.array([], dtype=int)
    try:
        hit, _ = tree.query(geoms, predicate="intersects")
    except GEOSException:
        hit, _ = tree.query(shapely.make_valid(geoms), predicate="intersects")
    return np.unique(hit)


def bbox_diagonal_m(bounds: np.ndarray) -> np.ndarray:
    """Approximate bounding-box diagonal in metres.

    Uses the latitude closest to the equator, so it never underestimates.
    """
    x0, y0, x1, y1 = bounds.T
    lat = np.minimum(np.abs(y0), np.abs(y1))
    dx = (x1 - x0) * 111_320 * np.cos(np.radians(lat))
    dy = (y1 - y0) * 110_574
    return np.hypot(dx, dy)


def osm_to_gpkg(src: Path, dst: Path, tmpdir: Path, where: str | None = None) -> None:
    """Convert an OSM file to GeoPackage layers with GDAL, in one pass."""
    cmd = [
        "ogr2ogr", "-f", "GPKG", dst, src,
        "-oo", f"CONFIG_FILE={OSMCONF}",
        "-oo", "TAGS_FORMAT=JSON",
        "-oo", "MAX_TMPFILE_SIZE=4000",
        "-oo", "COMPRESS_NODES=YES",
        "--config", "CPL_TMPDIR", tmpdir,
        "-gt", "65536",
    ]
    if where:
        cmd += ["-where", where]
    run(cmd)


def fid_range(gpkg: Path, layer: str) -> tuple[int | None, int | None]:
    with sqlite3.connect(gpkg) as con:
        return con.execute(f'SELECT MIN(fid), MAX(fid) FROM "{layer}"').fetchone()


# ---------------------------------------------------------------- steps

def build_merged(p: Paths) -> None:
    snapshot = snapshot_time(p.sources[0])
    tmp = tmp_path(p.merged)
    run(["osmium", "merge", *p.sources, "-o", tmp, "-f", "pbf", "--overwrite",
         f"--output-header=osmosis_replication_timestamp={snapshot}"])
    finish(tmp, p.merged)


def build_runs(p: Paths) -> None:
    tmp = tmp_path(p.runs_pbf)
    run(["osmium", "tags-filter", p.merged, RUN_FILTER, "-o", tmp, "-f", "pbf", "--overwrite"])
    finish(tmp, p.runs_pbf)


def build_zones(p: Paths, miles: float, margin_miles: float) -> None:
    parts = []
    # Lines and areas. -n keeps untagged ways that belong to piste route relations.
    for geoms, _, _ in stream_export(p.runs_pbf, ["-n", "--geometry-types=linestring,polygon"]):
        parts.append(geoms)
    # Tagged points (a few runs are mapped as a single node).
    for geoms, _, _ in stream_export(p.runs_pbf, ["--geometry-types=point"]):
        parts.append(geoms)
    if not parts:
        sys.exit(f"No downhill runs found in {p.runs_pbf}")
    runs = np.concatenate(parts)
    log.info("buffering %d run geometries", len(runs))

    exact = buffer_union(runs, miles * METERS_PER_MILE)
    coarse = buffer_union(runs, (miles + margin_miles) * METERS_PER_MILE)
    coarse = as_multipolygon(shapely.simplify(coarse, SIMPLIFY_DEG, preserve_topology=True))
    tiles = make_tiles(exact, TILE_DEG)
    log.info("zone: %d separate areas, %d tiles; coarse zone has %d vertices",
             len(exact.geoms), len(tiles), shapely.get_num_coordinates(coarse))

    tmp = tmp_path(p.zone_coarse)
    feature = {"type": "Feature", "properties": {}, "geometry": json.loads(shapely.to_geojson(coarse))}
    tmp.write_text(json.dumps(feature))
    finish(tmp, p.zone_coarse)

    tmp = tmp_path(p.zone_gpkg)
    gpd.GeoDataFrame({"miles": [miles]}, geometry=[exact], crs=4326).to_file(tmp, layer="zone", driver="GPKG")
    finish(tmp, p.zone_gpkg)

    tmp = tmp_path(p.tiles)
    gpd.GeoDataFrame(geometry=tiles, crs=4326).to_file(tmp, layer="tiles", driver="GPKG")
    finish(tmp, p.tiles)

    update_manifest(p, run_geometries=int(len(runs)), zone_areas=len(exact.geoms))


def build_pass1(p: Paths) -> None:
    tmp = tmp_path(p.pass1)
    run(["osmium", "extract", "-s", "smart", "-S", "types=multipolygon,boundary",
         "-p", p.zone_coarse, p.merged, "-o", tmp, "-f", "pbf", "--overwrite"])
    finish(tmp, p.pass1)


def build_areas(p: Paths) -> None:
    tmp = tmp_path(p.areas)
    run(["osmium", "tags-filter", p.merged, *AREA_FILTERS, "-o", tmp, "-f", "pbf", "--overwrite"])
    finish(tmp, p.areas)


def build_big_ids(p: Paths, margin_miles: float) -> None:
    """Find large areas that overlap the exact zone.

    An area smaller than the margin that touches the zone must have a node
    inside the coarse zone, so pass 1 already has it complete. Only larger
    areas need the exact test.
    """
    cutoff_m = margin_miles * METERS_PER_MILE - SIMPLIFY_SLACK_M
    tree = load_tiles(p)
    index = p.work / "nodes.idx"
    errors = p.work / "area_errors.log"
    found: set[str] = set()
    total = large = 0
    next_report = 5_000_000

    with errors.open("w") as err:
        args = ["--geometry-types=polygon", "-e", "-i", f"sparse_file_array,{index}"]
        for geoms, types, ids in stream_export(p.areas, args, stderr=err):
            total += len(geoms)
            big = np.flatnonzero(bbox_diagonal_m(shapely.bounds(geoms)) >= cutoff_m)
            large += len(big)
            for i in big[intersecting(tree, geoms[big])]:
                found.add(f"{types[i][0]}{ids[i]}")  # w123 / r123 for osmium getid
            if total >= next_report:
                log.info("  %d areas read, %d large, %d overlap the zone", total, large, len(found))
                next_report += 5_000_000
    index.unlink(missing_ok=True)

    broken = sum(1 for _ in errors.open())
    log.info("pass 2: %d areas read, %d large, %d overlap the zone; %d geometry errors (see %s)",
             total, large, len(found), broken, errors.name)

    tmp = tmp_path(p.big_ids)
    tmp.write_text("".join(f"{x}\n" for x in sorted(found)))
    finish(tmp, p.big_ids)
    update_manifest(p, pass2={"areas_read": total, "large_areas": large,
                              "overlapping": len(found), "geometry_error_lines": broken})


def build_pass2(p: Paths) -> None:
    """Pull the large areas out of the merged file, complete with all members and nodes."""
    tmp = tmp_path(p.pass2)
    if p.big_ids.stat().st_size == 0:
        log.info("no large areas found for pass 2")
        # An empty OSM file, so the next step can always merge both passes.
        run(["osmium", "tags-filter", "-R", p.runs_pbf, "n/osm_data_matches_nothing",
             "-o", tmp, "-f", "pbf", "--overwrite"])
        finish(tmp, p.pass2)
        return

    getid_log = p.work / "getid.log"
    cmd = [str(c) for c in ["osmium", "getid", "-v", "--verbose-ids", "-r", "-i", p.big_ids,
                            p.merged, "-o", tmp, "-f", "pbf", "--overwrite"]]
    log.info("$ %s", " ".join(cmd))
    with getid_log.open("w") as out:
        code = subprocess.run(cmd, stderr=out).returncode
    # Exit code 1 also means "some referenced objects weren't in the file",
    # e.g. a node just past the edge of Geofabrik's extract. That's normal.
    missing = re.search(r"Did not find (\d+) object", getid_log.read_text())
    if code == 1 and missing and tmp.exists():
        log.warning("pass 2: %s referenced objects are not in the input file (normal at "
                    "the edge of an extract; IDs listed in %s)", missing.group(1), getid_log.name)
    elif code != 0:
        sys.exit(f"osmium getid failed (exit {code}); see {getid_log}")
    finish(tmp, p.pass2)


def build_near(p: Paths) -> None:
    tmp = tmp_path(p.near_pbf)
    run(["osmium", "merge", p.pass1, p.pass2, "-o", tmp, "-f", "pbf", "--overwrite"])
    finish(tmp, p.near_pbf)


def build_raw_gpkg(p: Paths) -> None:
    tmp = tmp_path(p.near_raw)
    osm_to_gpkg(p.near_pbf, tmp, p.work)
    finish(tmp, p.near_raw)


def build_features(p: Paths) -> None:
    """Keep only features with some part inside the exact 10-mile zone."""
    tree = load_tiles(p)
    tmp = tmp_path(p.features)
    counts = {}
    for layer in pyogrio.list_layers(p.near_raw)[:, 0]:
        lo, hi = fid_range(p.near_raw, layer)
        geometry_type = pyogrio.read_info(p.near_raw, layer=layer)["geometry_type"]
        seen = kept = 0
        if lo is not None:
            for start in range(lo, hi + 1, CHUNK_ROWS):
                df = pyogrio.read_dataframe(
                    p.near_raw, layer=layer, where=f"fid >= {start} AND fid < {start + CHUNK_ROWS}")
                seen += len(df)
                if df.empty:
                    continue
                geoms = np.asarray(df.geometry.array)
                usable = np.flatnonzero(~shapely.is_missing(geoms) & ~shapely.is_empty(geoms))
                keep = usable[intersecting(tree, geoms[usable])]
                if len(keep):
                    pyogrio.write_dataframe(df.iloc[keep], tmp, layer=layer, driver="GPKG",
                                            geometry_type=geometry_type, append=kept > 0)
                    kept += len(keep)
        counts[layer] = {"read": seen, "kept": kept}
        log.info("  %-16s kept %d of %d", layer, kept, seen)
    if not tmp.exists():
        sys.exit("No features fell inside the zone; something went wrong upstream.")
    finish(tmp, p.features)
    update_manifest(p, layers=counts)


def build_runs_gpkg(p: Paths) -> None:
    tmp = tmp_path(p.runs_gpkg)
    # GDAL turns ":" into "_" in column names, so piste:type is piste_type.
    osm_to_gpkg(p.runs_pbf, tmp, p.work, where="piste_type = 'downhill'")
    finish(tmp, p.runs_gpkg)


# ---------------------------------------------------------------- orchestration

@dataclass
class Target:
    name: str
    build: Callable[[], None] | None     # None: an input file we never build or delete
    products: Callable[[], list[Path]]
    deps: list[str] = field(default_factory=list)
    keep: bool = False                    # keep after the run (results and small files)


class Pipeline:
    """Builds targets in dependency order, like `make`.

    A target is up to date if all its files exist and are newer than the
    downloaded data. Temporary targets are deleted once everything that reads
    them is up to date, so the whole run never holds every intermediate at once.
    """

    def __init__(self, targets: list[Target], goals: list[str], sources: list[Path],
                 force: bool, keep_work: bool):
        self.targets = {t.name: t for t in targets}
        self.goals = goals
        self.sources_mtime = max(s.stat().st_mtime for s in sources)
        self.force = force
        self.keep_work = keep_work
        self.built: set[str] = set()
        self.readers = {n: [t.name for t in targets if n in t.deps] for n in self.targets}

    def needed(self, name: str) -> bool:
        """True if this target still has to be built for some goal."""
        if self.fresh(name):
            return False
        return name in self.goals or any(self.needed(r) for r in self.readers[name])

    def fresh(self, name: str) -> bool:
        t = self.targets[name]
        if t.build is None or name in self.built:
            return True
        if self.force:
            return False
        return all(f.exists() and f.stat().st_mtime > self.sources_mtime for f in t.products())

    def ensure(self, name: str) -> None:
        if self.fresh(name):
            return
        t = self.targets[name]
        for dep in t.deps:
            self.ensure(dep)
        for f in t.products():
            tmp_path(f).unlink(missing_ok=True)  # leftovers from a crashed run
        log.info("== %s", name)
        started = time.time()
        t.build()
        self.built.add(name)
        log.info("== %s took %.1f min", name, (time.time() - started) / 60)
        if not self.keep_work:
            self.clean_up()

    def clean_up(self) -> None:
        for name, t in self.targets.items():
            if t.build is None:
                continue
            for f in t.products():
                tmp_path(f).unlink(missing_ok=True)  # half-written files from an interrupted run
            if t.keep:
                continue
            # Safe to delete once nothing that reads it still has to be built.
            if not any(self.needed(r) for r in self.readers[name]):
                for f in t.products():
                    if f.exists():
                        size = f.stat().st_size
                        f.unlink()
                        log.info("deleted %s (%s, no longer needed)", f.name, human_size(size))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help=f"where data is stored (default: {DEFAULT_DATA_DIR})")
    ap.add_argument("--input", type=Path,
                    help="use this one OSM file instead of the downloaded regions (for testing)")
    ap.add_argument("--miles", type=float, default=10.0, help="distance from runs (default: 10)")
    ap.add_argument("--margin-miles", type=float, default=1.0,
                    help="extra margin for the coarse first pass (default: 1)")
    ap.add_argument("--force", action="store_true", help="rebuild everything")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep temporary files (needed for the full set of checks in check_outputs.py)")
    args = ap.parse_args()

    setup_logging()
    require_tools("osmium", "ogr2ogr")
    if args.margin_miles * METERS_PER_MILE <= 2 * SIMPLIFY_SLACK_M:
        sys.exit("--margin-miles is too small; use at least 0.5.")
    p = Paths.build(args.data_dir, args.input)

    snapshots = {snapshot_time(s) for s in p.sources}
    if len(snapshots) != 1:
        sys.exit("The downloaded regions are at different snapshot times. Run osm_data/download_osm.py again.")
    snapshot = snapshots.pop()

    settings = {
        "miles": args.miles,
        "margin_miles": args.margin_miles,
        "run_filter": RUN_FILTER,
        "area_filters": AREA_FILTERS,
        "osmconf_md5": hashlib.md5(OSMCONF.read_bytes()).hexdigest(),
        "sources": [s.name for s in p.sources],
    }
    previous = read_json(p.manifest)
    if previous and previous.get("settings") != settings and not args.force:
        sys.exit("Settings changed since the last run. Rerun with --force to rebuild everything.")

    single = len(p.sources) == 1
    targets = [
        Target("merged", None if single else (lambda: build_merged(p)), lambda: [p.merged]),
        Target("runs", lambda: build_runs(p), lambda: [p.runs_pbf], ["merged"], keep=True),
        Target("zones", lambda: build_zones(p, args.miles, args.margin_miles),
               lambda: [p.zone_coarse, p.tiles, p.zone_gpkg], ["runs"], keep=True),
        Target("pass1", lambda: build_pass1(p), lambda: [p.pass1], ["merged", "zones"]),
        Target("areas", lambda: build_areas(p), lambda: [p.areas], ["merged"]),
        Target("big_ids", lambda: build_big_ids(p, args.margin_miles), lambda: [p.big_ids],
               ["areas", "zones"], keep=True),
        Target("pass2", lambda: build_pass2(p), lambda: [p.pass2], ["big_ids", "merged", "runs"]),
        Target("near", lambda: build_near(p), lambda: [p.near_pbf], ["pass1", "pass2"], keep=True),
        Target("raw_gpkg", lambda: build_raw_gpkg(p), lambda: [p.near_raw], ["near"]),
        Target("features", lambda: build_features(p), lambda: [p.features], ["raw_gpkg", "zones"], keep=True),
        Target("runs_gpkg", lambda: build_runs_gpkg(p), lambda: [p.runs_gpkg], ["runs"], keep=True),
    ]
    goals = ["features", "runs_gpkg", "zones"]
    pipeline = Pipeline(targets, goals, p.sources, args.force, args.keep_work)
    if all(pipeline.fresh(g) for g in goals):
        if not args.keep_work:
            pipeline.clean_up()  # leftovers from an interrupted run
        log.info("everything is up to date (use --force to rebuild)")
        return

    update_manifest(p, sources=[str(s) for s in p.sources], input_snapshot=snapshot,
                    settings=settings, status="running", started=now_iso())
    for goal in goals:
        pipeline.ensure(goal)
    update_manifest(p, status="complete", finished=now_iso())

    log.info("done. results in %s", p.out)
    log.info("disk use: raw %s, work %s, out %s", human_size(dir_size(p.data / "raw")),
             human_size(dir_size(p.work)), human_size(dir_size(p.out)))


if __name__ == "__main__":
    main()
