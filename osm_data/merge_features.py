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
    merge_near_misses.csv     touching pairs that were not merged, and why
    merge_review.html         the groups and near misses as a filterable page

Rules (details in osm_data/README.md):
    name       names match after normalizing (accents, case, punctuation, spacing,
               possessives, u./l. and other abbreviations). Small spelling differences are
               allowed (one edit from 8 letters, two from 16) only if the qualifier words
               match (upper/lower, east/west, numbers, single letters, ...).
    location   lines: an end of one is within 15 m of the other. Lifts must meet end to
               end, and side-by-side twin lifts are kept apart. Polygons: within 5 m.
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

Usage:
    python osm_data/merge_features.py --data-dir "/path/to/osm_data/data"
    python osm_data/merge_features.py --data-dir "/path/to/osm_data/data" --report
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
import shapely
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

METRIC_CRS = EQUAL_AREA_CRS     # distances and lengths; geometry itself stays in EPSG:4326
LINE_GAP_M = 15.0               # an end of one line within this of the other line
POLY_GAP_M = 5.0                # polygons within this of each other (or overlapping)
SHARED_NODE_M = 0.05            # closer than this counts as the same OSM node: no connector
COPY_WITHIN_M = 3.0             # a line is a copy of another if this share of it lies
COPY_SHARE = 0.9                # within this distance of the other
TWIN_LENGTH_RATIO = 0.8         # lifts meeting at both ends with lengths this close are twins
LENGTH_TOLERANCE = 0.001        # self-check: merged length vs. pieces + connectors

# Words that tell two runs (or streams) with otherwise the same name apart.
QUALIFIER_WORDS = frozenset("""
    upper lower middle top bottom lowest uppermost lowermost
    north south east west northeast northwest southeast southwest
    haut haute bas basse superieur superieure inferieur inferieure est ouest nord sud
    left right gauche droite inner outer
""".split())
ROMAN_NUMERALS = frozenset("i ii iii iv v vi vii viii ix x".split())
LEADING_ARTICLES = frozenset({"the", "la", "le", "les"})
ABBREVIATIONS = {
    "upr": "upper", "lwr": "lower", "mid": "middle",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
    "mt": "mount", "mtn": "mountain", "rd": "road", "trl": "trail", "tr": "trail",
    "ck": "creek", "crk": "creek",
}

DIFFICULTY_ORDER = ["novice", "easy", "intermediate", "advanced", "expert", "freeride", "extreme"]
DIFFICULTY_RANK = {d: i for i, d in enumerate(DIFFICULTY_ORDER)}
DIFFICULTY_ALIASES = {"beginner": "novice", "difficult": "advanced", "hard": "advanced"}
MULTI_VALUE_KEYS = {"piste:type", "piste:grooming", "ref", "piste:ref", "piste:number", "alt_name"}
PLUS_SEPARATED_KEYS = {"piste:grooming"}        # classic+skating means classic;skating
YES_IF_ANY_KEYS = {"gladed", "lit", "piste:lit", "piste:snowmaking", "patrolled"}
MOST_COVERAGE_KEYS = {"name", "waterway", "highway"}   # value covering the most length wins
RELATION_ONLY_KEYS = {"type", "route"}

NON_SKI = frozenset(NON_SKI_PISTE_TYPES)
TRAIL_HIGHWAYS = frozenset(SKI_TRAIL_HIGHWAYS)
# One watercourse: a creek can become a river, and a flowline is the river's path through a lake.
STREAM_RIVER = frozenset({"stream", "river", "flowline"})
GRASS_LANDUSE = frozenset({"grass", "meadow"})
LINE_KIND_KEYS = ["man_made", "natural", "highway", "railway", "barrier", "power"]
AREA_KIND_KEYS = ["landuse", "natural", "landcover", "leisure", "man_made", "tourism", "amenity",
                  "building", "sport", "wetland"]
# Relations in these layers with these types can share a name with their ways.
RELATION_TYPES = {"multilinestrings": None,   # every row
                  "other_relations": ("waterway", "route", "piste", "multilinestring")}

MERGE_LAYERS = ["lines", "multilinestrings", "multipolygons", "other_relations"]
NOT_TAG_COLUMNS = {"fid", "geom", "geometry", "osm_id", "osm_way_id", "other_tags", "z_order"}
PROVENANCE_COLUMNS = [("merge_group", "TEXT"), ("merged_ids", "TEXT"), ("merged_count", "INTEGER"),
                      ("merge_match", "TEXT"), ("merge_conflicts", "TEXT")]
