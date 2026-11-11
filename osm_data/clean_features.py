#!/usr/bin/env python3
"""Keep only skiing and natural features from the output of filter_near_runs.py.

First cleaning step. Reads <data-dir>/out/features_near_runs.gpkg and writes, under <data-dir>/out/:
    ski_nature.gpkg    the same layers and columns, with only the kept features
    ski_nature.json    input snapshot, rules hash, and counts per layer and per rule

A feature is kept if any rule in general_rules() matches it:
    natural      any natural=* tag (woods, peaks, trees, cliffs, ...) except water bodies
                 (natural=water/bay/strait/lake/pond)
    piste        any piste:* tag
    lifts        any aerialway tag (lifts, stations, pylons, proposed/abandoned lifts), funiculars
    ski_areas    landuse=winter_sports, site=piste, sport=*ski*, ski_resort=*
    ski_access   ski=* / xc_ski=* / ski:*=* tags, unless the value is "no"
    ski_pois     ski shops, ski rental and ski schools, alpine and wilderness huts, viewpoints
    ski_tags     any other tag whose value says skiing: ski jumps, snow guns, lift buildings,
                 snowboard and biathlon areas, abandoned ski areas, "piste" in a value, ...
    ski_trails   paths and tracks whose name says they are ski trails
    cutlines     man_made=cutline: straight clearings through forest
    vegetation   landuse=forest/meadow/grass, vegetation landcover, leaf_type, wood, wetland
                 types (buildings excluded)
    waterways    streams, rivers, rapids, waterfalls, flowlines, canals, ditches; lines and
                 points only, so river areas are dropped with the other water

Clearcuts are not forest: a clearcut also tagged as forest isn't kept by the nature
rules, and clearcut shapes are cut out of the forest polygons that overlap them.

Route relations (rows with a route tag in the multilinestrings and other_relations
layers) are judged only by the ski_routes rule: their piste:type must include a ski
type (anything not in NON_SKI_PISTE_TYPES); with no piste:type, the route itself must
be a ski, piste or aerialway route, carry ski access tags, or be named as a ski trail.

Rerunning only rebuilds when the input is newer or the rules have changed (--force
rebuilds anyway). --report prints how many features each rule matches, without
writing anything.

Usage:
    python osm_data/clean_features.py --data-dir "/path/to/osm_data/data"
    python osm_data/clean_features.py --data-dir "/path/to/osm_data/data" --report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import (
    DEFAULT_DATA_DIR,
    finish,
    human_size,
    log,
    now_iso,
    read_json,
    require_tools,
    setup_logging,
    tmp_path,
    write_json,
)

# Layers whose rows with a route tag are route relations, filtered by the ski-route rule only.
ROUTE_LAYERS = {"multilinestrings", "other_relations"}

# piste:type values that are not skiing. A route is kept if its piste:type has any
# other value (downhill, nordic, skitour, connection, traverse, ...).
NON_SKI_PISTE_TYPES = [
    "hike", "snowshoe", "sled", "sleigh", "toboggan", "fatbike", "fat_bike",
    "ice_skate", "snow_park", "playground", "snowmobile",
]

# Water bodies are dropped: these natural=* values don't count for the natural rule, and
# water=* tags alone keep nothing. Points for them (lake and bay labels) go too.
WATER_NATURAL = ["water", "bay", "strait", "lake", "pond"]
# Rivers and streams are kept as lines only. Layers here never use the waterways rule, so
# river areas (waterway=canal/rapids/... polygons) are dropped with the other water.
AREA_LAYERS = {"multipolygons"}
WATERWAYS = ["stream", "river", "rapids", "waterfall", "flowline", "canal", "ditch"]
VEGETATION_LANDUSE = ["forest", "meadow", "grass"]
# Matched as substrings of landcover=*, so "trees", "raised_grass" and "Lawn" all count.
VEGETATION_LANDCOVER = ["tree", "grass", "scrub", "shrub", "green", "lawn"]

# A tag value says skiing if it has the word "ski" or "skiing" (ski_jump, ski_lift, rental=ski,
# "Downhill Ski Run"; not water skiing), or contains one of these.
SKI_VALUE_WORDS = ["piste", "nordic", "snowboard", "biathlon", "snow_cannon", "snowmaking",
                   "winter_sports"]
# Keys holding free text, addresses, contacts or owners: a ski word there says nothing about the
# feature (an architect named Skidmore, a bus stop on Ski Hill Road, "no access @ winter").
TEXT_KEYS = ["%name%", "addr:%", "%description%", "note%", "fixme%", "%email%", "%website%", "%url%",
             "contact:%", "phone%", "image%", "%wiki%", "brand%", "operator%", "owner%", "%architect%",
             "source%", "inscription%", "comment%", "delivery%", "opening_hours%", "destination%",
             "from", "to", "via", "branch", "massgis:%", "tiger:%", "gnis:%", "ref%", "%ref", "cuisine",
             "%conditional%", "%location%", "%ssid%", "commemorates", "surveillance%", "fee%",
             "restriction%"]
# Columns that aren't descriptive tags, so they're never checked for ski words.
NOT_TAG_COLUMNS = {"fid", "geom", "osm_id", "osm_way_id", "other_tags", "z_order", "name", "ref",
                   "address", "is_in", "ele", "admin_level", "land_area"}
# Trails (not roads) whose name says they are ski trails: the word "ski" or one of these,
# but not a road name ("Ski Hill Road", "Ski Area Access") or a snowmobile trail ("Ski-Doo").
SKI_TRAIL_HIGHWAYS = ["path", "track", "footway", "bridleway"]
SKI_TRAIL_ROUTES = ["hiking", "foot", "walking", "horse", "running"]
SKI_TRAIL_NAME_WORDS = ["nordic", "ski de fond", "piste de ski", "cat track", "cattrack", "skiway", "xcski"]
NOT_SKI_TRAIL_NAME_WORDS = ["road", "rd", "drive", "street", "avenue", "ave", "boulevard", "blvd",
                            "access", "resort", "doo"]

# Clearcuts are not forest. A clearcut that is also tagged as forest isn't kept by the nature
# rules, and clearcut shapes are cut out of the forest polygons that overlap them (see cut_clearcuts).
FOREST_LANDUSE = ["forest", "forestry"]
NATURE_RULES = ["natural", "vegetation"]
CLEARCUT_LAYER = "multipolygons"
# Equal-area projection for North America, for reporting how much forest was cut.
EQUAL_AREA_CRS = "ESRI:102008"
# wetland=* on its own (most wetlands already have natural=wetland). Only wetland types.
WETLANDS = ["marsh", "swamp", "bog", "fen", "reedbed", "wet_meadow", "saltmarsh", "tidalflat",
            "mangrove", "string_bog", "palsa_bog"]
SKI_AMENITIES = ["ski_rental", "ski_school", "ski_parking"]
HUTS_AND_VIEWS = ["alpine_hut", "wilderness_hut", "viewpoint"]


def sql_list(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Layer:
    """Builds SQL for one layer, whose tags are either real columns or keys in other_tags (JSON)."""

    def __init__(self, name: str, columns: list[str]):
        self.name = name
        self.columns = set(columns)

    def tag(self, key: str) -> str:
        """SQL for the value of OSM tag `key` (NULL if absent).

        GDAL turns ":" into "_" in column names, so piste:type is the column piste_type.
        Tags without a column are looked up in other_tags; the LIKE check first skips
        the JSON parse for the vast majority of rows that don't have the key.

        other_tags in features_near_runs.gpkg is written with single quotes
        ({'sport': 'skiing'}), not strict JSON. SQLite 3.42+ reads that as JSON5, but
        it means the LIKE check must look for the bare key, not "key".
        """
        column = key.replace(":", "_")
        if column in self.columns:
            return f'"{column}"'
        return (f"(CASE WHEN other_tags LIKE '%{key}%' "
                f"THEN json_extract(other_tags, '$.\"{key}\"') END)")

    @staticmethod
    def any_key(word: str, key_sql: str, value_sql: str | None = None) -> str:
        """True if some key in other_tags matches `key_sql` (and its value `value_sql`).

        `word` must appear in every matching key; it is a cheap pre-check before parsing JSON.
        """
        cond = f"({key_sql})" + (f" AND ({value_sql})" if value_sql else "")
        return (f"(other_tags LIKE '%{word}%' AND EXISTS "
                f"(SELECT 1 FROM json_each(other_tags) WHERE {cond}))")

    def any_column(self, prefix: str) -> str:
        cols = sorted(c for c in self.columns if c.startswith(prefix))
        return " OR ".join(f'"{c}" IS NOT NULL' for c in cols) or "0"

    def any_tag_says_ski(self) -> str:
        """True if a descriptive tag's value says skiing (see says_ski), in a column or other_tags."""
        columns = ["(" + says_ski('"' + c + '"') + ")" for c in sorted(self.columns - NOT_TAG_COLUMNS)]
        words = ["ski", *SKI_VALUE_WORDS]
        prefilter = " OR ".join(f"other_tags LIKE '%{w}%'" for w in words)
        in_other_tags = (f"(({prefilter}) AND EXISTS (SELECT 1 FROM json_each(other_tags) "
                         f"WHERE NOT ({is_text_key('key')}) AND ({says_ski('value')})))")
        return " OR ".join([*columns, in_other_tags])

    # COALESCE: a missing tag makes these comparisons NULL, and NOT NULL would drop the row.
    def is_clearcut(self) -> str:
        return (f"COALESCE({self.tag('man_made')} = 'clearcut' OR {self.tag('landuse')} = 'logging' "
                f"OR lower({self.tag('landcover')}) LIKE '%clear%cut%', 0)")

    def is_forest(self) -> str:
        return (f"COALESCE({self.tag('landuse')} IN ({sql_list(FOREST_LANDUSE)}) "
                f"OR {self.tag('natural')} = 'wood' OR lower({self.tag('landcover')}) LIKE '%tree%', 0)")


