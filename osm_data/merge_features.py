#!/usr/bin/env python3
"""Merge features that are one real thing stored as several OSM objects.

Second cleaning step, after clean_features.py. OSM often splits one real feature into
several objects: a run cut into pieces at lift crossings, a gondola drawn as two sections,
a river cut into hundreds of ways, a piste route relation plus its own member ways. This
step joins them, matching on name + location.

Reads <data-dir>/out/ski_nature.gpkg and the relation member lists in near_runs.osm.pbf,
and writes, under <data-dir>/out/:
    ski_nature_merged.gpkg    same layers and columns; each merged feature is one row, plus
                              provenance columns (merge_group, merged_ids, merged_count,
                              merge_match, merge_conflicts). Untouched rows keep their fid.
    ski_nature_merged.json    inputs, rules hash and counts
    merge_groups.csv          one row per merged feature, with openstreetmap.org links
    merge_pieces.csv          one row per source object that went into a merged feature
    merge_near_misses.csv     touching pairs that were not merged, and why, plus merged
                              pieces drawn side by side ("drawn twice?")
    merge_review.html         the groups and near misses as a filterable page

Rules (details in osm_data/README.md):
    name       names match after normalizing (accents, case, punctuation, spacing,
               possessives, u./l. and other abbreviations). Small spelling differences are
               allowed (one edit from 8 letters, two from 16) only if the qualifier words
               match (upper/lower, east/west, numbers, single letters, ...).
    location   lines: an end of one is within 15 m of the other on the ground. Lifts
               must meet end to end, and side-by-side twin lifts are kept apart.
               Polygons: within 5 m.
    kind       only the same kind of feature merges (downhill with downhill, a lift with
               the same lift type, stream with river, woods with forest, grass with
               meadow, ...).
    relations  an unnamed way in exactly one named route/waterway relation takes its name.
               A relation whose ways in the lines layer all carry its name is the same
               feature as those ways and is merged into them.
    attributes the hardest piste:difficulty wins, multi-value tags are combined, gladed/
               lit/snowmaking are yes if any piece says yes, other conflicts go to the
               longest (or largest) piece. Every conflict is recorded.
Points and unnamed features (apart from inherited names) are never changed.

Typical usage example:

    python osm_data/merge_features.py --data-dir "/path/to/osm_data/data"
    python osm_data/merge_features.py --data-dir "/path/to/osm_data/data" --report

--report prints what would be merged and writes nothing. --force rebuilds even when the
output is newer than the inputs and the rules haven't changed.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import csv
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import numpy as np
import osmium
import pandas as pd
import pyogrio
import pyproj
import shapely
from rapidfuzz.distance import OSA
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from clean_features import EQUAL_AREA_CRS, FOREST_LANDUSE, NON_SKI_PISTE_TYPES, SKI_TRAIL_HIGHWAYS
from common import (
    DEFAULT_DATA_DIR,
    OSMCONF,
    connect_ro,
    finish,
    human_size,
    log,
    now_iso,
    polygonal,
    read_json,
    require_tools,
    run,
    setup_logging,
    tmp_path,
    write_json,
)

# ---------------------------------------------------------------- rules

# Distances and lengths in metres are measured on the ground, along the WGS84 ellipsoid
# (see ground_distance and ground_length), so a 15 m gap means 15 m everywhere. To find
# what lies near what, geometry is projected to METRIC_CRS, the North America Lambert
# Conformal Conic. Being conformal, it scales every short distance near a point by the
# same factor (see stretch), so the nearest points in it are the nearest points on the
# ground. The geometry that is read and written stays in EPSG:4326 (longitude/latitude).
METRIC_CRS = "ESRI:102009"
# Areas are measured in this equal-area projection, which keeps them true everywhere.
AREA_CRS = EQUAL_AREA_CRS
# Two lines link when an end of one lies within this many metres of the other line.
LINE_GAP_M = 15.0
# Two polygons link when they overlap or lie within this many metres of each other.
POLY_GAP_M = 5.0
# A line end closer than this (metres) to the line it links to already touches it (they
# share an OSM node), so no connector is drawn between them.
SHARED_NODE_M = 0.05
# A line is a copy of another (the same run drawn twice) when at least COPY_SHARE of its
# length lies within COPY_WITHIN_M metres of the other line, and either both its ends do
# too, or one does and it runs along the other line for COPY_SHARE of its length. The second
# case is a run redrawn with a different end; a short line that just leads away from the
# other's end doesn't count. The copy's tags are merged but its geometry is dropped.
COPY_WITHIN_M = 3.0
COPY_SHARE = 0.9
# Two linked lifts are twin lifts side by side (two separate lifts, not two sections of one)
# when the shorter one is at least TWIN_LENGTH_RATIO of the longer one's length, both its
# ends lie within TWIN_APART_M metres of the longer one, and it runs alongside the longer one
# for at least TWIN_OVERLAP_SHARE of its length (see runs_alongside). Sections of one lift
# meet at one end and then lead away from each other, so they never run alongside.
TWIN_LENGTH_RATIO = 0.8
TWIN_APART_M = 30.0
TWIN_OVERLAP_SHARE = 0.8
# Self-check tolerance, as a fraction (plus 1 m or 1 m²). A merged line must be as long as
# its pieces plus connectors; a merged polygon's area must lie between its largest piece's
# area and the sum of its pieces' areas.
LENGTH_TOLERANCE = 0.001

# Words that tell two runs (or streams) with otherwise the same name apart, such as Upper
# and Lower White Lightning. Names whose qualifiers differ never merge. French forms are
# included for Quebec. Numbers, Roman numerals and single letters are qualifiers too (see
# is_qualifier).
QUALIFIER_WORDS = frozenset("""
    upper lower middle top bottom lowest uppermost lowermost
    north south east west northeast northwest southeast southwest
    haut haute bas basse superieur superieure inferieur inferieure est ouest nord sud
    left right gauche droite inner outer