NEAR_REASONS = {1: "qualifier differs", 2: "name too different"}


def rules() -> dict:
    """Everything that changes the result, for the up-to-date check."""
    return {
        "metric_crs": METRIC_CRS, "line_gap_m": LINE_GAP_M, "poly_gap_m": POLY_GAP_M,
        "shared_node_m": SHARED_NODE_M, "copy_within_m": COPY_WITHIN_M, "copy_share": COPY_SHARE,
        "twin_length_ratio": TWIN_LENGTH_RATIO, "qualifiers": sorted(QUALIFIER_WORDS),
        "abbreviations": ABBREVIATIONS, "difficulty_order": DIFFICULTY_ORDER,
        "script_md5": hashlib.md5(Path(__file__).read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------- names

@dataclass(frozen=True)
class NameKey:
    raw: str              # the name as written (trimmed)
    compact: str          # normalized tokens joined without spaces
    base: str             # compact without the qualifier tokens
    quals: frozenset      # qualifier tokens (upper, east, 2, b, ...)


def is_qualifier(token: str) -> bool:
    return (token in QUALIFIER_WORDS or token in ROMAN_NUMERALS or len(token) == 1
            or any(c.isdigit() for c in token))


@lru_cache(maxsize=None)
def name_key(raw: str | None) -> NameKey | None:
    """Normalize a name for matching: "L. Cruiser" -> lower + cruiser, "Russ's" -> russ."""
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
    return 2 if length >= 16 else 1 if length >= 8 else 0


def osa_distance(a: str, b: str, limit: int) -> int:
    """Edit distance with adjacent swaps (optimal string alignment), capped at limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return min(prev[-1], limit + 1)


def compare_names(a: NameKey, b: NameKey) -> tuple[str | None, str | None]:
    """("exact" | "fuzzy" | None, near-miss reason | None)."""
    if a.raw == b.raw:
        return "exact", None
    if a.compact == b.compact:          # Bear Claw / Bearclaw, Cascapédia / Cascapedia
        return "fuzzy", None
    allowed = allowed_edits(min(len(a.compact), len(b.compact)))
    if a.quals != b.quals:              # Upper / Lower White Lightning
        if a.base and b.base and osa_distance(a.base, b.base, allowed) <= allowed:
            return None, "qualifier differs"
        return None, None
    dist = osa_distance(a.compact, b.compact, allowed + 1)
    if dist <= allowed:                 # Winnisook / Winisook
        return "fuzzy", None
    if dist == allowed + 1:             # The Vain / The Main
        return None, "name too different"
    return None, None


# ---------------------------------------------------------------- kinds

@dataclass(frozen=True)
class Kind:
    family: str                       # features merge only within a family
    label: str                        # for the review files: piste:downhill, natural:wood, ...
    pistes: frozenset | None = None   # piste:type values (piste kinds only)


def piste_types(value) -> frozenset:
    return frozenset(v.strip().lower() for v in str(value).split(";") if v.strip())


def piste_compatible(a: frozenset, b: frozenset) -> bool:
    """Ski pistes need a ski type in common; other pistes need the same types."""
    ski_a, ski_b = a - NON_SKI, b - NON_SKI
    if ski_a or ski_b:
        return bool(ski_a & ski_b)
    return a == b


def kinds_compatible(a: Kind, b: Kind) -> bool:
    if a.family != b.family:
        return False
    if not a.pistes or not b.pistes:     # not a piste, or a relation without piste:type
        return True
    return piste_compatible(a.pistes, b.pistes)


def line_kind(tags: dict) -> Kind:
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
    """Can this way be a piece of the same feature as this relation?"""
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
    """Lifts join only end to end; a lift touching another mid-way is a different lift."""
    return kind.family.startswith("aerialway:")


# ---------------------------------------------------------------- tags

def present(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and v is not pd.NA


@lru_cache(maxsize=None)
def layer_columns(layer: str) -> dict[str, str]:
    """Column -> OSM key for the tags osmconf.ini makes real columns (piste_type -> piste:type)."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(OSMCONF)
    return {k.replace(":", "_"): k for k in cp[layer]["attributes"].split(",")}


def parse_other_tags(text) -> dict:
    """other_tags is written as a Python-style dict ({'sport': 'skiing'}), not strict JSON."""
    if not present(text) or not str(text).strip():
        return {}
    try:
        tags = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        tags = json.loads(text)
    return {str(k): str(v) for k, v in tags.items()}


def format_other_tags(tags: dict) -> str | None:
    return repr(dict(sorted(tags.items()))) if tags else None


def row_tags(row: dict, layer: str) -> dict:
    cols = layer_columns(layer)
    tags = {key: str(row[col]) for col, key in cols.items() if col in row and present(row[col])}
    tags.update(parse_other_tags(row.get("other_tags")))
    return tags


def normalize_difficulty(value: str) -> str | None:
    s = value.strip().lower()
    s = DIFFICULTY_ALIASES.get(s, s)
    if s in DIFFICULTY_RANK:
        return s
    word = re.match(r"[a-z]*", s).group(0)       # "easy (green circle)" -> easy
    word = DIFFICULTY_ALIASES.get(word, word)
    return word if word in DIFFICULTY_RANK else None


def split_values(key: str, value: str) -> list[str]:
    sep = r"[;+]" if key in PLUS_SEPARATED_KEYS else r";"
    return [p.strip() for p in re.split(sep, value) if p.strip()]


def resolve(key: str, values: list[tuple[str, float]]) -> tuple[str, bool]:
    """Pick one value for `key` from (value, weight) pairs. Returns (value, is_conflict)."""
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
    """Combine the tags of (tags, weight) pieces. Returns (tags, conflicts).

    conflicts: {key: {"values": [...], "chosen": value}} for every key whose pieces disagree.
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
    reason: str
    a: int
    b: int
    distance: float


@dataclass
class Links:
    a: np.ndarray                     # edges between node indices
    b: np.ndarray
    dist: np.ndarray                  # metres
    copy_of: dict = field(default_factory=dict)       # node -> node it duplicates
    connectors: list = field(default_factory=list)    # (node, LineString in EPSG:4326)
    near: list = field(default_factory=list)          # NearMiss


def intern(values) -> tuple[np.ndarray, list]:
    ids: dict = {}
    out = np.empty(len(values), np.int64)
    for i, v in enumerate(values):
        out[i] = ids.setdefault(v, len(ids))
    return out, list(ids)


def compare_pairs(keys: list, kinds: list, o: np.ndarray, t: np.ndarray):
    """Name match code (0 none, 1 exact, 2 fuzzy), near-miss code and kind compatibility per pair.

    Each distinct pair of names or kinds is compared once.
    """
    kid, uniq_keys = intern(keys)
    tid, uniq_kinds = intern(kinds)

    def per_unique(ids, uniq, fn):
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
    if rows.empty:
        return []
    rows = rows.assign(a=np.minimum(rows.o, rows.t), b=np.maximum(rows.o, rows.t))
    best = rows.sort_values("d").drop_duplicates(["a", "b"])
    return [NearMiss(r, int(a), int(b), float(d)) for r, a, b, d in
            zip(best[reason_col], best.a, best.b, best.d)]


def find_line_links(geom: np.ndarray, geom_m: np.ndarray, kinds: list, keys: list,
                    lengths: np.ndarray, collect_near: bool = False) -> Links:
    """Links between named lines: an end of one within LINE_GAP_M of the other, same kind, names match."""
    named = np.flatnonzero(np.array([k is not None for k in keys], bool))
    if len(named) < 2:
        return Links(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))
    g = geom_m[named]
    m = len(named)
    starts, ends = shapely.get_point(g, 0), shapely.get_point(g, -1)
    pts = np.concatenate([starts, ends])
    pi, ti = shapely.STRtree(g).query(pts, predicate="dwithin", distance=LINE_GAP_M)
    oi, end = pi % m, pi // m
    keep = oi != ti
    pi, ti, oi, end = pi[keep], ti[keep], oi[keep], end[keep]
    o, t = named[oi], named[ti]
    mcode, ncode, kind_ok = compare_pairs(keys, kinds, o, t)

    link = (mcode > 0) & kind_ok
    near = ((mcode > 0) & ~kind_ok) | (kind_ok & (ncode > 0)) if collect_near else np.zeros(len(o), bool)
    sel = np.flatnonzero(link | near)
    p, tt = pts[pi[sel]], ti[sel]
    d = shapely.distance(p, g[tt])
    ds, de = shapely.distance(p, starts[tt]), shapely.distance(p, ends[tt])
    rows = pd.DataFrame({"o": o[sel], "t": t[sel], "end": end[sel], "d": d,
                         "dend": np.minimum(ds, de), "tend": np.where(ds <= de, 0, 1),
                         "link": link[sel], "mcode": mcode[sel], "ncode": ncode[sel],
                         "kind_ok": kind_ok[sel]})
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

    # Copies: both ends of the shorter line close to the other, and most of it alongside.
    close = L[L.d <= COPY_WITHIN_M]
    both_ends = close.groupby(["o", "t"])["end"].nunique()
    for oo, tt_ in both_ends[both_ends == 2].index:
        lo, lt = lengths[oo], lengths[tt_]
        if lo > lt or (lo == lt and oo < tt_) or oo in out.copy_of:
            continue
        inside = shapely.length(shapely.intersection(geom_m[oo], shapely.buffer(geom_m[tt_], COPY_WITHIN_M + 0.5)))
        if inside >= COPY_SHARE * max(lo, 1e-9):
            out.copy_of[int(oo)] = int(tt_)

    # Twin lifts: two lifts meeting at both ends with similar lengths are two lifts side by side.
    twins = set()
    lift_rows = L[e2e[L.o.to_numpy()]]
    for a, b in set(zip(np.minimum(lift_rows.o, lift_rows.t), np.maximum(lift_rows.o, lift_rows.t))):
        if a in out.copy_of or b in out.copy_of:
            continue
        sa, ea = shapely.get_point(geom_m[a], 0), shapely.get_point(geom_m[a], -1)
        sb, eb = shapely.get_point(geom_m[b], 0), shapely.get_point(geom_m[b], -1)
        both = min(max(sa.distance(sb), ea.distance(eb)), max(sa.distance(eb), ea.distance(sb)))
        if both <= LINE_GAP_M and min(lengths[a], lengths[b]) >= TWIN_LENGTH_RATIO * max(lengths[a], lengths[b]):
            twins.add((int(a), int(b)))
            if collect_near:
                out.near.append(NearMiss("twin lifts", int(a), int(b), float(both)))
    if twins:
        pair = list(zip(np.minimum(L.o, L.t), np.maximum(L.o, L.t)))
        L = L[[pq not in twins for pq in pair]]

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
                line = shapely.shortest_line(ep, geom[tt_])
            if key not in seen:
                seen.add(key)
                out.connectors.append((int(oo), line))
    return out


def find_area_links(geom_m: np.ndarray, kinds: list, keys: list, collect_near: bool = False) -> Links:
    """Links between named polygons within POLY_GAP_M of each other, same kind, names match."""
    named = np.flatnonzero(np.array([k is not None for k in keys], bool))
    if len(named) < 2:
        return Links(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))
    g = geom_m[named]
    i, j = shapely.STRtree(g).query(g, predicate="dwithin", distance=POLY_GAP_M)
    keep = i < j
    o, t = named[i[keep]], named[j[keep]]
    mcode, ncode, kind_ok = compare_pairs(keys, kinds, o, t)
    link = (mcode > 0) & kind_ok
    near = ((mcode > 0) & ~kind_ok) | (kind_ok & (ncode > 0)) if collect_near else np.zeros(len(o), bool)
    sel = np.flatnonzero(link | near)
    rows = pd.DataFrame({"o": o[sel], "t": t[sel], "d": shapely.distance(geom_m[o[sel]], geom_m[t[sel]]),
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
    graph = coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n))
    return connected_components(graph, directed=False)[1]


def to_metric(geoms) -> np.ndarray:
    return np.asarray(gpd.GeoSeries(list(geoms), crs=4326).to_crs(METRIC_CRS).array)


# ---------------------------------------------------------------- merged features

@dataclass
class Piece:
    osm_type: str           # way / relation
    osm_id: int
    layer: str
    fid: int
    name: str | None        # its own name (name, or piste:name)
    kind: str
    tags: dict
    size: float             # metres (lines) or m² (polygons) this piece adds
    role: str = "merged"
    gap_m: float | None = None
    inherited_from: int | None = None


@dataclass
class Group:
    layer: str
    kind: str
    pieces: list
    geom: object            # EPSG:4326
    rep: Piece              # the piece whose id the merged feature keeps
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
        ids = sorted(f"{p.osm_type[0]}{p.osm_id}" for p in self.pieces)
        return f"{self.layer}-{hashlib.sha1(';'.join(ids).encode()).hexdigest()[:10]}"

    @property
    def merged_ids(self) -> str:
        rest = sorted((p for p in self.pieces if p is not self.rep), key=lambda p: (p.osm_type, p.osm_id))
        return ";".join(f"{p.osm_type[0]}{p.osm_id}" for p in [self.rep, *rest])


def group_match(pieces: list[Piece]) -> str:
    names = {p.name for p in pieces if p.name}
    if len(names) > 1:
        return "fuzzy"
    if any(p.inherited_from is not None for p in pieces):
        return "inherited"
    return "exact"


def finish_group(g: Group, weights: list[float]) -> Group:
    tagged = [(p.tags, w) for p, w in zip(g.pieces, weights)]
    g.tags, g.conflicts = merge_tags(tagged)
    g.match = group_match(g.pieces)
    if g.tags.get("piste:type"):
        g.kind = f"piste:{g.tags['piste:type']}"
    return g


def line_parts(geom) -> list:
    return [p for p in shapely.get_parts(geom) if p.geom_type == "LineString" and not p.is_empty]


def merge_lines(parts: list):
    """Join line parts into as few lines as possible, keeping their direction where it can."""
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
    layer: str
    fid: int
    osm_id: int
    tags: dict
    key: NameKey | None
    kind: Kind
    geom: object
    members: list = field(default_factory=list)    # node indices of member ways in lines


def read_relations(src: Path) -> list[Relation]:
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
    """Member way ids of the wanted relations (the GeoPackage doesn't keep them)."""
    members: dict[int, list[int]] = {}

    class Handler(osmium.SimpleHandler):
        def relation(self, r):
            if r.id in wanted:
                members[r.id] = [m.ref for m in r.members if m.type == "w"]

    Handler().apply_file(str(pbf))
    return members


def fetch_rows(src: Path, layer: str, fids) -> dict[int, dict]:
    """Attribute columns (no geometry) of the given rows, by fid."""
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
    c = shapely.get_coordinates(line)
    return tuple(np.round(c[0], 7)), tuple(np.round(c[-1], 7))


# ---------------------------------------------------------------- the lines layer + relations

@dataclass
class Result:
    groups: list = field(default_factory=list)
    near: list = field(default_factory=list)      # dicts for merge_near_misses.csv
    drops: dict = field(default_factory=dict)     # layer -> fids replaced by merged rows
    counts: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)  # self-check problems


def near_row(reason: str, layer: str, a: dict, b: dict, distance: float, geom) -> dict:
    return {"reason": reason, "layer": layer,
            "kind_a": a["kind"], "osm_type_a": a["osm_type"], "osm_id_a": a["osm_id"],
            "osm_link_a": osm_link(a["osm_type"], a["osm_id"]), "name_a": a["name"],
            "kind_b": b["kind"], "osm_type_b": b["osm_type"], "osm_id_b": b["osm_id"],
            "osm_link_b": osm_link(b["osm_type"], b["osm_id"]), "name_b": b["name"],
            "distance_m": round(distance, 1), "map_link": map_link(geom)}


def merge_line_features(src: Path, pbf: Path, result: Result) -> None:
    started = time.time()
    cols = ["osm_id", "name", "piste_name", "piste_type", "aerialway", "railway", "waterway", "highway",
            "man_made", "natural", "barrier", "power"]
    df = pyogrio.read_dataframe(src, layer="lines", columns=cols, fid_as_index=True)
    n_lines = len(df)
    col2key = layer_columns("lines")
    kind_cols = [c for c in cols if c not in ("osm_id", "name", "piste_name")]
    kind_vals = df[kind_cols].to_numpy(object)

    def kind_tags(i: int) -> dict:
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
    inherited = {i: rs[0] for i, rs in claims.items() if len(set(id(r) for r in rs)) == 1}
    keys = list(own_keys)
    for i, r in inherited.items():
        keys[i] = r.key

    # R2: a relation whose member ways all carry its name is the same feature as those ways.
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
        pieces = [(kind_tags(i), float(shapely.length(geom[i]))) for i in r.members] + [(rel_tags, 0.0)]
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
    named = np.flatnonzero(np.array([k is not None for k in all_keys], bool))
    geom_m = np.empty(n, dtype=object)
    geom_m[named] = to_metric(all_geom[named])
    lengths = np.zeros(n)
    lengths[named] = shapely.length(geom_m[named])
    log.info("inherited names: %d ways; relations absorbed: %d (%d extra parts); metric transform done (%.0f s)",
             len(inherited), len(absorbed), len(v_geom), time.time() - started)

    links = find_line_links(all_geom, geom_m, all_kinds, all_keys, lengths, collect_near=True)
    a, b = [links.a], [links.b]
    rel_nodes: dict[int, list[int]] = {}      # id(relation) -> its nodes
    for k, r in enumerate(v_rel):
        rel_nodes.setdefault(id(r), []).append(n_lines + k)
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
    grouped = np.flatnonzero((sizes[labels] > 1) | np.isin(labels, list(rel_by_label)))
    by_label: dict[int, list[int]] = {}
    for i in grouped:
        by_label.setdefault(int(labels[i]), []).append(int(i))
    connectors_by_label: dict[int, list] = {}
    for node, line in links.connectors:
        connectors_by_label.setdefault(int(labels[node]), []).append(line)
    gap = {}
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
        g.expected = float(sum(p.size for p in pieces if p.role != "copy dropped"))   # type: ignore[attr-defined]
        g.connectors = conns                                                          # type: ignore[attr-defined]
        groups.append(finish_group(g, weights))

    # Lengths after merging, and the length self-check.
    if groups:
        after = shapely.length(to_metric([g.geom for g in groups]))
        conn_all = [c for g in groups for c in g.connectors]
        conn_len = shapely.length(to_metric(conn_all)) if conn_all else np.empty(0)
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
    # from the parts of one forked feature.
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
    again = find_line_links(f_geom, f_m, f_kinds, f_keys, shapely.length(f_m))
    owner = np.array(f_owner)
    clash = [(x, y) for x, y in zip(again.a, again.b) if owner[x] != owner[y]]

    def describe(x: int) -> str:
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
    return next(i for i in ways if osm_ids[i] == rep.osm_id)


def merge_relation_features(relations: list[Relation], absorbed: list[Relation], result: Result) -> None:
    """R3: route relations left over that share a name and touch become one relation."""
    taken = {id(r) for r in absorbed}
    for layer in RELATION_TYPES:
        rels = [r for r in relations if r.layer == layer and id(r) not in taken and r.key is not None]
        if len(rels) < 2:
            continue
        geoms = np.array([r.geom for r in rels], dtype=object)
        gm = to_metric(geoms)
        lines_only = np.array([shapely.MultiLineString(line_parts(g)) if line_parts(g) else g for g in gm], dtype=object)
        i, j = shapely.STRtree(lines_only).query(lines_only, predicate="dwithin", distance=LINE_GAP_M)
        keep = i < j
        i, j = i[keep], j[keep]
        ok = [compare_names(rels[x].key, rels[y].key)[0] is not None and kinds_compatible(rels[x].kind, rels[y].kind)
              for x, y in zip(i, j)]
        i, j = i[ok], j[ok]
        if not len(i):
            continue
        labels = component_labels(len(rels), i, j)
        lengths = shapely.length(lines_only)
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
            g.size_after = float(shapely.length(to_metric([out_geom])[0]))
            result.groups.append(finish_group(g, [p.size for p in pieces]))
            result.drops.setdefault(layer, []).extend(p.fid for p in pieces)
        log.info("%s: merged %d touching same-name relations into %d", layer,
                 len(set(np.concatenate([i, j]))), len(np.unique(labels[np.concatenate([i, j])])))


# ---------------------------------------------------------------- the multipolygons layer

def merge_area_features(src: Path, result: Result) -> None:
    started = time.time()
    df = pyogrio.read_dataframe(src, layer="multipolygons", where="name IS NOT NULL OR piste_name IS NOT NULL",
                                fid_as_index=True)
    records = df.drop(columns="geometry").to_dict("records")
    tags = [row_tags(r, "multipolygons") for r in records]
    kinds = [area_kind(t) for t in tags]
    keys = [name_key(t.get("name") or t.get("piste:name")) for t in tags]
    geom = shapely.make_valid(np.asarray(df.geometry.array))
    geom_m = shapely.make_valid(to_metric(geom))
    areas = shapely.area(geom_m)
    fids = df.index.to_numpy(np.int64)
    links = find_area_links(geom_m, kinds, keys, collect_near=True)
    labels = component_labels(len(df), links.a, links.b)
    gap = {}
    for x, y, d in zip(links.a, links.b, links.dist):
        gap[x] = min(gap.get(x, d), d)
        gap[y] = min(gap.get(y, d), d)

    def ident(r: dict) -> tuple[str, int]:
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
        for g, area in zip(groups, shapely.area(to_metric([g.geom for g in groups]))):
            g.size_after = float(area)
            biggest = max(p.size for p in g.pieces)
            if area > g.size_before * (1 + LENGTH_TOLERANCE) + 1 or area < biggest * (1 - LENGTH_TOLERANCE) - 1:
                result.failures.append(f"multipolygons group {g.group_id}: merged area {area:.0f} m², "
                                       f"pieces {g.size_before:.0f} m² (largest {biggest:.0f} m²)")
    result.groups.extend(groups)
    dropped = [p.fid for g in groups for p in g.pieces]
    result.drops.setdefault("multipolygons", []).extend(dropped)

    def info(k: int) -> dict:
        ot, oid = ident(records[k])
        return {"osm_type": ot, "osm_id": oid, "name": keys[k].raw if keys[k] else "", "kind": kinds[k].label}

    for nm in links.near:
        result.near.append(near_row(nm.reason, "multipolygons", info(nm.a), info(nm.b), nm.distance, geom[nm.a]))
    result.counts["multipolygons"] = {"named_read": len(df), "groups": len(groups), "pieces": len(dropped)}

    # Self-check: nothing links any more.
    in_group = set(dropped)
    keep = [k for k in range(len(df)) if int(fids[k]) not in in_group]
    merged_m = list(to_metric([g.geom for g in groups])) if groups else []
    f_m = np.array([geom_m[k] for k in keep] + merged_m, dtype=object)
    f_kinds = [kinds[k] for k in keep] + [area_kind(g.tags) for g in groups]
    f_keys = [keys[k] for k in keep] + [name_key(g.tags.get("name") or g.tags.get("piste:name")) for g in groups]
    again = find_area_links(shapely.make_valid(f_m), f_kinds, f_keys)
    for x, y in list(zip(again.a, again.b))[:20]:
        result.failures.append(f"polygons still link after merging: {f_keys[x].raw!r} and {f_keys[y].raw!r}")
    log.info("multipolygons: %d merged features from %d pieces (%.0f s)", len(groups), len(dropped),
             time.time() - started)


# ---------------------------------------------------------------- writing

def output_frame(groups: list[Group], layer: str, columns: list[str]) -> gpd.GeoDataFrame:
    """Merged rows with exactly the layer's columns, in the layer's order."""
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
    return f"https://www.openstreetmap.org/{osm_type}/{osm_id}"


def map_link(geom) -> str:
    if geom is None or geom.is_empty:
        return ""
    p = shapely.point_on_surface(geom)
    x0, y0, x1, y1 = geom.bounds
    span = max(x1 - x0, y1 - y0, 1e-5)
    zoom = int(min(18, max(4, math.floor(math.log2(1400 / span)))))
    return f"https://www.openstreetmap.org/?mlat={p.y:.6f}&mlon={p.x:.6f}#map={zoom}/{p.y:.6f}/{p.x:.6f}"


def group_rows(groups: list[Group]) -> list[dict]:
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
    tmp = tmp_path(path)
    with tmp.open("w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    finish(tmp, path)


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
    tmp.write_text(REVIEW_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")),
                   encoding="utf-8")
    finish(tmp, out / "merge_review.html")
    return {"merge_groups.csv": len(groups), "merge_pieces.csv": len(pieces), "merge_near_misses.csv": len(result.near)}


# ---------------------------------------------------------------- summary

def summary(result: Result) -> dict:
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
    s = summary(result)
    print(f"\n{'layer / kind':<44} {'groups':>8} {'pieces':>9} {'fuzzy':>7} {'inherit':>8} {'conflict':>9} {'rels':>6}")
    for kind, c in s["by_kind"].items():
        print(f"{kind[:44]:<44} {c['groups']:>8,} {c['pieces']:>9,} {c['fuzzy']:>7,} {c['inherited']:>8,} "
              f"{c['with_conflicts']:>9,} {c['relations_absorbed']:>6,}")
    print(f"\ntotal: {len(result.groups):,} merged features from {sum(len(g.pieces) for g in result.groups):,} pieces")
    print("near misses (touching, not merged):", ", ".join(f"{k} {v:,}" for k, v in s["near_misses"].items()))
    for f in result.failures:
        print("SELF-CHECK FAILED:", f)


# ---------------------------------------------------------------- main

def main() -> None:
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