def says_ski(value: str) -> str:
    """SQL: true if `value` has the word ski/skiing (not water skiing) or a SKI_VALUE_WORDS entry.

    Words are split on spaces, ";", "_" and ":", so ski_jump and "ski;snowboard" count but
    "Kowalski" and "skillion" don't. LIKE treats "_" as any character, so snow_cannon also
    matches "snow cannon".
    """
    words = f"(' ' || replace(replace(replace(lower({value}), ';', ' '), '_', ' '), ':', ' ') || ' ')"
    ski = (f"({value} LIKE '%ski%' AND ({words} LIKE '% ski %' OR {words} LIKE '% skiing %') "
           f"AND {value} NOT LIKE '%water%ski%')")
    return " OR ".join([ski, *(f"{value} LIKE '%{w}%'" for w in SKI_VALUE_WORDS)])


def is_text_key(key: str) -> str:
    return " OR ".join(f"{key} LIKE '{p}'" for p in TEXT_KEYS)


def names_ski_trail(name: str) -> str:
    """SQL: true if a trail name says it's a ski trail ("X-C Ski Trail", "Nordic Loop", "Cat Track")."""
    words = f"(' ' || replace(replace(replace(replace(lower({name}), '-', ' '), '(', ' '), ')', ' '), '/', ' ') || ' ')"
    says = " OR ".join([f"({name} LIKE '%ski%' AND ({words} LIKE '% ski %' OR {words} LIKE '% skiing %'))",
                        *(f"{name} LIKE '%{w}%'" for w in SKI_TRAIL_NAME_WORDS)])
    not_trail = " OR ".join(f"{words} LIKE '% {w} %'" for w in NOT_SKI_TRAIL_NAME_WORDS)
    return f"({says}) AND NOT ({not_trail})"