""".split())
# Roman numerals that number sections or lifts (Gondola I, Gondola II). Also qualifiers.
ROMAN_NUMERALS = frozenset("i ii iii iv v vi vii viii ix x".split())
# Dropped from the start of a name of two or more words, so "The Glades" matches "Glades".
LEADING_ARTICLES = frozenset({"the", "la", "le", "les"})
# Abbreviations expanded before names are compared, so "Upr Cruiser" matches "Upper
# Cruiser" and "Mt Snow" matches "Mount Snow".
ABBREVIATIONS = {
    "upr": "upper", "lwr": "lower", "mid": "middle",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
    "mt": "mount", "mtn": "mountain", "rd": "road", "trl": "trail", "tr": "trail",
    "ck": "creek", "crk": "creek",
}

# piste:difficulty values from easiest to hardest. When pieces disagree, the hardest wins.
DIFFICULTY_ORDER = ["novice", "easy", "intermediate", "advanced", "expert", "freeride", "extreme"]
# Position of each difficulty in DIFFICULTY_ORDER; a higher rank is harder.
DIFFICULTY_RANK = {d: i for i, d in enumerate(DIFFICULTY_ORDER)}
# Non-standard piste:difficulty values and the standard value each one means.
DIFFICULTY_ALIASES = {"beginner": "novice", "difficult": "advanced", "hard": "advanced"}
# Tags that can hold several values separated by ";". When pieces disagree, the merged
# feature gets every value, the one covering the most length first.
MULTI_VALUE_KEYS = {"piste:type", "piste:grooming", "ref", "piste:ref", "piste:number", "alt_name"}
# Multi-value tags whose values may also be separated by "+": classic+skating means
# classic;skating.
PLUS_SEPARATED_KEYS = {"piste:grooming"}
# Yes/no tags that are "yes" on the merged feature if any piece says yes: a run is gladed
# if any section of it is.
YES_IF_ANY_KEYS = {"gladed", "lit", "piste:lit", "piste:snowmaking", "patrolled"}
# Tags whose merged value is the one carried by the most total length (or area) across all
# pieces. Other conflicting tags take the value of the single longest piece instead.
MOST_COVERAGE_KEYS = {"name", "waterway", "highway"}
# Tags that describe a relation itself rather than the feature, so they are never copied
# onto a merged feature.
RELATION_ONLY_KEYS = {"type", "route"}

# piste:type values that aren't skiing (hike, sled, snowshoe, ...). A piste with only these
# types merges only with pistes of exactly the same types (see piste_compatible).
NON_SKI = frozenset(NON_SKI_PISTE_TYPES)
# highway values for paths and tracks a ski trail can follow. They all count as one "trail"
# kind, so a path and a track with the same name can merge.
TRAIL_HIGHWAYS = frozenset(SKI_TRAIL_HIGHWAYS)
# One watercourse: a creek can become a river, and a flowline is the river's path through a lake.
STREAM_RIVER = frozenset({"stream", "river", "flowline"})
# landuse values for grass. They merge with each other and with natural=grassland.
GRASS_LANDUSE = frozenset({"grass", "meadow"})
# Tags that give the kind of any other line, checked in this order (the first one the line
# has wins): man_made=pipeline, power=line, ...
LINE_KIND_KEYS = ["man_made", "natural", "highway", "railway", "barrier", "power"]
# Tags that give a polygon's kind label, checked in this order (the first one it has wins).
AREA_KIND_KEYS = ["landuse", "natural", "landcover", "leisure", "man_made", "tourism", "amenity",
                  "building", "sport", "wetland"]
# Relation layers to read, and which relation type values to read from each (None reads
# every row). These relations can share a name with their member ways.
RELATION_TYPES = {"multilinestrings": None,
                  "other_relations": ("waterway", "route", "piste", "multilinestring")}

# Layers this script changes. Every other layer (points) is copied unchanged.
MERGE_LAYERS = ["lines", "multilinestrings", "multipolygons", "other_relations"]
# Columns that hold something other than an OSM tag. Currently unused in this script.
NOT_TAG_COLUMNS = {"fid", "geom", "geometry", "osm_id", "osm_way_id", "other_tags", "z_order"}
# (column, SQLite type) of the provenance columns added to each layer in MERGE_LAYERS. They
# stay empty on rows that weren't merged. merge_group is the group id, merged_ids lists the
# pieces' OSM ids (representative first), merged_count is the number of pieces,
# merge_match is how the names matched and merge_conflicts holds the tag conflicts as JSON.
PROVENANCE_COLUMNS = [("merge_group", "TEXT"), ("merged_ids", "TEXT"), ("merged_count", "INTEGER"),
                      ("merge_match", "TEXT"), ("merge_conflicts", "TEXT")]
# Near-miss codes returned by compare_pairs, and the reason each one stands for.
NEAR_REASONS = {1: "qualifier differs", 2: "name too different"}
# Reason for two pieces of one merged line that run side by side without being a copy (see
# find_line_links). They may be one feature drawn twice, so they are listed with the near
# misses for review, but both are kept.
DRAWN_TWICE = "drawn twice?"


def rules() -> dict:
    """List every setting that changes the result, for the up-to-date check.

    main() stores a hash of this dict in ski_nature_merged.json. When the hash changes, the
    output is rebuilt even if it is newer than the inputs.

    Returns:
        A dict of setting name to value. It includes an MD5 hash of this script, so any
        edit to the script (even to a comment) counts as a rule change.
    """
    return {
        "metric_crs": METRIC_CRS, "area_crs": AREA_CRS, "line_gap_m": LINE_GAP_M, "poly_gap_m": POLY_GAP_M,
        "shared_node_m": SHARED_NODE_M, "copy_within_m": COPY_WITHIN_M, "copy_share": COPY_SHARE,
        "twin_length_ratio": TWIN_LENGTH_RATIO, "twin_apart_m": TWIN_APART_M,
        "twin_overlap_share": TWIN_OVERLAP_SHARE, "qualifiers": sorted(QUALIFIER_WORDS),
        "abbreviations": ABBREVIATIONS, "difficulty_order": DIFFICULTY_ORDER,
        "script_md5": hashlib.md5(Path(__file__).read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------- names

@dataclass(frozen=True)
class NameKey:
    """A feature's name, normalized for matching (see name_key).

    Attributes:
        raw: The name as written in OSM, trimmed.
        compact: The normalized words joined without spaces, so "Bear Claw" and "Bearclaw"
            both become "bearclaw".
        base: compact without the qualifier words: "Upper White Lightning" gives
            "whitelightning".
        quals: The qualifier words (upper, east, 2, b, ii, ...) that tell features with
            otherwise the same name apart.
    """

    raw: str
    compact: str
    base: str
    quals: frozenset


def is_qualifier(token: str) -> bool:
    """Check whether a word of a name tells same-named features apart.

    Args:
        token: One normalized word of a name (lowercase, no accents).

    Returns:
        True if the word is in QUALIFIER_WORDS or ROMAN_NUMERALS, is a single letter or
        contains a digit (Chair 7, 7A).
    """
    return (token in QUALIFIER_WORDS or token in ROMAN_NUMERALS or len(token) == 1
            or any(c.isdigit() for c in token))


@lru_cache(maxsize=None)
def name_key(raw: str | None) -> NameKey | None:
    """Normalize a name for matching.

    Accents and case are removed, & and + become "and", "U." and "L." become upper and
    lower, the words in ABBREVIATIONS are expanded, and French elisions (l', d', j'),
    possessives and a leading article are dropped. What's left is split into words of
    letters and digits. For example "L. Cruiser" gives lower + cruiser and "Russ's" gives
    russ. Results are cached because the same names come up many times.

    Args:
        raw: The name as written in OSM. May be None or NaN for unnamed features.

    Returns:
        The NameKey for the name, or None if there is no name or it has no letters or
        digits.
    """
    if not present(raw) or not str(raw).strip():
        return None
    raw = str(raw).strip()
    s = unicodedata.normalize("NFKD", raw)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[’‘`´]", "'", s)
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"(^|\s)u\.(?=\s)", r"\1upper", s)
    s = re.sub(r"(^|\s)l\.(?=\s)", r"\1lower", s)
    s = re.sub(r"\b[ldj]'", " ", s)        # French elision: l'Archipel, d'Hiver
    s = re.sub(r"'s\b", "", s)             # possessive: Cassidy's -> cassidy
    s = s.replace("'", "")                 # Russ' -> russ
    tokens = [ABBREVIATIONS.get(t, t) for t in re.findall(r"[a-z0-9]+", s)]
    if len(tokens) > 1 and tokens[0] in LEADING_ARTICLES:
        tokens = tokens[1:]
    if not tokens:
        return None
    quals = frozenset(t for t in tokens if is_qualifier(t))
    return NameKey(raw, "".join(tokens), "".join(t for t in tokens if t not in quals), quals)


def allowed_edits(length: int) -> int:
    """Return how many spelling edits two names may differ by and still match.

    Args:
        length: Length of the shorter of the two compact names, in characters.

    Returns:
        2 for names of 16 or more characters, 1 for 8 to 15, and 0 for shorter names,
        which must match exactly once spaces and punctuation are removed.
    """
    return 2 if length >= 16 else 1 if length >= 8 else 0


def compare_names(a: NameKey, b: NameKey) -> tuple[str | None, str | None]:
    """Decide whether two normalized names are the same name.

    Spelling differences are counted with rapidfuzz's OSA (optimal string alignment)
    distance: the number of one-letter insertions, deletions, substitutions and
    adjacent swaps needed to turn one name into the other.

    Args:
        a: The first name, as returned by name_key().
        b: The second name, as returned by name_key().

    Returns:
        A (match, near_miss) tuple.
        match is "exact" if the names are written the same, "fuzzy" if they differ
        only in spacing, punctuation, accents or a few letters (see allowed_edits),
        and None if they don't match.
        near_miss is None unless the names don't match but come close, for the
        near-miss review file. It is then "qualifier differs" if their qualifier words
        differ but the rest of the names match (Upper / Lower White Lightning), or
        "name too different" if they are one edit more than allowed apart
        (The Vain / The Main).
    """
    if a.raw == b.raw:
        return "exact", None
    if a.compact == b.compact:          # Bear Claw / Bearclaw, Cascapédia / Cascapedia
        return "fuzzy", None
    allowed = allowed_edits(min(len(a.compact), len(b.compact)))
    # score_cutoff lets OSA stop early: any distance above it comes back as score_cutoff + 1.
    if a.quals != b.quals:              # Upper / Lower White Lightning
        if a.base and b.base and OSA.distance(a.base, b.base, score_cutoff=allowed) <= allowed:
            return None, "qualifier differs"
        return None, None
    dist = OSA.distance(a.compact, b.compact, score_cutoff=allowed + 1)
    if dist <= allowed:                 # Winnisook / Winisook
        return "fuzzy", None
    if dist == allowed + 1:             # The Vain / The Main
        return None, "name too different"
    return None, None


# ---------------------------------------------------------------- kinds

@dataclass(frozen=True)
class Kind:
    """What sort of feature a row is, which decides what it may merge with.

    Attributes:
        family: Features merge only within a family, such as "piste",
            "aerialway:chair_lift", "waterway:stream_river" or "woodland". Pistes must
            also have compatible piste types (see kinds_compatible).
        label: A readable kind for the review files, such as "piste:downhill" or
            "natural:wood".
        pistes: The piste:type values of a piste or piste relation; None for other kinds.
    """

    family: str
    label: str
    pistes: frozenset | None = None


def piste_types(value) -> frozenset:
    """Split a piste:type value into its separate types.

    Args:
        value: A piste:type tag value, which may hold several types separated by ";"
            (e.g. "nordic;hike").

    Returns:
        The lowercased types, without blanks.
    """
    return frozenset(v.strip().lower() for v in str(value).split(";") if v.strip())


def piste_compatible(a: frozenset, b: frozenset) -> bool:
    """Check whether two pistes' types allow them to merge.

    Ski pistes (with any type not in NON_SKI) need a ski type in common: nordic merges with
    nordic;hike but not with downhill. Pistes with no ski type need exactly the same types,
    so a hiking trail doesn't merge with a snowshoe trail.

    Args:
        a: The first piste's types (see piste_types).
        b: The second piste's types.

    Returns:
        True if the two pistes can be pieces of one feature.
    """
    ski_a, ski_b = a - NON_SKI, b - NON_SKI
    if ski_a or ski_b:
        return bool(ski_a & ski_b)
    return a == b


def kinds_compatible(a: Kind, b: Kind) -> bool:
    """Check whether two features are of kinds that can merge.

    Args:
        a: The first feature's kind.
        b: The second feature's kind.

    Returns:
        True if both are in the same family and, when both have piste types, those
        types are compatible (see piste_compatible).
    """
    if a.family != b.family:
        return False
    if not a.pistes or not b.pistes:     # not a piste, or a relation without piste:type
        return True
    return piste_compatible(a.pistes, b.pistes)


def line_kind(tags: dict) -> Kind:
    """Classify a line by its tags.

    The first of these tags that the line has decides its kind: piste:type, aerialway,
    railway=funicular (counted as a lift), waterway (streams, rivers and flowlines share one
    family), highway paths and tracks (one "trail" family), then the keys in LINE_KIND_KEYS.

    Args:
        tags: The line's OSM tags, key to value.

    Returns:
        The line's Kind, or Kind("other", "other") if none of those tags apply.
    """
    pt = tags.get("piste:type")
    if pt:
        return Kind("piste", f"piste:{pt}", piste_types(pt))
    aw = tags.get("aerialway")
    if aw:
        return Kind(f"aerialway:{aw}", f"aerialway:{aw}")
    if tags.get("railway") == "funicular":
        return Kind("aerialway:funicular", "railway:funicular")
    ww = tags.get("waterway")
    if ww:
        return Kind("waterway:stream_river" if ww in STREAM_RIVER else f"waterway:{ww}", f"waterway:{ww}")
    hw = tags.get("highway")
    if hw in TRAIL_HIGHWAYS:
        return Kind("trail", f"highway:{hw}")
    for key in LINE_KIND_KEYS:
        if tags.get(key):
            return Kind(f"{key}:{tags[key]}", f"{key}:{tags[key]}")
    return Kind("other", "other")


def area_kind(tags: dict) -> Kind:
    """Classify a polygon by its tags.

    Pistes and lifts are classified as in line_kind. A ski area (landuse=winter_sports) is
    its own family even if it also has tree landcover. Woods and forests share the
    "woodland" family and grass, meadow and grassland share the "grassland" family, so they
    merge with each other. Any other polygon's family is its first tag in AREA_KIND_KEYS.

    Args:
        tags: The polygon's OSM tags, key to value.

    Returns:
        The polygon's Kind. Apart from pistes and lifts, its label is the polygon's first
        tag in AREA_KIND_KEYS (e.g. "natural:wood"), so the review files show the
        original tag.
    """
    pt = tags.get("piste:type")
    if pt:
        return Kind("piste", f"piste:{pt}", piste_types(pt))
    aw = tags.get("aerialway")
    if aw:
        return Kind(f"aerialway:{aw}", f"aerialway:{aw}")
    label = next((f"{k}:{tags[k]}" for k in AREA_KIND_KEYS if tags.get(k)), "other")
    landuse, natural = tags.get("landuse"), tags.get("natural")
    landcover = (tags.get("landcover") or "").lower()
    if landuse == "winter_sports":
        return Kind("landuse:winter_sports", label)
    if landuse in FOREST_LANDUSE or natural == "wood" or "tree" in landcover:
        return Kind("woodland", label)
    if landuse in GRASS_LANDUSE or natural == "grassland" or "grass" in landcover:
        return Kind("grassland", label)
    return Kind(label, label)


def relation_kind(tags: dict) -> Kind:
    """Classify a route, piste or waterway relation by its tags.

    Args:
        tags: The relation's OSM tags, key to value.

    Returns:
        The relation's Kind. Its family is "rel:waterway", "rel:piste" (with the piste
        types, if it has piste:type), "rel:aerialway", "rel:trail" for any other route, or
        "rel:any". relation_accepts uses the family to decide which ways can belong to it.
    """
    typ, route = (tags.get("type") or "").lower(), (tags.get("route") or "").lower()
    pt = tags.get("piste:type")
    if typ == "waterway":
        return Kind("rel:waterway", "relation:waterway")
    if pt or "piste" in route or "ski" in route or typ == "piste":
        return Kind("rel:piste", f"relation:piste:{pt or route or typ}", piste_types(pt) if pt else frozenset())
    if "aerialway" in route:
        return Kind("rel:aerialway", "relation:aerialway")
    if route:
        return Kind("rel:trail", f"relation:route:{route}")
    return Kind("rel:any", f"relation:{typ or 'other'}")


def relation_accepts(rel: Kind, way: Kind) -> bool:
    """Check whether a way can be a piece of the same feature as a relation.

    Waterway relations accept any waterway. Piste relations accept pistes of a compatible
    type, and paths and tracks. Aerialway relations accept lifts but not stations. Other
    route relations accept paths, tracks and pistes. Any other relation accepts any way.

    Args:
        rel: The relation's kind (see relation_kind).
        way: The way's kind (see line_kind).

    Returns:
        True if the way can be a piece of the relation's feature.
    """
    if rel.family == "rel:waterway":
        return way.family.startswith("waterway:")
    if rel.family == "rel:piste":
        if way.family == "piste":
            return not rel.pistes or piste_compatible(rel.pistes, way.pistes)
        return way.family == "trail"
    if rel.family == "rel:aerialway":
        return way.family.startswith("aerialway:") and way.family != "aerialway:station"
    if rel.family == "rel:trail":
        return way.family in ("trail", "piste")
    return True


def end_to_end_only(kind: Kind) -> bool:
    """Check whether features of this kind may only join end to end.

    Lifts join only end to end, like two sections of a gondola. A lift touching another
    mid-way is a different lift.

    Args:
        kind: The feature's kind.

    Returns:
        True for lifts (every aerialway family, funiculars included).
    """
    return kind.family.startswith("aerialway:")


# ---------------------------------------------------------------- tags

def present(v) -> bool:
    """Check whether a value read from a table is set.

    Args:
        v: A value from a DataFrame cell or a SQLite row.

    Returns:
        False for None, NaN and pd.NA; True for anything else, including "".
    """
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and v is not pd.NA


@lru_cache(maxsize=None)
def layer_columns(layer: str) -> dict[str, str]:
    """Map a layer's tag columns to the OSM keys they hold.

    osmconf.ini lists the tags that GDAL turns into columns of their own for each layer.
    Column names have "_" in place of ":", so the piste_type column holds piste:type. All
    other tags are in the other_tags column.

    Args:
        layer: The layer name, which is also its section in osmconf.ini (e.g. "lines").

    Returns:
        Column name to OSM key, e.g. {"name": "name", "piste_type": "piste:type", ...}.
    """
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(OSMCONF)
    return {k.replace(":", "_"): k for k in cp[layer]["attributes"].split(",")}


def parse_other_tags(text) -> dict:
    """Read the other_tags column of a row into a dict.

    other_tags is written as a Python-style dict ({'sport': 'skiing'}), not strict JSON,
    so it is read with ast.literal_eval, falling back to json.loads.

    Args:
        text: The row's other_tags value. May be None, NaN or empty.

    Returns:
        Tag key to value, both as strings. Empty if the row has no other tags.

    Raises:
        json.JSONDecodeError: If the text is neither a Python literal nor JSON.
    """
    if not present(text) or not str(text).strip():
        return {}
    try:
        tags = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        tags = json.loads(text)
    return {str(k): str(v) for k, v in tags.items()}


def format_other_tags(tags: dict) -> str | None:
    """Write tags in the other_tags format, which parse_other_tags reads back.

    Args:
        tags: Tag key to value, for the tags that have no column of their own.

    Returns:
        The tags as a Python-style dict sorted by key, or None if there are none.
    """
    return repr(dict(sorted(tags.items()))) if tags else None


def row_tags(row: dict, layer: str) -> dict:
    """Collect every OSM tag of a row, from its tag columns and its other_tags.

    Args:
        row: One row of the layer, column name to value.
        layer: The layer the row comes from, which decides its tag columns (see
            layer_columns).

    Returns:
        OSM key to value, as strings, for every tag the row has.
    """
    cols = layer_columns(layer)
    tags = {key: str(row[col]) for col, key in cols.items() if col in row and present(row[col])}
    tags.update(parse_other_tags(row.get("other_tags")))
    return tags


def normalize_difficulty(value: str) -> str | None:
    """Turn a piste:difficulty value into one of DIFFICULTY_ORDER.

    Case is ignored, DIFFICULTY_ALIASES are applied, and if the whole value isn't a
    difficulty its first word is tried: "Easy (green circle)" gives "easy".

    Args:
        value: A piste:difficulty tag value.

    Returns:
        The standard difficulty, or None if the value isn't one.
    """
    s = value.strip().lower()
    s = DIFFICULTY_ALIASES.get(s, s)
    if s in DIFFICULTY_RANK:
        return s
    word = re.match(r"[a-z]*", s).group(0)       # "easy (green circle)" -> easy
    word = DIFFICULTY_ALIASES.get(word, word)
    return word if word in DIFFICULTY_RANK else None


def split_values(key: str, value: str) -> list[str]:
    """Split the value of a multi-value tag into its separate values.

    Args:
        key: The tag key. Values of keys in PLUS_SEPARATED_KEYS are also split on "+".
        value: The tag value, e.g. "classic;skating".

    Returns:
        The values in order, stripped, without blanks.
    """
    sep = r"[;+]" if key in PLUS_SEPARATED_KEYS else r";"
    return [p.strip() for p in re.split(sep, value) if p.strip()]


def resolve(key: str, values: list[tuple[str, float]]) -> tuple[str, bool]:
    """Pick one value for a tag whose pieces disagree.

    The first rule that applies decides:
        * piste:difficulty: the hardest difficulty wins (see normalize_difficulty).
        * MULTI_VALUE_KEYS: every value is kept, the one with the most weight first.
        * YES_IF_ANY_KEYS: "yes" if any piece says yes.
        * MOST_COVERAGE_KEYS: the value with the most weight in total.
        * Otherwise: the value of the heaviest (longest or largest) single piece.

    Args:
        key: The OSM tag key.
        values: A (value, weight) pair for each piece that has the tag. The weight is the
            piece's length in metres or area in m².

    Returns:
        A (value, is_conflict) tuple. value is the chosen value. is_conflict is False only
        for a multi-value tag whose pieces hold the same values written differently
        (classic+skating, skating;classic), which doesn't need to be recorded.
    """
    if key == "piste:difficulty":
        ranked = [n for n in (normalize_difficulty(v) for v, _ in values) if n]
        if ranked:
            return max(ranked, key=DIFFICULTY_RANK.get), True
    if key in MULTI_VALUE_KEYS:
        weights: dict[str, float] = {}
        sets = set()
        for v, w in values:
            parts = split_values(key, v)
            sets.add(frozenset(parts))
            for p in parts:
                weights[p] = weights.get(p, 0) + w
        if len(sets) == 1:       # same values written differently (classic+skating, skating;classic)
            return max(values, key=lambda vw: vw[1])[0], False
        return ";".join(sorted(weights, key=lambda p: -weights[p])), True
    if key in YES_IF_ANY_KEYS and any(v.strip().lower() == "yes" for v, _ in values):
        return "yes", True
    if key in MOST_COVERAGE_KEYS:
        totals: dict[str, float] = {}
        for v, w in values:
            totals[v] = totals.get(v, 0) + w
        return max(totals, key=totals.get), True
    return max(values, key=lambda vw: vw[1])[0], True


def merge_tags(pieces: list[tuple[dict, float]]) -> tuple[dict, dict]:
    """Combine the tags of the pieces of one merged feature.

    A tag that all pieces having it agree on is kept as it is. A tag with different values
    is settled by resolve().

    Args:
        pieces: A (tags, weight) pair for each piece: its OSM tags, and its length in
            metres or area in m².

    Returns:
        A (tags, conflicts) tuple. tags is the merged feature's tags. conflicts has an
        entry {key: {"values": [each distinct value], "chosen": value}} for every key the
        pieces disagreed on, except name (name spellings go in the review files instead).
    """
    keys = list(dict.fromkeys(k for tags, _ in pieces for k in tags))
    out, conflicts = {}, {}
    for key in keys:
        values = [(tags[key], w) for tags, w in pieces if key in tags]
        distinct = list(dict.fromkeys(v for v, _ in values))
        if len(distinct) == 1:
            out[key] = distinct[0]
            continue
        chosen, is_conflict = resolve(key, values)
        out[key] = chosen
        if is_conflict and key != "name":       # name spellings are listed separately
            conflicts[key] = {"values": distinct, "chosen": chosen}
    return out, conflicts


# ---------------------------------------------------------------- finding links

@dataclass
class NearMiss:
    """A pair of touching features that were not merged (a near miss).

    Attributes:
        reason: Why they were kept apart: "qualifier differs", "name too different",
            "different kind", "lifts touch mid-way" or "twin lifts". DRAWN_TWICE marks two
            pieces of one merged line that run side by side, listed for review.
        a: Node index of the first feature (its position in the arrays passed to
            find_line_links or find_area_links).
        b: Node index of the second feature.
        distance: Distance between the two on the ground, in metres.
    """

    reason: str
    a: int
    b: int
    distance: float


@dataclass
class Links:
    """Pairs of features found to be pieces of the same feature.

    Features are identified by node index: their position in the arrays passed to
    find_line_links or find_area_links.

    Attributes:
        a: First node of each link.
        b: Second node of each link, so node a[k] links to node b[k].
        dist: Distance in metres on the ground between the two nodes of each link.
        copy_of: Node to node, for each line that is a copy of another line (the same run
            drawn twice). The copy is dropped from the merged geometry.
        connectors: (node, line) pairs. Each line is a short EPSG:4326 LineString that
            closes the gap between a line end of that node and the line it links to.
        near: NearMiss records for touching pairs that were not linked. Only filled in
            when collect_near is set.
    """

    a: np.ndarray
    b: np.ndarray
    dist: np.ndarray
    copy_of: dict = field(default_factory=dict)
    connectors: list = field(default_factory=list)
    near: list = field(default_factory=list)


def intern(values) -> tuple[np.ndarray, list]:
    """Number each distinct value, so that each one only needs comparing once.

    Args:
        values: A sequence of hashable values (NameKeys, Kinds, ...).

    Returns:
        An (ids, uniques) tuple. uniques lists each distinct value once, in order of first
        appearance. ids holds, for each value, its position in uniques.
    """
    ids: dict = {}
    out = np.empty(len(values), np.int64)
    for i, v in enumerate(values):
        out[i] = ids.setdefault(v, len(ids))
    return out, list(ids)


def compare_pairs(keys: list, kinds: list, o: np.ndarray, t: np.ndarray):
    """Compare the names and kinds of many pairs of features.

    Each distinct pair of names, and each distinct pair of kinds, is compared only once.
    This matters because the hundreds of ways of one river all share a name.

    Args:
        keys: NameKey of every node. Unnamed nodes (None) must not appear in o or t.
        kinds: Kind of every node.
        o: Node index of the first feature of each pair.
        t: Node index of the second feature of each pair (same length as o).

    Returns:
        A (match_code, near_code, kind_ok) tuple of arrays with one entry per pair.
        match_code is 0 if the names don't match, 1 if they match exactly and 2 if they
        match fuzzily. near_code is 0, or a key of NEAR_REASONS when the names come
        close without matching. kind_ok is True if the kinds can merge (see
        kinds_compatible).
    """
    kid, uniq_keys = intern(keys)
    tid, uniq_kinds = intern(kinds)

    def per_unique(ids, uniq, fn):
        """Call fn once for each distinct pair of values.

        Each pair is encoded as one integer (first id * n + second id) so that np.unique
        can find the distinct pairs.

        Args:
            ids: For every node, its value's position in uniq (see intern).
            uniq: The distinct values.
            fn: The comparison to run on two values (compare_names or kinds_compatible).

        Returns:
            A (results, inverse) tuple: fn's result for each distinct pair, and for each
            (o, t) pair the index of its result.
        """
        n = len(uniq)
        codes, inverse = np.unique(ids[o] * n + ids[t], return_inverse=True)
        results = [fn(uniq[c // n], uniq[c % n]) for c in codes]
        return results, inverse

    name_results, inv = per_unique(kid, uniq_keys, compare_names)
    match_codes = {None: 0, "exact": 1, "fuzzy": 2}
    near_codes = {None: 0, "qualifier differs": 1, "name too different": 2}
    mcode = np.array([match_codes[m] for m, _ in name_results], np.int8)[inv] if name_results else np.empty(0, np.int8)
    ncode = np.array([near_codes[r] for _, r in name_results], np.int8)[inv] if name_results else np.empty(0, np.int8)
    kind_results, inv = per_unique(tid, uniq_kinds, kinds_compatible)
    kind_ok = np.array(kind_results, bool)[inv] if kind_results else np.empty(0, bool)
    return mcode, ncode, kind_ok


def near_misses_from(rows: pd.DataFrame, reason_col: str) -> list[NearMiss]:
    """Turn candidate pairs into near-miss records, one per pair of features.

    The same pair can come up more than once (from each end of a line, or from both
    features); only the closest is kept.

    Args:
        rows: One row per candidate pair, with columns o and t (node indexes), d (distance
            in metres) and the reason column.
        reason_col: Name of the column that holds each pair's near-miss reason.

    Returns:
        One NearMiss per pair of nodes, with a < b.
    """
    if rows.empty:
        return []
    rows = rows.assign(a=np.minimum(rows.o, rows.t), b=np.maximum(rows.o, rows.t))
    best = rows.sort_values("d").drop_duplicates(["a", "b"])
    return [NearMiss(r, int(a), int(b), float(d)) for r, a, b, d in
            zip(best[reason_col], best.a, best.b, best.d)]


def find_line_links(geom: np.ndarray, geom_m: np.ndarray, kinds: list, keys: list,
                    lengths: np.ndarray, collect_near: bool = False) -> Links:
    """Find which named lines are pieces of the same feature.

    Two lines link when an end of one lies within LINE_GAP_M of the other line on the
    ground, their kinds are compatible and their names match. On top of that:
        * Lifts must meet end to end; a lift touching another mid-way doesn't link.
        * A line drawn twice is a copy (see COPY_WITHIN_M). The shorter one is recorded
          in copy_of; it still links, so its tags are merged.
        * Two linked lines that run side by side but aren't a copy are kept, and listed
          as DRAWN_TWICE near misses for review.
        * Two lifts of similar length running alongside each other are twin lifts side by
          side (see TWIN_LENGTH_RATIO) and don't link.
        * Where a linked line end doesn't already touch the other line, a straight
          connector is made to close the gap.

    Args:
        geom: Line geometries in EPSG:4326, one per node. Connectors are drawn on these.
        geom_m: The same lines in METRIC_CRS. Only named nodes need one.
        kinds: Kind of every node.
        keys: NameKey of every node, or None if it has no name. Unnamed lines never link.
        lengths: Length of every line in metres on the ground (see ground_length). Only
            named nodes are used.
        collect_near: Also record touching pairs that didn't link, and DRAWN_TWICE
            pairs, for the review file. The self-checks leave this off.

    Returns:
        The links found. Each pair of nodes appears at most once, with a < b and the
        shortest distance between them on the ground.
    """
    named = np.flatnonzero(np.array([k is not None for k in keys], bool))
    if len(named) < 2:
        return Links(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))
    g = geom_m[named]
    m = len(named)
    scale = np.ones(len(geom))
    scale[named] = stretch(geom[named])
    starts, ends = shapely.get_point(g, 0), shapely.get_point(g, -1)
    # pts holds the start of every named line, then the end of every named line, so a hit
    # on point pi comes from line pi % m, and pi // m says which end (0 start, 1 end).
    pts = np.concatenate([starts, ends])
    # The search reaches at least LINE_GAP_M on the ground; ground distances decide below.
    radius = LINE_GAP_M * np.tile(scale[named], 2)
    pi, ti = shapely.STRtree(g).query(pts, predicate="dwithin", distance=radius)
    oi, end = pi % m, pi // m
    keep = oi != ti     # a line's own ends always lie on it
    pi, ti, oi, end = pi[keep], ti[keep], oi[keep], end[keep]
    o, t = named[oi], named[ti]
    mcode, ncode, kind_ok = compare_pairs(keys, kinds, o, t)

    link = (mcode > 0) & kind_ok
    # Near misses: the names match but the kinds don't, or the kinds match and the names
    # come close.
    near = ((mcode > 0) & ~kind_ok) | (kind_ok & (ncode > 0)) if collect_near else np.zeros(len(o), bool)
    sel = np.flatnonzero(link | near)
    # d: from the line end to the other line, on the ground. Only pairs within LINE_GAP_M
    # go on.
    d = ground_distance(pts[pi[sel]], g[ti[sel]])
    sel, d = sel[d <= LINE_GAP_M], d[d <= LINE_GAP_M]
    p, tt = pts[pi[sel]], ti[sel]
    # dend: from the line end to the nearer end of the other line, and tend says which end
    # that is (0 start, 1 end).
    ds, de = ground_distance(p, starts[tt]), ground_distance(p, ends[tt])
    rows = pd.DataFrame({"o": o[sel], "t": t[sel], "end": end[sel], "d": d,
                         "dend": np.minimum(ds, de), "tend": np.where(ds <= de, 0, 1),
                         "link": link[sel], "mcode": mcode[sel], "ncode": ncode[sel],
                         "kind_ok": kind_ok[sel]})
    # A lift end that is near the other lift but not near either of its ends touches it
    # mid-way, so it doesn't link.
    e2e = np.array([end_to_end_only(k) for k in kinds], bool)
    rows.loc[e2e[rows.o.to_numpy()] & (rows.dend > LINE_GAP_M), "link"] = False

    out = Links(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))
    if collect_near:
        nr = rows[~rows.link].copy()
        nr["reason"] = nr.ncode.map(NEAR_REASONS)
        nr.loc[(nr.mcode > 0) & ~nr.kind_ok, "reason"] = "different kind"
        nr.loc[(nr.mcode > 0) & nr.kind_ok, "reason"] = "lifts touch mid-way"
        out.near.extend(near_misses_from(nr[nr.reason.notna()], "reason"))
    L = rows[rows.link]

    # Copies: an end of the shorter line close to the other, and most of it alongside (see
    # COPY_WITHIN_M). The zone alongside is COPY_WITHIN_M wide on the ground, plus 0.5 m of
    # slack.
    width = COPY_WITHIN_M + 0.5
    cand = L.loc[L.d <= COPY_WITHIN_M, ["o", "t"]].drop_duplicates().sort_values(["o", "t"])
    co, ct = cand.o.to_numpy(), cand.t.to_numpy()
    shorter = (lengths[co] < lengths[ct]) | ((lengths[co] == lengths[ct]) & (co > ct))
    co, ct = co[shorter], ct[shorter]
    # Quick test first: the part of a copy outside the zone is at least as long as its far
    # end lies beyond the zone, which rules out lines that lead away (most links).
    far = np.maximum(ground_distance(shapely.get_point(geom_m[co], 0), geom_m[ct]),
                     ground_distance(shapely.get_point(geom_m[co], -1), geom_m[ct]))
    may = far - width <= (1 - COPY_SHARE) * lengths[co]
    for oo, tt_, far_ in zip(co[may], ct[may], far[may]):
        if oo in out.copy_of:
            continue
        # The shares are measured in METRIC_CRS. Both lengths scale alike, so they are true.
        own = max(shapely.length(geom_m[oo]), 1e-9)
        zone = shapely.buffer(geom_m[tt_], width * scale[tt_])
        if shapely.length(shapely.intersection(geom_m[oo], zone)) < COPY_SHARE * own:
            continue
        if far_ > COPY_WITHIN_M:        # one end strays: it must run along the other line
            ends = [shapely.get_point(geom_m[oo], i) for i in (0, -1)]
            along = abs(shapely.line_locate_point(geom_m[tt_], ends[0])
                        - shapely.line_locate_point(geom_m[tt_], ends[1]))
            if along < COPY_SHARE * own:
                continue
        out.copy_of[int(oo)] = int(tt_)

    # Twin lifts: two lifts of similar length running alongside each other are two lifts side
    # by side, even when they were drawn in opposite directions and only one pair of ends meets.
    twins = set()
    lift_rows = L[e2e[L.o.to_numpy()]]
    for a, b in set(zip(np.minimum(lift_rows.o, lift_rows.t), np.maximum(lift_rows.o, lift_rows.t))):
        if a in out.copy_of or b in out.copy_of:
            continue
        if min(lengths[a], lengths[b]) < TWIN_LENGTH_RATIO * max(lengths[a], lengths[b]):
            continue
        apart = runs_alongside(geom_m[a], geom_m[b])
        if apart is not None:
            twins.add((int(a), int(b)))
            if collect_near:
                out.near.append(NearMiss("twin lifts", int(a), int(b), float(apart)))
    if twins:
        pair = list(zip(np.minimum(L.o, L.t), np.maximum(L.o, L.t)))
        L = L[[pq not in twins for pq in pair]]

    # Linked lines that run side by side without being a copy: one feature drawn twice (by
    # two mappers, or an import beside a newer drawing), or two real ones (tubing lanes, twin
    # culverts, one-way ski tracks). Their shapes don't tell which, so both are kept and the
    # pair is listed for review. Side by side means the shorter line runs along the longer
    # one, and lies within LINE_GAP_M of it, for TWIN_OVERLAP_SHARE of its length. A line
    # with both ends within COPY_WITHIN_M of the other leaves it and rejoins it (around an
    # island or trees), so it isn't listed.
    if collect_near and not L.empty:
        pairs = pd.DataFrame({"a": np.minimum(L.o, L.t), "b": np.maximum(L.o, L.t)}).drop_duplicates()
        pairs = pairs[~pairs.a.isin(list(out.copy_of)) & ~pairs.b.isin(list(out.copy_of))]
        a_, b_ = pairs.a.to_numpy(), pairs.b.to_numpy()
        s_ = np.where(lengths[a_] <= lengths[b_], a_, b_)
        l_ = np.where(lengths[a_] <= lengths[b_], b_, a_)
        # Shares are measured in METRIC_CRS, where both lengths scale alike.
        ss, ll, own = geom_m[s_], geom_m[l_], shapely.length(geom_m[s_])
        s0, s1 = shapely.get_point(ss, 0), shapely.get_point(ss, -1)
        along = np.abs(shapely.line_locate_point(ll, s0) - shapely.line_locate_point(ll, s1))
        k = np.flatnonzero(along >= TWIN_OVERLAP_SHARE * own)
        ss, ll, own, s0, s1 = ss[k], ll[k], own[k], s0[k], s1[k]
        nearby = shapely.length(shapely.intersection(ss, shapely.buffer(ll, LINE_GAP_M * scale[l_[k]])))
        rejoins = np.maximum(ground_distance(s0, ll), ground_distance(s1, ll)) <= COPY_WITHIN_M
        listed = (nearby >= TWIN_OVERLAP_SHARE * own) & ~rejoins
        # How far apart they run, measured at the middle of the shorter line.
        middle = shapely.line_interpolate_point(ss[listed], 0.5, normalized=True)
        apart = ground_distance(middle, ll[listed])
        for x, y, d_ in zip(s_[k][listed], l_[k][listed], apart):
            out.near.append(NearMiss(DRAWN_TWICE, int(min(x, y)), int(max(x, y)), float(d_)))

    edges = (L.assign(a=np.minimum(L.o, L.t), b=np.maximum(L.o, L.t))
             .groupby(["a", "b"], as_index=False)["d"].min())
    out.a, out.b, out.dist = edges.a.to_numpy(np.int64), edges.b.to_numpy(np.int64), edges.d.to_numpy()

    # Connectors close the gaps: from each line end to the nearest linked line, unless they
    # already share a node. End-to-end gaps get one connector, not one from each side.
    conn = L[~L.o.isin(list(out.copy_of)) & ~L.t.isin(list(out.copy_of))]
    if not conn.empty:
        best = conn.loc[conn.groupby(["o", "end"])["d"].idxmin()]
        seen = set()
        for oo, e, tt_, d_, dend, tend in zip(best.o, best.end, best.t, best.d, best.dend, best.tend):
            if d_ < SHARED_NODE_M:
                continue
            ep = shapely.get_point(geom[oo], 0 if e == 0 else -1)
            if dend - d_ < 0.01:      # nearest point is the other line's end
                key = frozenset({(oo, e), (tt_, tend)})
                line = shapely.LineString([ep, shapely.get_point(geom[tt_], 0 if tend == 0 else -1)])
            else:
                key = (oo, e, tt_)
                # The nearest point on the other line is found in METRIC_CRS, where it is
                # the nearest on the ground (in longitude/latitude it isn't), then placed
                # on the EPSG:4326 line.
                ep_m = shapely.get_point(geom_m[oo], 0 if e == 0 else -1)
                x, y = shapely.get_coordinates(shapely.shortest_line(ep_m, geom_m[tt_]))[1]
                near = shapely.Point(FROM_METRIC.transform(x, y))
                line = shapely.LineString([ep, shapely.line_interpolate_point(
                    geom[tt_], shapely.line_locate_point(geom[tt_], near))])
            if key not in seen:
                seen.add(key)
                out.connectors.append((int(oo), line))
    return out


def runs_alongside(a, b) -> float | None:
    """Check whether the shorter of two lines runs alongside the longer one.

    Both ends of the shorter line must lie within TWIN_APART_M of the longer line on the
    ground, and the stretch of the longer line between the points nearest those ends must
    be at least TWIN_OVERLAP_SHARE of the shorter line's length. Two sections of one lift
    fail this: the far end of one lies away from the other.

    Args:
        a: The first line, in METRIC_CRS.
        b: The second line, in METRIC_CRS.

    Returns:
        How far apart the two lines run, in metres (the larger distance from an end of the
        shorter line to the longer line), or None if they don't run alongside each other.
    """
    long, short = (a, b) if a.length >= b.length else (b, a)
    if short.length == 0:
        return None
    s, e = shapely.get_point(short, 0), shapely.get_point(short, -1)
    apart = ground_distance(np.array([s, e]), long).max()
    if apart > TWIN_APART_M:
        return None
    # Both lengths here are in METRIC_CRS and scale alike, so their ratio is true.
    overlap = abs(shapely.line_locate_point(long, s) - shapely.line_locate_point(long, e))
    return float(apart) if overlap >= TWIN_OVERLAP_SHARE * short.length else None


def find_area_links(geom: np.ndarray, geom_m: np.ndarray, kinds: list, keys: list,
                    collect_near: bool = False) -> Links:
    """Find which named polygons are pieces of the same feature.

    Two polygons link when they overlap or lie within POLY_GAP_M of each other on the
    ground, their kinds are compatible and their names match.

    Args:
        geom: Polygon geometries in EPSG:4326, one per node.
        geom_m: The same polygons in METRIC_CRS, valid (see shapely.make_valid).
        kinds: Kind of every node.
        keys: NameKey of every node, or None if it has no name. Unnamed polygons never link.
        collect_near: Also record touching pairs that didn't link, for the review file.

    Returns:
        The links found, with a < b. copy_of and connectors stay empty for polygons.
    """
    named = np.flatnonzero(np.array([k is not None for k in keys], bool))
    if len(named) < 2:
        return Links(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))
    g = geom_m[named]
    # The search reaches at least POLY_GAP_M on the ground; ground distances decide below.
    radius = POLY_GAP_M * stretch(geom[named])
    i, j = shapely.STRtree(g).query(g, predicate="dwithin", distance=radius)
    keep = i < j        # each pair once, and never a polygon with itself
    o, t = named[i[keep]], named[j[keep]]
    mcode, ncode, kind_ok = compare_pairs(keys, kinds, o, t)
    link = (mcode > 0) & kind_ok
    near = ((mcode > 0) & ~kind_ok) | (kind_ok & (ncode > 0)) if collect_near else np.zeros(len(o), bool)
    sel = np.flatnonzero(link | near)
    d = ground_distance(geom_m[o[sel]], geom_m[t[sel]])
    sel, d = sel[d <= POLY_GAP_M], d[d <= POLY_GAP_M]
    rows = pd.DataFrame({"o": o[sel], "t": t[sel], "d": d,
                         "link": link[sel], "mcode": mcode[sel], "ncode": ncode[sel], "kind_ok": kind_ok[sel]})
    out = Links(rows.o[rows.link].to_numpy(np.int64), rows.t[rows.link].to_numpy(np.int64),
                rows.d[rows.link].to_numpy())
    if collect_near:
        nr = rows[~rows.link].copy()
        nr["reason"] = nr.ncode.map(NEAR_REASONS)
        nr.loc[nr.mcode > 0, "reason"] = "different kind"
        out.near.extend(near_misses_from(nr[nr.reason.notna()], "reason"))
    return out


def component_labels(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Group nodes that are linked, directly or through other nodes.

    Args:
        n: Number of nodes.
        a: First node of each link.
        b: Second node of each link.

    Returns:
        A label for every node. Nodes with the same label form one merged feature; a node
        without links has a label of its own.
    """
    graph = coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n))
    return connected_components(graph, directed=False)[1]