def general_rules(L: Layer) -> list[tuple[str, str]]:
    """(rule name, SQL condition) pairs. A feature is kept if any condition is true."""
    sport, shop, natural = L.tag("sport"), L.tag("shop"), L.tag("natural")
    landcover = f"lower({L.tag('landcover')})"
    # Nature rules skip clearcuts that are also tagged as forest; ski rules still keep them.
    forest_clearcut = f"({L.is_clearcut()} AND {L.is_forest()})"
    rules = [
        ("natural", f"{natural} IS NOT NULL AND {natural} NOT IN ({sql_list(WATER_NATURAL)}) "
                    f"AND NOT {forest_clearcut}"),
        ("piste", " OR ".join([
            L.any_column("piste_"),
            L.any_key("piste", "key LIKE '%piste%'"),
        ])),
        ("lifts", " OR ".join([
            f"{L.tag('aerialway')} IS NOT NULL",
            # aerialway:*, proposed:aerialway, abandoned:aerialway, disused:aerialway, ...
            L.any_key("aerialway", "key LIKE '%aerialway%'"),
            f"{L.tag('railway')} = 'funicular'",
        ])),
        ("ski_areas", " OR ".join([
            f"{L.tag('landuse')} = 'winter_sports'",
            f"{L.tag('site')} = 'piste'",
            f"({sport} LIKE '%ski%' AND {sport} NOT LIKE '%water_ski%')",
            L.any_key("ski_resort", "key LIKE 'ski_resort%'"),
        ])),
        ("ski_access", ski_access(L)),
        ("ski_pois", " OR ".join([
            f"{shop} IN ('ski', 'ski_rental', 'ski_shop')",
            f"(';' || {shop} || ';') LIKE '%;ski;%'",
            f"{L.tag('amenity')} IN ({sql_list(SKI_AMENITIES)})",
            f"{L.tag('tourism')} IN ({sql_list(HUTS_AND_VIEWS)})",
        ])),
        # Ski jumps, snow guns, lift buildings, snowboard parks, abandoned ski areas, "piste" values, ...
        ("ski_tags", L.any_tag_says_ski()),
        ("ski_trails", f"{L.tag('highway')} IN ({sql_list(SKI_TRAIL_HIGHWAYS)}) "
                       f"AND name IS NOT NULL AND ({names_ski_trail('name')})"),
        # Straight clearings through forest: firebreaks, power-line and border cuts, some lift lines.
        ("cutlines", f"{L.tag('man_made')} = 'cutline'"),
        # Buildings are skipped here: a house with leaf_type or wetland=* is not nature.
        ("vegetation", f"{L.tag('building')} IS NULL AND NOT {forest_clearcut} AND (" + " OR ".join([
            f"{L.tag('landuse')} IN ({sql_list(VEGETATION_LANDUSE)})",
            *(f"{landcover} LIKE '%{v}%'" for v in VEGETATION_LANDCOVER),
            f"{L.tag('leaf_type')} IS NOT NULL",
            f"{L.tag('wood')} IS NOT NULL",
            f"{L.tag('wetland')} IN ({sql_list(WETLANDS)})",
        ]) + ")"),
    ]
    if L.name not in AREA_LAYERS:
        rules.append(("waterways", f"{L.tag('waterway')} IN ({sql_list(WATERWAYS)})"))
    return rules


def ski_access(L: Layer) -> str:
    """ski=*, xc_ski=* and ski:*=* tags (ski:nordic, ski:alpine, ...), unless the value is "no"."""
    return L.any_key("ski", "key IN ('ski', 'xc_ski') OR key LIKE 'ski:%'", "value <> 'no'")


def ski_route_rule(L: Layer) -> str:
    """True for route relations that are ski routes.

    piste:type can hold several values ("nordic;hike"). Every non-ski value is
    removed from ";a;b;"; if anything is left, the route includes a ski type.
    Each replace runs twice so repeated values ("hike;hike") are removed too.
    Routes without a ski piste:type still count if they carry ski access tags
    (a hiking route with ski=designated) or a name like "Nordic Ski Trail".
    """
    piste, route = L.tag("piste:type"), L.tag("route")
    rest = f"(';' || replace(lower({piste}), ' ', '') || ';')"
    for value in NON_SKI_PISTE_TYPES * 2:
        rest = f"replace({rest}, ';{value};', ';')"
    return " OR ".join([
        f"({piste} IS NOT NULL AND trim({rest}, ';') <> '')",
        f"({piste} IS NULL AND ({route} LIKE '%ski%' OR {route} LIKE '%piste%' "
        f"OR {route} LIKE '%aerialway%'))",
        f"({ski_access(L)})",
        # Trail routes only: bus routes and bike routes are often named after a ski area.
        f"({route} IN ({sql_list(SKI_TRAIL_ROUTES)}) AND name IS NOT NULL AND ({names_ski_trail('name')}))",
    ])