def to_metric(geoms, crs: str = METRIC_CRS) -> np.ndarray:
    """Reproject geometries from EPSG:4326 to a projection in metres.

    Args:
        geoms: Shapely geometries in EPSG:4326 (longitude/latitude).
        crs: The projection: METRIC_CRS to find what lies near what, or AREA_CRS to
            measure areas.

    Returns:
        An object array of the reprojected geometries, in the same order.
    """
    return np.asarray(gpd.GeoSeries(list(geoms), crs=4326).to_crs(crs).array)


# The WGS84 ellipsoid, on which ground distances and lengths are measured.
GEOD = pyproj.Geod(ellps="WGS84")
# METRIC_CRS itself, for its scale factors, and the way back from it to EPSG:4326.
METRIC_PROJ = pyproj.Proj(METRIC_CRS)
FROM_METRIC = pyproj.Transformer.from_crs(METRIC_CRS, 4326, always_xy=True)


def stretch(geoms) -> np.ndarray:
    """Find how much METRIC_CRS stretches short distances on each geometry, at most.

    METRIC_CRS scales a short distance by a factor that depends only on latitude: 1 at
    20° N and 60° N, down to 0.94 near 41° N between them, and more than 1 further north
    (1.11 at 70° N). Multiplying a distance in metres by this factor gives a search radius
    in METRIC_CRS that reaches just as far on the ground. Because the factor only falls
    and then rises from south to north, its largest value on a geometry is at the
    geometry's southern or northern edge.

    Args:
        geoms: Geometries in EPSG:4326.

    Returns:
        The largest scale factor on each geometry: METRIC_CRS metres per metre on the
        ground.
    """
    west, south, _, north = shapely.bounds(geoms).T
    scale = METRIC_PROJ.get_factors(np.r_[west, west], np.r_[south, north]).parallel_scale
    return np.asarray(scale).reshape(2, -1).max(axis=0)