def layer_rules(L: Layer) -> list[tuple[str, str]]:
    rules = general_rules(L)
    if L.name not in ROUTE_LAYERS:
        return rules
    route = L.tag("route")
    return ([(name, f"{route} IS NULL AND ({sql})") for name, sql in rules]
            + [("ski_routes", f"{route} IS NOT NULL AND ({ski_route_rule(L)})")])


def keep_where(rules: list[tuple[str, str]]) -> str:
    return " OR ".join(f"({sql})" for _, sql in rules)


def forests_to_cut_where(L: Layer) -> str:
    """Kept forest polygons that clearcuts are cut out of: kept only by the nature rules.

    Runs, resort areas and other ski features keep their shape even if tagged as forest.
    """
    rules = dict(layer_rules(L))
    nature = " OR ".join(f"({rules[n]})" for n in NATURE_RULES)
    others = " OR ".join(f"({sql})" for n, sql in rules.items() if n not in NATURE_RULES)
    return f"{L.is_forest()} AND NOT {L.is_clearcut()} AND ({nature}) AND NOT COALESCE(({others}), 0)"


# ---------------------------------------------------------------- reading the input

def connect_ro(gpkg: Path) -> sqlite3.Connection:
    return sqlite3.connect(gpkg.resolve().as_uri() + "?mode=ro", uri=True)


def read_layers(gpkg: Path) -> list[Layer]:
    with connect_ro(gpkg) as con:
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type = 'features' ORDER BY rowid")]
        return [Layer(n, [r[1] for r in con.execute(f'PRAGMA table_info("{n}")')]) for n in names]


def count_rules(gpkg: Path, layers: list[Layer]) -> dict:
    """Per layer: rows read, rows kept, and rows matched by each rule (one scan per layer)."""
    counts = {}
    with connect_ro(gpkg) as con:
        for L in layers:
            started = time.time()
            rules = layer_rules(L)
            sums = ", ".join(f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END)" for _, sql in rules)
            row = con.execute(
                f'SELECT COUNT(*), SUM(CASE WHEN {keep_where(rules)} THEN 1 ELSE 0 END), {sums} '
                f'FROM "{L.name}"').fetchone()
            row = [int(v or 0) for v in row]
            counts[L.name] = {"read": row[0], "kept": row[1],
                              "rules": {name: n for (name, _), n in zip(rules, row[2:])}}
            log.info("  %-16s %10d of %10d kept (%.0f s)", L.name, row[1], row[0], time.time() - started)
    return counts


def print_report(counts: dict) -> None:
    for layer, c in counts.items():
        print(f"\n{layer}: keeps {c['kept']:,} of {c['read']:,}")
        for name, n in c["rules"].items():
            print(f"  {name:<12} {n:>12,}")
    print("\nA feature can match several rules, so rule counts add up to more than 'keeps'.")


# ---------------------------------------------------------------- clearcuts

def polygonal(geom):
    """The polygon parts of `geom` as a MultiPolygon, or None if it has none."""
    import shapely

    parts, todo = [], list(shapely.get_parts(geom))
    while todo:
        g = todo.pop()
        if g.geom_type == "Polygon" and not g.is_empty:
            parts.append(g)
        elif g.geom_type in ("MultiPolygon", "GeometryCollection"):
            todo.extend(shapely.get_parts(g))
    return shapely.MultiPolygon(parts) if parts else None


def cut_clearcuts(src: Path, L: Layer):
    """Cut clearcut shapes out of the kept forest polygons that overlap them.

    In OSM a clearcut is often drawn on top of a larger wood or forest polygon, so the
    clearcut would still look like forest. Returns (rows, drop_fids, stats):
        rows       forest polygons that lost area: source columns, new geometry
        drop_fids  source fids to leave out of the plain copy (rows, plus forests that
                   were entirely clearcut and are dropped)
        stats      counts and km² for ski_nature.json
    """
    import geopandas as gpd
    import pyogrio
    import shapely

    cuts = pyogrio.read_dataframe(src, layer=L.name, columns=[], where=L.is_clearcut())
    stats = {"clearcut_areas": len(cuts), "forest_polygons_cut": 0, "forest_polygons_removed": 0,
             "forest_km2_removed": 0.0}
    if cuts.empty:
        return None, [], stats
    cut_geoms = shapely.make_valid(cuts.geometry.values)
    forests = pyogrio.read_dataframe(src, layer=L.name, where=forests_to_cut_where(L),
                                     mask=shapely.union_all(cut_geoms), fid_as_index=True)
    index = shapely.STRtree(cut_geoms)
    changed, new_geoms, removed = [], [], []
    for fid, geom in zip(forests.index, shapely.make_valid(forests.geometry.values)):
        hits = index.query(geom, predicate="intersects")
        if not len(hits):
            continue
        rest = shapely.difference(geom, shapely.union_all(cut_geoms[hits]))
        rest = polygonal(rest)
        if rest is None:
            removed.append(fid)
        elif rest.area < geom.area * (1 - 1e-9):  # touching a clearcut's edge doesn't count
            changed.append(fid)
            new_geoms.append(rest)

    before = gpd.GeoSeries(forests.geometry.loc[changed + removed].values, crs=forests.crs)
    after = gpd.GeoSeries(new_geoms, crs=forests.crs)
    stats.update(forest_polygons_cut=len(changed), forest_polygons_removed=len(removed),
                 forest_km2_removed=round(float(before.to_crs(EQUAL_AREA_CRS).area.sum()
                                                - after.to_crs(EQUAL_AREA_CRS).area.sum()) / 1e6, 1))
    rows = forests.loc[changed].copy()
    rows["geometry"] = gpd.GeoSeries(new_geoms, index=rows.index, crs=forests.crs)
    return rows, [int(f) for f in changed + removed], stats


# ---------------------------------------------------------------- building the output

def build(src: Path, dst: Path, layers: list[Layer], cut_rows, cut_fids: list[int]) -> None:
    import pyogrio

    tmp = tmp_path(dst)
    tmp.unlink(missing_ok=True)
    for i, L in enumerate(layers):
        log.info("$ ogr2ogr %s <- %s layer %s (keep rules: %s)", tmp.name, src.name, L.name,
                 ", ".join(name for name, _ in layer_rules(L)))
        where = keep_where(layer_rules(L))
        if L.name == CLEARCUT_LAYER and cut_fids:
            where = f"({where}) AND fid NOT IN ({', '.join(map(str, cut_fids))})"
        cmd = ["ogr2ogr", "-f", "GPKG", *(["-update"] if i else []), str(tmp), str(src), L.name,
               "-nln", L.name, "-where", where, "-gt", "65536"]
        code = subprocess.run(cmd).returncode
        if code != 0:
            # The -where filter is several KB of SQL, so don't print the whole command.
            sys.exit(f"ogr2ogr failed on layer {L.name} (exit {code}); see GDAL's error above.")
    if cut_rows is not None and len(cut_rows):
        # Same columns in the same order as the layer (pyogrio appends fields by position).
        # These rows get new fids; osm_id / osm_way_id still identify them.
        log.info("appending %d forest polygons with clearcuts cut out", len(cut_rows))
        pyogrio.write_dataframe(cut_rows, tmp, layer=CLEARCUT_LAYER, append=True)
    finish(tmp, dst)