def ground_distance(a, b) -> np.ndarray:
    """Measure the distance on the ground between pairs of geometries in METRIC_CRS.

    The nearest points of each pair are found in METRIC_CRS, which keeps the shape of small
    things, so they are the nearest points on the ground too. The distance between them is
    then measured along the WGS84 ellipsoid.

    Args:
        a: Non-empty geometries in METRIC_CRS, or a single one.
        b: Non-empty geometries in METRIC_CRS, as many as in a, or a single one.

    Returns:
        The distance of each pair in metres.
    """
    xy = shapely.get_coordinates(shapely.shortest_line(a, b))   # each line's two ends in turn
    lon, lat = FROM_METRIC.transform(xy[:, 0], xy[:, 1])
    return np.asarray(GEOD.inv(lon[0::2], lat[0::2], lon[1::2], lat[1::2])[2])


def ground_length(geoms) -> np.ndarray:
    """Measure the length of lines on the ground, along the WGS84 ellipsoid.

    Args:
        geoms: Geometries in EPSG:4326.

    Returns:
        The length of each geometry in metres (a polygon's perimeter, 0 for a point).
    """
    return np.array([GEOD.geometry_length(g) for g in geoms], dtype=float)


# ---------------------------------------------------------------- merged features

@dataclass
class Piece:
    """One source OSM object that goes into a merged feature.

    Attributes:
        osm_type: "way" or "relation".
        osm_id: The way's or relation's OSM id.
        layer: The ski_nature.gpkg layer it was read from.
        fid: Its row id in that layer.
        name: Its own name (name, else piste:name), or None. A name inherited from a
            relation is not counted here.
        kind: Its kind label (see Kind.label), for the review files.
        tags: Its OSM tags, including any name inherited from a relation.
        size: The length in metres (lines) or area in m² (polygons) it adds. For an
            absorbed relation, only the parts that aren't already ways in the lines layer.
        role: What happened to it: "representative" (the merged feature keeps its OSM id),
            "merged", "copy dropped", "name inherited" or "relation absorbed".
        gap_m: Distance in metres to the nearest piece it links to, or None if it has no
            links of its own (an absorbed relation, or a way joined only through one).
        inherited_from: OSM id of the relation it took its name from, or None.
    """

    osm_type: str
    osm_id: int
    layer: str
    fid: int
    name: str | None
    kind: str
    tags: dict
    size: float
    role: str = "merged"
    gap_m: float | None = None
    inherited_from: int | None = None


@dataclass
class Group:
    """A merged feature: one output row made from several pieces.

    merge_line_features also sets two extra attributes on line groups for the length
    self-check: expected (total length of the pieces that weren't dropped as copies) and
    connectors (the lines added to close gaps).

    Attributes:
        layer: The layer the merged row is written to.
        kind: Kind label of the merged feature; piste:<type> once tags are merged.
        pieces: The Piece objects that went into it.
        geom: The merged geometry, in EPSG:4326.
        rep: The representative piece, whose OSM id the merged row keeps: the longest (or
            largest) piece that isn't a dropped copy.
        tags: The merged tags (see merge_tags).
        conflicts: The tags the pieces disagreed on (see merge_tags).
        match: How the names matched: "exact", "fuzzy" or "inherited" (see group_match).
        size_before: Total length (m) or area (m²) of the pieces.
        size_after: Length or area of the merged geometry.
        max_gap_m: Longest connector added to close a gap, in metres. Lines only.
        z_order: The representative's z_order, copied to the merged row. Lines only.
        fid_after: The merged row's fid in ski_nature_merged.gpkg, filled in once written.
    """

    layer: str
    kind: str
    pieces: list
    geom: object
    rep: Piece
    tags: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)
    match: str = "exact"
    size_before: float = 0.0
    size_after: float = 0.0
    max_gap_m: float = 0.0
    z_order: int | None = None
    fid_after: int | None = None

    @property
    def group_id(self) -> str:
        """The merged feature's id, e.g. "lines-3f9a0c12bd".

        Written to the merge_group column. It is the layer plus a hash of the sorted piece ids, so the same pieces get the same
        id on every run.
        """
        ids = sorted(f"{p.osm_type[0]}{p.osm_id}" for p in self.pieces)
        return f"{self.layer}-{hashlib.sha1(';'.join(ids).encode()).hexdigest()[:10]}"

    @property
    def merged_ids(self) -> str:
        """The pieces' OSM ids, representative first, written to merged_ids.

        Each id has a w (way) or r (relation) prefix, e.g. "w123;w456;r789".
        """
        rest = sorted((p for p in self.pieces if p is not self.rep), key=lambda p: (p.osm_type, p.osm_id))
        return ";".join(f"{p.osm_type[0]}{p.osm_id}" for p in [self.rep, *rest])


def group_match(pieces: list[Piece]) -> str:
    """Describe how the names of a merged feature's pieces matched.

    Args:
        pieces: The pieces of the merged feature.

    Returns:
        "fuzzy" if the pieces' own names are written differently, "inherited" if they are
        written the same but some piece took its name from a relation, else "exact".
    """
    names = {p.name for p in pieces if p.name}
    if len(names) > 1:
        return "fuzzy"
    if any(p.inherited_from is not None for p in pieces):
        return "inherited"
    return "exact"


def finish_group(g: Group, weights: list[float]) -> Group:
    """Fill in a group's merged tags, tag conflicts and name match.

    Args:
        g: The group. It is changed in place.
        weights: Length or area of each piece, in the order of g.pieces, used to settle
            tag conflicts.

    Returns:
        The same group. Its kind becomes piste:<type> if the merged tags have a piste:type.
    """
    tagged = [(p.tags, w) for p, w in zip(g.pieces, weights)]
    g.tags, g.conflicts = merge_tags(tagged)
    g.match = group_match(g.pieces)
    if g.tags.get("piste:type"):
        g.kind = f"piste:{g.tags['piste:type']}"
    return g


def line_parts(geom) -> list:
    """Return the non-empty LineStrings among a geometry's parts.

    Args:
        geom: A LineString, MultiLineString or GeometryCollection. Parts that aren't
            LineStrings are left out.

    Returns:
        A list of LineStrings.
    """
    return [p for p in shapely.get_parts(geom) if p.geom_type == "LineString" and not p.is_empty]


def merge_lines(parts: list):
    """Join line parts into as few lines as possible.

    Parts are first joined only where one ends where the next starts, which keeps their
    direction (OSM draws rivers in the direction they flow, and pistes usually downhill).
    If ignoring direction gives fewer lines, that result is used instead.

    Args:
        parts: LineStrings in EPSG:4326, including any connectors.

    Returns:
        A LineString if the parts form one chain, else a MultiLineString (a forked run, for
        example).
    """
    multi = shapely.MultiLineString(parts)
    merged = shapely.line_merge(multi, directed=True)
    if merged.geom_type != "LineString":
        undirected = shapely.line_merge(multi)
        if len(shapely.get_parts(undirected)) < len(shapely.get_parts(merged)):
            merged = undirected
    return merged


# ---------------------------------------------------------------- reading

@dataclass
class Relation:
    """A route, piste or waterway relation and its member ways.

    Attributes:
        layer: The layer it was read from: "multilinestrings" or "other_relations".
        fid: Its row id in that layer.
        osm_id: Its OSM relation id.
        tags: Its OSM tags.
        key: Its normalized name (name, else piste:name), or None if it has no name.
        kind: Its kind (see relation_kind).
        geom: Its geometry in EPSG:4326, as GDAL built it from the member ways.
        members: Node indexes (row positions in the lines layer) of its member ways that
            are in the lines layer. Filled in by merge_line_features.
    """

    layer: str
    fid: int
    osm_id: int
    tags: dict
    key: NameKey | None
    kind: Kind
    geom: object
    members: list = field(default_factory=list)


def read_relations(src: Path) -> list[Relation]:
    """Read the relations that can share a name with their ways.

    Which layers and relation types are read is set by RELATION_TYPES.

    Args:
        src: Path to ski_nature.gpkg.

    Returns:
        One Relation per row read, with members still empty.
    """
    out = []
    for layer, types in RELATION_TYPES.items():
        where = None if types is None else "type IN (" + ", ".join(f"'{t}'" for t in types) + ")"
        df = pyogrio.read_dataframe(src, layer=layer, where=where, fid_as_index=True)
        for fid, row in zip(df.index, df.drop(columns="geometry").to_dict("records")):
            tags = row_tags(row, layer)
            out.append(Relation(layer, int(fid), int(row["osm_id"]), tags,
                                name_key(tags.get("name") or tags.get("piste:name")),
                                relation_kind(tags), df.geometry.loc[fid]))
    return out


def read_relation_members(pbf: Path, wanted: set[int]) -> dict[int, list[int]]:
    """Read the member way ids of some relations from the OSM file.

    The GeoPackage has each relation's geometry but not its list of members, so the members
    are read from the .osm.pbf file with pyosmium.

    Args:
        pbf: Path to near_runs.osm.pbf.
        wanted: OSM ids of the relations to look up.

    Returns:
        Relation id to the ids of its member ways, in member order. Node and relation
        members are left out, and relations not found in the file are missing.
    """
    members: dict[int, list[int]] = {}

    class Handler(osmium.SimpleHandler):
        """Collects the member way ids of the wanted relations into members."""

        def relation(self, r):
            """Record the member ways of r if it is a wanted relation."""
            if r.id in wanted:
                members[r.id] = [m.ref for m in r.members if m.type == "w"]

    Handler().apply_file(str(pbf))
    return members


def fetch_rows(src: Path, layer: str, fids) -> dict[int, dict]:
    """Read the attribute columns (everything but the geometry) of some rows.

    Rows are read 30,000 at a time to stay under SQLite's limit on query parameters.

    Args:
        src: Path to the GeoPackage.
        layer: The layer to read from.
        fids: The rows' fids, in any order. Duplicates are fine.

    Returns:
        fid to row, where each row maps column name to value (fid included).
    """
    fids = sorted(set(int(f) for f in fids))
    out = {}
    with connect_ro(src) as con:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{layer}")') if r[1] != "geom"]
        sel = ", ".join(f'"{c}"' for c in cols)
        for i in range(0, len(fids), 30_000):
            chunk = fids[i:i + 30_000]
            q = f'SELECT {sel} FROM "{layer}" WHERE fid IN ({",".join("?" * len(chunk))})'
            for r in con.execute(q, chunk):
                out[r[0]] = dict(zip(cols, r))
    return out


def endpoint_pair(line) -> tuple:
    """Return a line's first and last points, rounded to about 1 cm.

    Coordinates are rounded to 7 decimal places. Used to tell which parts of a relation's
    geometry are its member ways.

    Args:
        line: A LineString in EPSG:4326.

    Returns:
        A ((x, y), (x, y)) tuple: the start point, then the end point.
    """
    c = shapely.get_coordinates(line)
    return tuple(np.round(c[0], 7)), tuple(np.round(c[-1], 7))


# ---------------------------------------------------------------- the lines layer + relations

@dataclass
class Result:
    """Everything the merge found, collected across layers.

    Attributes:
        groups: The merged features (Group objects) of every layer.
        near: One dict per near miss, in the merge_near_misses.csv format (see near_row).
        drops: Layer name to the fids of the input rows that merged rows replace.
        counts: Per-layer counts for the manifest (rows read, groups, pieces, ...).
        failures: Self-check problems. If there are any, nothing is written.
    """

    groups: list = field(default_factory=list)
    near: list = field(default_factory=list)
    drops: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)


def near_row(reason: str, layer: str, a: dict, b: dict, distance: float, geom) -> dict:
    """Build one row of merge_near_misses.csv.

    Args:
        reason: Why the two features weren't merged, or DRAWN_TWICE (see
            NearMiss.reason).
        layer: The layer they are in.
        a: The first feature, as a dict with keys osm_type, osm_id, name and kind.
        b: The second feature, with the same keys.
        distance: The distance between them, in metres (for DRAWN_TWICE, how far apart
            they run at the middle of the shorter one).
        geom: Geometry (EPSG:4326) for the map link to show.

    Returns:
        The row, column name to value.
    """
    return {"reason": reason, "layer": layer,
            "kind_a": a["kind"], "osm_type_a": a["osm_type"], "osm_id_a": a["osm_id"],
            "osm_link_a": osm_link(a["osm_type"], a["osm_id"]), "name_a": a["name"],
            "kind_b": b["kind"], "osm_type_b": b["osm_type"], "osm_id_b": b["osm_id"],
            "osm_link_b": osm_link(b["osm_type"], b["osm_id"]), "name_b": b["name"],
            "distance_m": round(distance, 1), "map_link": map_link(geom)}