def output_counts(gpkg: Path, layers: list[Layer]) -> dict[str, int]:
    with connect_ro(gpkg) as con:
        return {L.name: con.execute(f'SELECT COUNT(*) FROM "{L.name}"').fetchone()[0] for L in layers}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help=f"where data is stored (default: {DEFAULT_DATA_DIR})")
    ap.add_argument("--force", action="store_true", help="rebuild even if the output is up to date")
    ap.add_argument("--report", action="store_true",
                    help="print how many features each rule matches; write nothing")
    args = ap.parse_args()

    setup_logging()
    out = args.data_dir.resolve() / "out"
    src = out / "features_near_runs.gpkg"
    dst = out / "ski_nature.gpkg"
    manifest_path = out / "ski_nature.json"
    if not src.exists():
        sys.exit(f"Missing {src}. Run osm_data/filter_near_runs.py first.")

    layers = read_layers(src)
    rules_sql = {L.name: keep_where(layer_rules(L)) for L in layers}
    area_layer = next(L for L in layers if L.name == CLEARCUT_LAYER)
    rules_sql["clearcuts"] = [area_layer.is_clearcut(), forests_to_cut_where(area_layer)]
    rules_hash = hashlib.md5(json.dumps(rules_sql, sort_keys=True).encode()).hexdigest()

    if args.report:
        log.info("counting matches per rule in %s", src.name)
        print_report(count_rules(src, layers))
        return

    require_tools("ogr2ogr")
    tmp_path(dst).unlink(missing_ok=True)  # leftover from an interrupted run
    previous = read_json(manifest_path) or {}
    if (not args.force and dst.exists() and dst.stat().st_mtime > src.stat().st_mtime
            and previous.get("rules_hash") == rules_hash):
        log.info("%s is up to date (use --force to rebuild)", dst.name)
        return

    started = now_iso()
    log.info("== counting matches per rule")
    counts = count_rules(src, layers)
    log.info("== cutting clearcuts out of forest polygons")
    cut_rows, cut_fids, clearcuts = cut_clearcuts(src, area_layer)
    log.info("  %d clearcut areas: %d forest polygons cut, %d entirely clearcut and dropped (%.1f km² of forest removed)",
             clearcuts["clearcut_areas"], clearcuts["forest_polygons_cut"],
             clearcuts["forest_polygons_removed"], clearcuts["forest_km2_removed"])
    log.info("== writing %s", dst.name)
    build(src, dst, layers, cut_rows, cut_fids)
    written = output_counts(dst, layers)
    for name, n in written.items():
        expected = counts[name]["kept"] - (clearcuts["forest_polygons_removed"] if name == CLEARCUT_LAYER else 0)
        if n != expected:
            sys.exit(f"{name}: wrote {n} features but expected {expected}")

    source_manifest = read_json(out / "manifest.json") or {}
    write_json(manifest_path, {
        "input": str(src),
        "input_modified": datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "input_snapshot": source_manifest.get("input_snapshot"),
        "rules_hash": rules_hash,
        "non_ski_piste_types": NON_SKI_PISTE_TYPES,
        "dropped_water_natural": WATER_NATURAL,
        "waterways": WATERWAYS,
        "clearcuts": clearcuts,
        "layers": counts,
        "started": started,
        "finished": now_iso(),
    })
    log.info("done: %s (%s), %d features kept of %d", dst.name, human_size(dst.stat().st_size),
             sum(written.values()), sum(c["read"] for c in counts.values()))


if __name__ == "__main__":
    main()