def merge_line_features(src: Path, pbf: Path, result: Result) -> None:
    """Merge the lines layer and the route and waterway relations.

    The steps are:
        1. Read every line's kind and name, and the relations with their member ways.
        2. R1: an unnamed way in exactly one named relation of a matching kind takes the
           relation's name.
        3. R2: a relation whose member ways all carry its name is the same feature as
           those ways. It is absorbed: merged into them and dropped from its own layer.
           Parts of its geometry that aren't ways in the lines layer become extra nodes.
        4. Link touching lines with matching names (find_line_links), group the linked
           nodes, and build one merged feature per group.
        5. Self-checks: each merged line is as long as its pieces plus connectors, and
           running the matching again on the result finds nothing left to merge.
        6. R3: merge touching same-name relations that weren't absorbed
           (merge_relation_features).

    Nodes are numbered in the order of the lines layer's rows (0 to n_lines - 1),
    followed by the extra relation parts.

    Args:
        src: Path to ski_nature.gpkg.
        pbf: Path to near_runs.osm.pbf, for the relations' member lists.
        result: Collects the groups, dropped fids, near misses, counts and self-check
            failures. Changed in place.
    """
    started = time.time()
    cols = ["osm_id", "name", "piste_name", "piste_type", "aerialway", "railway", "waterway", "highway",
            "man_made", "natural", "barrier", "power"]
    df = pyogrio.read_dataframe(src, layer="lines", columns=cols, fid_as_index=True)
    n_lines = len(df)
    col2key = layer_columns("lines")
    kind_cols = [c for c in cols if c not in ("osm_id", "name", "piste_name")]
    kind_vals = df[kind_cols].to_numpy(object)

    def kind_tags(i: int) -> dict:
        """Return the tags of line i that line_kind looks at, OSM key to value."""
        return {col2key[c]: v for c, v in zip(kind_cols, kind_vals[i]) if present(v)}

    kinds = [line_kind(kind_tags(i)) for i in range(len(df))]
    own_names = [n if present(n) else (pn if present(pn) else None)
                 for n, pn in zip(df["name"], df["piste_name"])]
    own_keys = [name_key(n) for n in own_names]
    osm_ids = df["osm_id"].astype(np.int64).to_numpy()
    fids = df.index.to_numpy(np.int64)
    geom = np.asarray(df.geometry.array)
    way_index = {w: i for i, w in enumerate(osm_ids)}
    log.info("lines: %d read (%d named) in %.0f s", n_lines, sum(k is not None for k in own_keys),
             time.time() - started)

    # Relations and their member ways.
    relations = read_relations(src)
    members = read_relation_members(pbf, {r.osm_id for r in relations})
    for r in relations:
        r.members = [way_index[w] for w in members.get(r.osm_id, []) if w in way_index]
    log.info("relations: %d route/waterway relations, %d with member ways in lines",
             len(relations), sum(bool(r.members) for r in relations))

    # R1: an unnamed way in exactly one named relation (of a matching kind) takes its name.
    claims: dict[int, list[Relation]] = {}
    for r in relations:
        if r.key is None:
            continue
        for i in r.members:
            if own_keys[i] is None and relation_accepts(r.kind, kinds[i]):
                claims.setdefault(i, []).append(r)
    # A way claimed by two or more different relations stays unnamed.
    inherited = {i: rs[0] for i, rs in claims.items() if len(set(id(r) for r in rs)) == 1}
    keys = list(own_keys)
    for i, r in inherited.items():
        keys[i] = r.key

    # R2: a relation whose member ways all carry its name is the same feature as those ways.
    # A relation that isn't absorbed but shares a name with one of its ways is listed in
    # the near-miss file as "relation kept".
    absorbed: list[Relation] = []
    kept_with_match: list[tuple[Relation, int]] = []
    for r in relations:
        if r.key is None or not r.members:
            continue
        ok = all(keys[i] is not None and relation_accepts(r.kind, kinds[i])
                 and compare_names(keys[i], r.key)[0] for i in r.members)
        if ok:
            absorbed.append(r)
        else:
            same = [i for i in r.members if own_keys[i] is not None and compare_names(own_keys[i], r.key)[0]]
            if same:
                kept_with_match.append((r, same[0]))

    # The ways of an absorbed relation link as the kind the merged feature will have: paths
    # in a nordic piste relation become nordic pistes, so they also join same-name pistes.
    link_kinds = list(kinds)
    rel_kind: dict[int, Kind] = {}
    for r in absorbed:
        rel_tags = {k: v for k, v in r.tags.items() if k not in RELATION_ONLY_KEYS}
        pieces = [(kind_tags(i), float(length)) for i, length in zip(r.members, ground_length(geom[r.members]))]
        pieces.append((rel_tags, 0.0))
        rel_kind[id(r)] = line_kind(merge_tags(pieces)[0])
        for i in r.members:
            link_kinds[i] = rel_kind[id(r)]

    # Parts of an absorbed relation that aren't ways in lines (untagged ways, or ways just
    # outside the 10-mile zone) become extra nodes, so they can link like any other piece.
    v_geom, v_kind, v_key, v_rel = [], [], [], []
    for r in absorbed:
        covered = set()
        for i in r.members:
            s, e = endpoint_pair(geom[i])
            covered |= {(s, e), (e, s)}
        for part in line_parts(r.geom):
            if endpoint_pair(part) not in covered:
                v_geom.append(part)
                v_kind.append(rel_kind[id(r)])
                v_key.append(r.key)
                v_rel.append(r)
    n = n_lines + len(v_geom)
    all_geom = np.concatenate([geom, np.array(v_geom, dtype=object)]) if v_geom else geom
    all_kinds = link_kinds + v_kind
    all_keys = keys + v_key
    # Only named nodes can link, so only they need metric geometry and lengths.
    named = np.flatnonzero(np.array([k is not None for k in all_keys], bool))
    geom_m = np.empty(n, dtype=object)
    geom_m[named] = to_metric(all_geom[named])
    lengths = np.zeros(n)
    lengths[named] = ground_length(all_geom[named])
    log.info("inherited names: %d ways; relations absorbed: %d (%d extra parts); metric transform done (%.0f s)",
             len(inherited), len(absorbed), len(v_geom), time.time() - started)

    links = find_line_links(all_geom, geom_m, all_kinds, all_keys, lengths, collect_near=True)
    a, b = [links.a], [links.b]
    rel_nodes: dict[int, list[int]] = {}      # id(relation) -> its extra-part nodes
    for k, r in enumerate(v_rel):
        rel_nodes.setdefault(id(r), []).append(n_lines + k)
    # An absorbed relation's ways and extra parts form one group even where they don't
    # touch: link its first node to each of the others.
    for r in absorbed:
        nodes = r.members + rel_nodes.get(id(r), [])
        a.append(np.full(len(nodes) - 1, nodes[0], np.int64))
        b.append(np.array(nodes[1:], np.int64))
    labels = component_labels(n, np.concatenate(a), np.concatenate(b))
    log.info("lines: %d links, %d copies, %d connectors (%.0f s)", len(links.a), len(links.copy_of),
             len(links.connectors), time.time() - started)

    # Group the nodes by component.
    sizes = np.bincount(labels, minlength=n)
    rel_by_label: dict[int, list[Relation]] = {}
    for r in absorbed:
        rel_by_label.setdefault(int(labels[r.members[0]]), []).append(r)
    # Nodes to merge: those in a component of two or more nodes, plus the single way of a
    # relation with one member (the relation is still absorbed into it).
    grouped = np.flatnonzero((sizes[labels] > 1) | np.isin(labels, list(rel_by_label)))
    by_label: dict[int, list[int]] = {}
    for i in grouped:
        by_label.setdefault(int(labels[i]), []).append(int(i))
    connectors_by_label: dict[int, list] = {}
    for node, line in links.connectors:
        connectors_by_label.setdefault(int(labels[node]), []).append(line)
    gap = {}    # node -> distance to the nearest node it links to (Piece.gap_m)
    for x, y, d in zip(links.a, links.b, links.dist):
        gap[x] = min(gap.get(x, d), d)
        gap[y] = min(gap.get(y, d), d)

    way_nodes = [i for i in grouped if i < n_lines]
    rows = fetch_rows(src, "lines", fids[way_nodes])
    groups = []
    for label, nodes in by_label.items():
        ways = [i for i in nodes if i < n_lines]
        rels = rel_by_label.get(label, [])
        pieces, weights, parts = [], [], []
        for i in ways:
            row = rows[int(fids[i])]
            tags = row_tags(row, "lines")
            if i in inherited:
                tags["name"] = inherited[i].key.raw
            p = Piece("way", int(osm_ids[i]), "lines", int(fids[i]), own_names[i], kinds[i].label,
                      tags, float(lengths[i]), gap_m=gap.get(i),
                      inherited_from=inherited[i].osm_id if i in inherited else None)
            if i in links.copy_of:
                p.role = "copy dropped"
            else:
                parts.append(geom[i])
                if p.inherited_from is not None:
                    p.role = "name inherited"
            pieces.append(p)
            weights.append(p.size)
        # The representative is the longest way that isn't a dropped copy.
        rep = max(pieces, key=lambda p: (p.role != "copy dropped", p.size))
        if rep.role != "copy dropped":
            rep.role = "representative"
        for r in rels:
            extra = [k for k in rel_nodes.get(id(r), [])]
            size = float(sum(lengths[k] for k in extra if k not in links.copy_of))
            parts.extend(all_geom[k] for k in extra if k not in links.copy_of)
            tags = {k: v for k, v in r.tags.items() if k not in RELATION_ONLY_KEYS}
            pieces.append(Piece("relation", r.osm_id, r.layer, r.fid, r.tags.get("name") or r.tags.get("piste:name"),
                                r.kind.label, tags, size, role="relation absorbed"))
            weights.append(size)
        conns = connectors_by_label.get(label, [])
        merged = merge_lines(parts + conns)
        g = Group("lines", kinds[rep_index(ways, osm_ids, rep)].label, pieces, merged, rep,
                  size_before=float(sum(p.size for p in pieces)),
                  z_order=int(rows[rep.fid].get("z_order") or 0))
        # Extra attributes for the length self-check below.
        g.expected = float(sum(p.size for p in pieces if p.role != "copy dropped"))   # type: ignore[attr-defined]
        g.connectors = conns                                                          # type: ignore[attr-defined]
        groups.append(finish_group(g, weights))

    # Lengths after merging, and the length self-check.
    if groups:
        after = ground_length([g.geom for g in groups])
        conn_all = [c for g in groups for c in g.connectors]
        conn_len = ground_length(conn_all)
        k = 0
        for g, length in zip(groups, after):
            cl = conn_len[k:k + len(g.connectors)]
            k += len(g.connectors)
            g.size_after = float(length)
            g.max_gap_m = float(cl.max()) if len(cl) else 0.0
            expected = g.expected + float(cl.sum())
            if abs(length - expected) > LENGTH_TOLERANCE * expected + 1.0:
                result.failures.append(f"lines group {g.group_id}: merged length {length:.0f} m, "
                                       f"pieces + connectors {expected:.0f} m")
    result.groups.extend(groups)
    result.drops.setdefault("lines", []).extend(int(fids[i]) for i in grouped if i < n_lines)
    result.drops.setdefault("multilinestrings", []).extend(r.fid for r in absorbed if r.layer == "multilinestrings")
    result.drops.setdefault("other_relations", []).extend(r.fid for r in absorbed if r.layer == "other_relations")

    # Near misses, described by node.
    def node_info(i: int) -> dict:
        """Describe node i (a way or an extra relation part) for near_row."""
        if i < n_lines:
            return {"osm_type": "way", "osm_id": int(osm_ids[i]), "name": own_names[i] or "", "kind": kinds[i].label}
        r = v_rel[i - n_lines]
        return {"osm_type": "relation", "osm_id": r.osm_id, "name": r.key.raw if r.key else "", "kind": r.kind.label}

    for nm in links.near:
        a_info, b_info = node_info(nm.a), node_info(nm.b)
        if (a_info["osm_type"], a_info["osm_id"]) == (b_info["osm_type"], b_info["osm_id"]):
            continue        # two parts of one relation
        result.near.append(near_row(nm.reason, "lines", a_info, b_info, nm.distance, all_geom[nm.a]))
    for r, i in kept_with_match:
        result.near.append(near_row("relation kept", r.layer,
                                    {"osm_type": "relation", "osm_id": r.osm_id, "name": r.key.raw, "kind": r.kind.label},
                                    node_info(i), 0.0, geom[i]))
    result.counts["lines"] = {"read": n_lines, "named": int(sum(k is not None for k in own_keys)),
                              "inherited_names": len(inherited), "relations_absorbed": len(absorbed),
                              "copies_dropped": len(links.copy_of), "connectors": len(links.connectors),
                              "groups": len(groups), "pieces": int(len(way_nodes))}

    # Self-check: run the same matching on the result. Nothing should link any more, apart
    # from the parts of one forked feature. f_owner says where each line came from: the
    # index of its merged group, or -1 - node for a named way that wasn't merged.
    grouped_set = set(grouped.tolist())
    keep = [i for i in range(n_lines) if i not in grouped_set and own_keys[i] is not None]
    f_geom, f_kinds, f_keys, f_owner = [], [], [], []
    for i in keep:
        f_geom.append(geom[i]); f_kinds.append(kinds[i]); f_keys.append(own_keys[i]); f_owner.append(-1 - i)
    for k, g in enumerate(groups):
        kind, key = line_kind(g.tags), name_key(g.tags.get("name") or g.tags.get("piste:name"))
        for part in line_parts(g.geom):
            f_geom.append(part); f_kinds.append(kind); f_keys.append(key); f_owner.append(k)
    f_geom = np.array(f_geom, dtype=object)
    f_m = to_metric(f_geom)
    again = find_line_links(f_geom, f_m, f_kinds, f_keys, ground_length(f_geom))
    owner = np.array(f_owner)
    clash = [(x, y) for x, y in zip(again.a, again.b) if owner[x] != owner[y]]

    def describe(x: int) -> str:
        """Describe line x of the self-check for a failure message."""
        k = owner[x]
        what = (f"merged {groups[k].group_id}" if k >= 0 else f"way/{osm_ids[-1 - k]} (fid {fids[-1 - k]})")
        return f"{f_keys[x].raw!r} {f_kinds[x].label} [{what}]"

    for x, y in clash[:20]:
        result.failures.append(f"lines still link after merging: {describe(x)} and {describe(y)} "
                               f"near {map_link(f_geom[x])}")
    if len(clash) > 20:
        result.failures.append(f"... {len(clash) - 20} more lines still link after merging")
    log.info("lines: %d merged features from %d pieces, %d relations absorbed (%.0f s)",
             len(groups), len(way_nodes), len(absorbed), time.time() - started)

    merge_relation_features(relations, absorbed, result)


def rep_index(ways: list[int], osm_ids: np.ndarray, rep: Piece) -> int:
    """Find the node index of a group's representative way.

    Args:
        ways: Node indexes of the group's ways.
        osm_ids: OSM way id of every node in the lines layer.
        rep: The group's representative piece, which is always a way.

    Returns:
        The node in ways whose OSM id is the representative's.
    """
    return next(i for i in ways if osm_ids[i] == rep.osm_id)


def merge_relation_features(relations: list[Relation], absorbed: list[Relation], result: Result) -> None:
    """R3: merge leftover relations that share a name and touch.

    Relations already absorbed into their ways are skipped. Within each relation layer,
    relations whose lines lie within LINE_GAP_M of each other on the ground, with matching
    names and compatible kinds, become one relation. Their lines are joined (see
    merge_lines); in other_relations, any parts that aren't LineStrings are kept alongside.

    Args:
        relations: Every relation read by read_relations.
        absorbed: The relations already merged into their member ways.
        result: Collects the new groups and the dropped fids. Changed in place.
    """
    taken = {id(r) for r in absorbed}
    for layer in RELATION_TYPES:
        rels = [r for r in relations if r.layer == layer and id(r) not in taken and r.key is not None]
        if len(rels) < 2:
            continue
        lines_only = np.array([shapely.MultiLineString(line_parts(r.geom)) if line_parts(r.geom) else r.geom
                               for r in rels], dtype=object)
        gm = to_metric(lines_only)
        radius = LINE_GAP_M * stretch(lines_only)
        i, j = shapely.STRtree(gm).query(gm, predicate="dwithin", distance=radius)
        keep = i < j
        i, j = i[keep], j[keep]
        keep = ground_distance(gm[i], gm[j]) <= LINE_GAP_M
        i, j = i[keep], j[keep]
        ok = [compare_names(rels[x].key, rels[y].key)[0] is not None and kinds_compatible(rels[x].kind, rels[y].kind)
              for x, y in zip(i, j)]
        i, j = i[ok], j[ok]
        if not len(i):
            continue
        labels = component_labels(len(rels), i, j)
        lengths = ground_length(lines_only)
        for label in np.unique(labels[np.concatenate([i, j])]):
            members = [k for k in range(len(rels)) if labels[k] == label]
            pieces = [Piece("relation", rels[k].osm_id, layer, rels[k].fid, rels[k].key.raw, rels[k].kind.label,
                            rels[k].tags, float(lengths[k])) for k in members]
            rep = max(pieces, key=lambda p: p.size)
            rep.role = "representative"
            parts = [p for k in members for p in line_parts(rels[k].geom)]
            other = [p for k in members for p in shapely.get_parts(rels[k].geom) if p.geom_type != "LineString"]
            merged = merge_lines(parts) if parts else None
            merged_parts = line_parts(merged) if merged is not None else []
            if layer == "multilinestrings":
                out_geom = shapely.MultiLineString(merged_parts)
            else:
                out_geom = shapely.GeometryCollection(merged_parts + other)
            g = Group(layer, rels[members[0]].kind.label, pieces, out_geom, rep,
                      size_before=float(sum(p.size for p in pieces)))
            g.size_after = float(ground_length([out_geom])[0])
            result.groups.append(finish_group(g, [p.size for p in pieces]))
            result.drops.setdefault(layer, []).extend(p.fid for p in pieces)
        log.info("%s: merged %d touching same-name relations into %d", layer,
                 len(set(np.concatenate([i, j]))), len(np.unique(labels[np.concatenate([i, j])])))


# ---------------------------------------------------------------- the multipolygons layer

def merge_area_features(src: Path, result: Result) -> None:
    """Merge named polygons in the multipolygons layer that are one feature.

    Linked polygons (see find_area_links) are unioned into one MultiPolygon. Woods and
    forests get the kind "woodland", and grass, meadow and grassland the kind "grassland".
    Self-checks: each merged area lies between its largest piece's area and the sum of its
    pieces' areas (overlaps count once), and nothing links any more after merging.

    Args:
        src: Path to ski_nature.gpkg.
        result: Collects the groups, dropped fids, near misses, counts and self-check
            failures. Changed in place.
    """
    started = time.time()
    df = pyogrio.read_dataframe(src, layer="multipolygons", where="name IS NOT NULL OR piste_name IS NOT NULL",
                                fid_as_index=True)
    records = df.drop(columns="geometry").to_dict("records")
    tags = [row_tags(r, "multipolygons") for r in records]
    kinds = [area_kind(t) for t in tags]
    keys = [name_key(t.get("name") or t.get("piste:name")) for t in tags]
    geom = shapely.make_valid(np.asarray(df.geometry.array))
    geom_m = shapely.make_valid(to_metric(geom))
    areas = shapely.area(shapely.make_valid(to_metric(geom, AREA_CRS)))
    fids = df.index.to_numpy(np.int64)
    links = find_area_links(geom, geom_m, kinds, keys, collect_near=True)
    labels = component_labels(len(df), links.a, links.b)
    gap = {}
    for x, y, d in zip(links.a, links.b, links.dist):
        gap[x] = min(gap.get(x, d), d)
        gap[y] = min(gap.get(y, d), d)

    def ident(r: dict) -> tuple[str, int]:
        """Return a polygon row's (OSM type, OSM id).

        GDAL fills osm_id for polygons built from relations and osm_way_id for polygons
        built from closed ways.

        Args:
            r: One row of the multipolygons layer, column name to value.
        """
        return ("relation", int(r["osm_id"])) if present(r.get("osm_id")) else ("way", int(r["osm_way_id"]))

    groups = []
    for label in np.unique(labels[np.concatenate([links.a, links.b])]) if len(links.a) else []:
        members = np.flatnonzero(labels == label)
        pieces = []
        for k in members:
            ot, oid = ident(records[k])
            pieces.append(Piece(ot, oid, "multipolygons", int(fids[k]), keys[k].raw, kinds[k].label,
                                tags[k], float(areas[k]), gap_m=gap.get(k)))
        rep = max(pieces, key=lambda p: p.size)
        rep.role = "representative"
        merged = polygonal(shapely.make_valid(shapely.union_all(geom[members])))
        g = Group("multipolygons", kinds[members[0]].family if kinds[members[0]].family in ("woodland", "grassland")
                  else kinds[members[0]].label, pieces, merged, rep,
                  size_before=float(sum(p.size for p in pieces)))
        groups.append(finish_group(g, [p.size for p in pieces]))
    if groups:
        for g, area in zip(groups, shapely.area(to_metric([g.geom for g in groups], AREA_CRS))):
            g.size_after = float(area)
            biggest = max(p.size for p in g.pieces)
            if area > g.size_before * (1 + LENGTH_TOLERANCE) + 1 or area < biggest * (1 - LENGTH_TOLERANCE) - 1:
                result.failures.append(f"multipolygons group {g.group_id}: merged area {area:.0f} m², "
                                       f"pieces {g.size_before:.0f} m² (largest {biggest:.0f} m²)")
    result.groups.extend(groups)
    dropped = [p.fid for g in groups for p in g.pieces]
    result.drops.setdefault("multipolygons", []).extend(dropped)

    def info(k: int) -> dict:
        """Describe polygon k for near_row."""
        ot, oid = ident(records[k])
        return {"osm_type": ot, "osm_id": oid, "name": keys[k].raw if keys[k] else "", "kind": kinds[k].label}

    for nm in links.near:
        result.near.append(near_row(nm.reason, "multipolygons", info(nm.a), info(nm.b), nm.distance, geom[nm.a]))
    result.counts["multipolygons"] = {"named_read": len(df), "groups": len(groups), "pieces": len(dropped)}

    # Self-check: nothing links any more.
    in_group = set(dropped)
    keep = [k for k in range(len(df)) if int(fids[k]) not in in_group]
    merged_m = list(to_metric([g.geom for g in groups])) if groups else []
    f_geom = np.array([geom[k] for k in keep] + [g.geom for g in groups], dtype=object)
    f_m = np.array([geom_m[k] for k in keep] + merged_m, dtype=object)
    f_kinds = [kinds[k] for k in keep] + [area_kind(g.tags) for g in groups]
    f_keys = [keys[k] for k in keep] + [name_key(g.tags.get("name") or g.tags.get("piste:name")) for g in groups]
    again = find_area_links(f_geom, shapely.make_valid(f_m), f_kinds, f_keys)
    for x, y in list(zip(again.a, again.b))[:20]:
        result.failures.append(f"polygons still link after merging: {f_keys[x].raw!r} and {f_keys[y].raw!r}")
    log.info("multipolygons: %d merged features from %d pieces (%.0f s)", len(groups), len(dropped),
             time.time() - started)


# ---------------------------------------------------------------- writing

def output_frame(groups: list[Group], layer: str, columns: list[str]) -> gpd.GeoDataFrame:
    """Build one layer's merged rows, ready to append to the output.

    Tags that have a column of their own go in that column and the rest go in other_tags.
    The id columns get the representative's OSM id, and the provenance columns are filled
    in from the group.

    Args:
        groups: The merged features of this layer.
        layer: The layer name.
        columns: The layer's output columns, in order, without fid and geom.

    Returns:
        A GeoDataFrame in EPSG:4326 with exactly those columns plus the geometry, one row
        per group.
    """
    col2key = layer_columns(layer)
    rows = []
    for g in groups:
        tags = dict(g.tags)
        row = {}
        for col in columns:
            if col in col2key:
                row[col] = tags.pop(col2key[col], None)
        for col in columns:
            if col in row:
                continue
            # In multipolygons, osm_id is only set for relations and osm_way_id for
            # closed ways, as GDAL writes them.
            if col == "osm_id":
                row[col] = str(g.rep.osm_id) if (layer != "multipolygons" or g.rep.osm_type == "relation") else None
            elif col == "osm_way_id":
                row[col] = str(g.rep.osm_id) if g.rep.osm_type == "way" else None
            elif col == "z_order":
                row[col] = g.z_order or 0
            elif col == "merge_group":
                row[col] = g.group_id
            elif col == "merged_ids":
                row[col] = g.merged_ids
            elif col == "merged_count":
                row[col] = len(g.pieces)
            elif col == "merge_match":
                row[col] = g.match
            elif col == "merge_conflicts":
                row[col] = json.dumps(g.conflicts, ensure_ascii=False, sort_keys=True) if g.conflicts else None
            else:
                row[col] = None
        row["other_tags"] = format_other_tags(tags)
        rows.append(row)
    frame = pd.DataFrame(rows, columns=columns)
    for col in columns:
        if col not in ("z_order", "merged_count"):
            frame[col] = frame[col].astype(object)
    return gpd.GeoDataFrame(frame, geometry=[g.geom for g in groups], crs=4326)


def write_output(src: Path, dst: Path, result: Result) -> dict:
    """Write ski_nature_merged.gpkg, with merged rows in place of their pieces.

    Every layer of src is copied to a temporary file, keeping its fids. The merged layers
    get the provenance columns, lose the rows that were merged, and gain the merged rows.
    Each merged row's fid is then read back into its group (Group.fid_after) and the row
    counts are checked. The temporary file replaces dst only if every check passes;
    otherwise it is left in place for inspection.

    Args:
        src: Path to ski_nature.gpkg.
        dst: Path to write (ski_nature_merged.gpkg).
        result: The merge result. Problems found are added to result.failures.

    Returns:
        Row counts per layer: {layer: {"before", "replaced", "merged_features", "after"}}.
    """
    tmp = tmp_path(dst)
    tmp.unlink(missing_ok=True)
    # Copy every layer keeping its fids. Forked runs are MultiLineStrings, so the lines
    # layer is created with the generic geometry type and holds both line types.
    with connect_ro(src) as con:
        layers = [r[0] for r in con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type = 'features' ORDER BY rowid")]
    for i, layer in enumerate(layers):
        run(["ogr2ogr", "-f", "GPKG", *(["-update"] if i else []), tmp, src, layer, "-nln", layer,
             "-preserve_fid", "-gt", "65536", *(["-nlt", "GEOMETRY"] if layer == "lines" else [])])
    con = sqlite3.connect(tmp)
    try:
        before = {layer: con.execute(f'SELECT COUNT(*) FROM "{layer}"').fetchone()[0] for layer in MERGE_LAYERS}
        # The fids to delete go through a temporary table, since there can be more of
        # them than SQLite allows as query parameters.
        con.execute("CREATE TEMP TABLE drop_fids (fid INTEGER PRIMARY KEY)")
        for layer in MERGE_LAYERS:
            for col, typ in PROVENANCE_COLUMNS:
                con.execute(f'ALTER TABLE "{layer}" ADD COLUMN "{col}" {typ}')
            con.execute("DELETE FROM drop_fids")
            con.executemany("INSERT INTO drop_fids VALUES (?)", [(f,) for f in sorted(set(result.drops.get(layer, [])))])
            con.execute(f'DELETE FROM "{layer}" WHERE fid IN (SELECT fid FROM drop_fids)')
        con.commit()
        columns = {layer: [r[1] for r in con.execute(f'PRAGMA table_info("{layer}")') if r[1] not in ("fid", "geom")]
                   for layer in MERGE_LAYERS}
    finally:
        con.close()

    for layer in MERGE_LAYERS:
        groups = [g for g in result.groups if g.layer == layer]
        if groups:
            log.info("appending %d merged features to %s", len(groups), layer)
            pyogrio.write_dataframe(output_frame(groups, layer, columns[layer]), tmp, layer=layer,
                                    append=True, promote_to_multi=False)

    # Read back where the merged rows landed, and check the counts.
    by_id = {g.group_id: g for g in result.groups}
    counts = {}
    with connect_ro(tmp) as con:
        for layer in MERGE_LAYERS:
            for fid, gid in con.execute(f'SELECT fid, merge_group FROM "{layer}" WHERE merge_group IS NOT NULL'):
                if by_id[gid].fid_after is not None:
                    result.failures.append(f"{layer}: merge_group {gid} written twice")
                by_id[gid].fid_after = fid
            after = con.execute(f'SELECT COUNT(*) FROM "{layer}"').fetchone()[0]
            dropped = len(set(result.drops.get(layer, [])))
            added = sum(1 for g in result.groups if g.layer == layer)
            counts[layer] = {"before": before[layer], "replaced": dropped, "merged_features": added, "after": after}
            if after != before[layer] - dropped + added:
                result.failures.append(f"{layer}: {after} rows written, expected {before[layer] - dropped + added}")
    missing = [g.group_id for g in result.groups if g.fid_after is None]
    if missing:
        result.failures.append(f"{len(missing)} merged features missing from the output, e.g. {missing[:3]}")
    if result.failures:
        return counts
    finish(tmp, dst)
    return counts


# ---------------------------------------------------------------- review files

def osm_link(osm_type: str, osm_id: int) -> str:
    """Return the openstreetmap.org page of an OSM object.

    Args:
        osm_type: "node", "way" or "relation".
        osm_id: The object's OSM id.

    Returns:
        A URL such as https://www.openstreetmap.org/way/123.
    """
    return f"https://www.openstreetmap.org/{osm_type}/{osm_id}"


def map_link(geom) -> str:
    """Return an openstreetmap.org map link that shows a geometry.

    The map has a marker on a point inside the geometry, and is zoomed (between levels 4
    and 18) so the whole geometry fits.

    Args:
        geom: A shapely geometry in EPSG:4326, or None.

    Returns:
        The URL, or "" if there is no geometry.
    """
    if geom is None or geom.is_empty:
        return ""
    p = shapely.point_on_surface(geom)
    x0, y0, x1, y1 = geom.bounds
    span = max(x1 - x0, y1 - y0, 1e-5)
    zoom = int(min(18, max(4, math.floor(math.log2(1400 / span)))))
    return f"https://www.openstreetmap.org/?mlat={p.y:.6f}&mlon={p.x:.6f}#map={zoom}/{p.y:.6f}/{p.x:.6f}"


def group_rows(groups: list[Group]) -> list[dict]:
    """Build the rows of merge_groups.csv, one per merged feature.

    Args:
        groups: The merged features.

    Returns:
        One row per group, column name to value: its ids and links, its names and
        difficulty before and after merging, its tag conflicts, and its length (lines) or
        area (polygons) before and after.
    """
    out = []
    for g in groups:
        is_area = g.layer == "multipolygons"
        diffs = list(dict.fromkeys(p.tags["piste:difficulty"] for p in g.pieces if p.tags.get("piste:difficulty")))
        x0, y0, x1, y1 = g.geom.bounds
        out.append({
            "group_id": g.group_id, "layer": g.layer, "fid_after": g.fid_after, "kind": g.kind,
            "name_after": g.tags.get("name") or g.tags.get("piste:name") or "",
            "pieces": len(g.pieces),
            "osm_ids": "; ".join(f"{p.osm_type}/{p.osm_id}" for p in g.pieces),
            "osm_links": " ".join(osm_link(p.osm_type, p.osm_id) for p in g.pieces),
            "map_link": map_link(g.geom),
            "bbox": f"{x0:.6f},{y0:.6f},{x1:.6f},{y1:.6f}",
            "name_match": g.match,
            "names_before": " | ".join(dict.fromkeys(p.name for p in g.pieces if p.name)),
            "difficulty_before": "; ".join(diffs),
            "difficulty_after": g.tags.get("piste:difficulty", "") if diffs else "",
            "conflicts": "; ".join(f"{k}: {' | '.join(v['values'])} -> {v['chosen']}" for k, v in g.conflicts.items()),
            "geometry_after": g.geom.geom_type,
            "length_before_m": "" if is_area else round(g.size_before, 1),
            "length_after_m": "" if is_area else round(g.size_after, 1),
            "area_before_m2": round(g.size_before) if is_area else "",
            "area_after_m2": round(g.size_after) if is_area else "",
            "max_gap_m": round(g.max_gap_m, 1),
        })
    return out


def piece_rows(groups: list[Group]) -> list[dict]:
    """Build the rows of merge_pieces.csv, one per source object.

    Args:
        groups: The merged features.

    Returns:
        One row per piece of each group, column name to value: its ids and link, its role
        in the group, its gap to the nearest piece it links to, its size, and a few key
        tags as they were before merging.
    """
    out = []
    for g in groups:
        for p in g.pieces:
            t = p.tags
            out.append({
                "group_id": g.group_id, "osm_type": p.osm_type, "osm_id": p.osm_id,
                "osm_link": osm_link(p.osm_type, p.osm_id), "layer_before": p.layer, "fid_before": p.fid,
                "name_before": p.name or "", "kind_before": p.kind, "role": p.role,
                "inherited_from": f"relation/{p.inherited_from}" if p.inherited_from else "",
                "gap_m": "" if p.gap_m is None else round(float(p.gap_m), 1),
                "size": round(p.size, 1), "size_unit": "m2" if g.layer == "multipolygons" else "m",
                **{k.replace(":", "_"): t.get(k, "") for k in
                   ("piste:type", "piste:difficulty", "piste:grooming", "aerialway", "waterway", "landuse", "natural")},
            })
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write rows to a CSV file.

    The rows go to a temporary file first, so a crash never leaves a partial file.

    Args:
        path: Where to write.
        rows: The rows. The first row's keys become the header. With no rows the file is
            empty.
    """
    tmp = tmp_path(path)
    with tmp.open("w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    finish(tmp, path)


# Template for merge_review.html. write_review replaces __DATA__ with the review data as
# JSON ({"meta": text, "groups": rows, "near": rows}). The page switches between merged
# features and near misses, filters them, and links each object to openstreetmap.org.
REVIEW_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merge review</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--accent:#0a66c2;--chip:#f2f2f7}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f7;--muted:#a1a1a6;--line:#3a3a3c;--accent:#64a8ff;--chip:#2c2c2e}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{padding:16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg)}
h1{font-size:18px;margin:0 0 8px} .meta{color:var(--muted);font-size:12px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
input{flex:1;min-width:200px}
main{padding:0 16px 32px;overflow-x:auto}
table{border-collapse:collapse;width:100%;margin-top:8px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:var(--bg);font-size:12px;color:var(--muted)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.chip{display:inline-block;padding:1px 6px;border-radius:10px;background:var(--chip);font-size:12px}
.links a{margin-right:6px;white-space:nowrap}
</style></head><body>
<header><h1>Merge review</h1><div class="meta" id="meta"></div>
<div class="controls"><select id="table"><option value="groups">Merged features</option>
<option value="near">Near misses (not merged)</option></select>
<select id="kind"><option value="">All kinds</option></select>
<select id="match"><option value="">Any</option></select>
<input id="q" type="search" placeholder="Filter by name, OSM id, kind, conflict..."></div>
<div class="meta" id="count"></div></header>
<main><table><thead id="head"></thead><tbody id="body"></tbody></table></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);const LIMIT=400;
const $=id=>document.getElementById(id);
$('meta').textContent=D.meta;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const link=(u,t)=>u?`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`:'';
const cols={groups:[['Kind',r=>`<span class="chip">${esc(r.kind)}</span>`],['Name after',r=>esc(r.name_after)],
 ['Names before',r=>esc(r.names_before)],['Match',r=>esc(r.name_match)],['Pieces',r=>r.pieces],
 ['OSM objects',r=>'<span class="links">'+r.osm_ids.split('; ').map(x=>link('https://www.openstreetmap.org/'+x,x)).join('')+'</span>'],
 ['Map',r=>link(r.map_link,'map')],['Difficulty',r=>r.difficulty_before?esc(r.difficulty_before+' → '+r.difficulty_after):''],
 ['Conflicts',r=>esc(r.conflicts)],['Geometry',r=>esc(r.geometry_after)],['Gap m',r=>r.max_gap_m],['Row after',r=>esc(r.layer+' fid '+r.fid_after)]],
 near:[['Reason',r=>`<span class="chip">${esc(r.reason)}</span>`],['A',r=>link(r.osm_link_a,r.osm_type_a+'/'+r.osm_id_a)+' '+esc(r.name_a)],
 ['Kind A',r=>esc(r.kind_a)],['B',r=>link(r.osm_link_b,r.osm_type_b+'/'+r.osm_id_b)+' '+esc(r.name_b)],['Kind B',r=>esc(r.kind_b)],
 ['Distance m',r=>r.distance_m],['Map',r=>link(r.map_link,'map')]]};
const kindOf={groups:r=>r.kind,near:r=>r.reason};const matchOf={groups:r=>r.name_match,near:r=>r.layer};
function options(){const t=$('table').value;for(const [sel,fn,label] of [['kind',kindOf[t],t==='groups'?'All kinds':'All reasons'],['match',matchOf[t],t==='groups'?'Any match':'Any layer']]){
 const vals=[...new Set(D[t].map(fn))].sort();$(sel).innerHTML=`<option value="">${label}</option>`+vals.map(v=>`<option>${esc(v)}</option>`).join('');}}
function render(){const t=$('table').value,k=$('kind').value,m=$('match').value,q=$('q').value.toLowerCase();
 const rows=D[t].filter(r=>(!k||kindOf[t](r)===k)&&(!m||matchOf[t](r)===m)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
 $('head').innerHTML='<tr>'+cols[t].map(c=>`<th>${c[0]}</th>`).join('')+'</tr>';
 $('body').innerHTML=rows.slice(0,LIMIT).map(r=>'<tr>'+cols[t].map(c=>`<td>${c[1](r)}</td>`).join('')+'</tr>').join('');
 $('count').textContent=`${rows.length.toLocaleString()} rows`+(rows.length>LIMIT?` (showing the first ${LIMIT}; filter to narrow down)`:'');}
$('table').onchange=()=>{options();render()};for(const id of ['kind','match','q'])$(id).oninput=render;options();render();
</script></body></html>
"""


def write_review(out: Path, result: Result, snapshot: str | None) -> dict:
    """Write the review files: three CSV files and merge_review.html.

    The CSV files are merge_groups.csv (one row per merged feature), merge_pieces.csv (one
    row per source object) and merge_near_misses.csv (touching pairs that weren't merged,
    and merged pieces drawn side by side).
    The HTML page shows the groups and near misses with filters.

    Args:
        out: The output directory.
        result: The merge result.
        snapshot: Timestamp of the OSM data, shown on the review page, or None if unknown.

    Returns:
        The number of rows in each CSV file, by file name, for the manifest.
    """
    groups, pieces = group_rows(result.groups), piece_rows(result.groups)
    write_csv(out / "merge_groups.csv", groups)
    write_csv(out / "merge_pieces.csv", pieces)
    write_csv(out / "merge_near_misses.csv", result.near)
    keep = ["group_id", "layer", "fid_after", "kind", "name_after", "names_before", "name_match", "pieces",
            "osm_ids", "map_link", "difficulty_before", "difficulty_after", "conflicts", "geometry_after", "max_gap_m"]
    data = {"meta": f"OSM snapshot {snapshot or 'unknown'}. {len(groups):,} merged features from "
                    f"{len(pieces):,} pieces; {len(result.near):,} near misses. Links open the objects on "
                    "openstreetmap.org (use the History tab if they changed after the snapshot).",
            "groups": [{k: r[k] for k in keep} for r in groups], "near": result.near}
    tmp = tmp_path(out / "merge_review.html")
    # "</" is escaped so that no value can close the <script> tag that holds the data.
    tmp.write_text(REVIEW_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")),
                   encoding="utf-8")
    finish(tmp, out / "merge_review.html")
    return {"merge_groups.csv": len(groups), "merge_pieces.csv": len(pieces), "merge_near_misses.csv": len(result.near)}


# ---------------------------------------------------------------- summary

def summary(result: Result) -> dict:
    """Count merged features by layer and kind, and near misses by reason.

    Args:
        result: The merge result.

    Returns:
        A dict with two entries. "by_kind" maps "<layer> <kind>" to counts of groups,
        pieces, fuzzy and inherited name matches, groups with tag conflicts and absorbed
        relations, sorted by most pieces first. "near_misses" maps each reason to its
        count.
    """
    by_kind: dict[str, dict] = {}
    for g in result.groups:
        s = by_kind.setdefault(f"{g.layer} {g.kind}", {"groups": 0, "pieces": 0, "fuzzy": 0, "inherited": 0,
                                                        "with_conflicts": 0, "relations_absorbed": 0})
        s["groups"] += 1
        s["pieces"] += len(g.pieces)
        s["fuzzy"] += g.match == "fuzzy"
        s["inherited"] += g.match == "inherited"
        s["with_conflicts"] += bool(g.conflicts)
        s["relations_absorbed"] += sum(p.role == "relation absorbed" for p in g.pieces)
    near: dict[str, int] = {}
    for r in result.near:
        near[r["reason"]] = near.get(r["reason"], 0) + 1
    return {"by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1]["pieces"])), "near_misses": near}


def print_report(result: Result) -> None:
    """Print the summary table, near-miss counts and self-check failures.

    Args:
        result: The merge result.
    """
    s = summary(result)
    print(f"\n{'layer / kind':<44} {'groups':>8} {'pieces':>9} {'fuzzy':>7} {'inherit':>8} {'conflict':>9} {'rels':>6}")
    for kind, c in s["by_kind"].items():
        print(f"{kind[:44]:<44} {c['groups']:>8,} {c['pieces']:>9,} {c['fuzzy']:>7,} {c['inherited']:>8,} "
              f"{c['with_conflicts']:>9,} {c['relations_absorbed']:>6,}")
    print(f"\ntotal: {len(result.groups):,} merged features from {sum(len(g.pieces) for g in result.groups):,} pieces")
    near = dict(s["near_misses"])
    twice = near.pop(DRAWN_TWICE, 0)
    print("near misses (touching, not merged):", ", ".join(f"{k} {v:,}" for k, v in near.items()))
    print(f"merged pieces drawn side by side, listed as {DRAWN_TWICE!r} for review: {twice:,}")
    for f in result.failures:
        print("SELF-CHECK FAILED:", f)


# ---------------------------------------------------------------- main

def main() -> None:
    """Run the merge step from the command line.

    The options are described in the module docstring, which is also the --help text.
    Does nothing if ski_nature_merged.gpkg is newer than its inputs and the rules haven't
    changed, unless --force is given. With --report, prints what would be merged and
    writes nothing. Exits with an error if an input is missing or a self-check fails.
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help=f"where data is stored (default: {DEFAULT_DATA_DIR})")
    ap.add_argument("--force", action="store_true", help="rebuild even if the output is up to date")
    ap.add_argument("--report", action="store_true", help="print what would be merged; write nothing")
    args = ap.parse_args()

    setup_logging()
    out = args.data_dir.resolve() / "out"
    src, pbf = out / "ski_nature.gpkg", out / "near_runs.osm.pbf"
    dst, manifest_path = out / "ski_nature_merged.gpkg", out / "ski_nature_merged.json"
    review = [out / n for n in ("merge_groups.csv", "merge_pieces.csv", "merge_near_misses.csv", "merge_review.html")]
    for f, step in ((src, "clean_features.py"), (pbf, "filter_near_runs.py")):
        if not f.exists():
            sys.exit(f"Missing {f}. Run osm_data/{step} first.")
    rules_hash = hashlib.md5(json.dumps(rules(), sort_keys=True).encode()).hexdigest()

    if not args.report:
        tmp_path(dst).unlink(missing_ok=True)   # leftover from an interrupted run
        previous = read_json(manifest_path) or {}
        newest_input = max(src.stat().st_mtime, pbf.stat().st_mtime)
        if (not args.force and dst.exists() and dst.stat().st_mtime > newest_input
                and previous.get("rules_hash") == rules_hash and all(f.exists() for f in review)):
            log.info("%s is up to date (use --force to rebuild)", dst.name)
            return

    started = now_iso()
    result = Result()
    merge_line_features(src, pbf, result)
    merge_area_features(src, result)
    for layer, fids in result.drops.items():
        if len(fids) != len(set(fids)):
            result.failures.append(f"{layer}: some rows were put in two merged features")

    if args.report:
        print_report(result)
        return
    if result.failures:
        print_report(result)
        sys.exit("Self-checks failed; nothing written.")

    log.info("== writing %s", dst.name)
    require_tools("ogr2ogr")
    counts = write_output(src, dst, result)
    if result.failures:
        print_report(result)
        sys.exit(f"Self-checks failed; {dst.name} not written (the temporary copy is {tmp_path(dst)}).")
    snapshot = (read_json(out / "ski_nature.json") or {}).get("input_snapshot")
    review_counts = write_review(out, result, snapshot)

    write_json(manifest_path, {
        "input": str(src),
        "input_modified": datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "relations_from": str(pbf),
        "input_snapshot": snapshot,
        "rules_hash": rules_hash,
        "rules": {k: v for k, v in rules().items() if k != "script_md5"},
        "layers": counts,
        "matching": result.counts,
        "summary": summary(result),
        "review_files": review_counts,
        "started": started,
        "finished": now_iso(),
    })
    print_report(result)
    log.info("done: %s (%s); review files: %s", dst.name, human_size(dst.stat().st_size),
             ", ".join(f.name for f in review))


if __name__ == "__main__":
    main()
