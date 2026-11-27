#!/usr/bin/env python3
"""Load the cleaned ski data into a local PostGIS database.

Reads <data-dir>/out/ski_nature_merged.gpkg (the output of merge_features.py, never
changed) and writes a PostgreSQL database, by default one named "skiing":

    osm schema   one table per GeoPackage layer (points, lines, multilinestrings,
                 multipolygons, other_relations) with the same fids and columns, plus
                 load_info, one row describing what was loaded. other_tags and
                 merge_conflicts are jsonb and merged_ids is a text array.
    ski schema   views over those tables: runs, run_areas, run_routes, lifts, resorts.

The load builds everything in a staging schema (osm_new), then swaps it in for osm in one
transaction, so a failed or interrupted load leaves the previous database as it was. The
swap replaces the osm schema and the views above. It deliberately doesn't cascade: if you
add your own view or foreign key that depends on them, the swap fails instead of silently
deleting it. Keep your own tables in another schema.

No features are changed, dropped or merged here, so there is no review output.

GDAL (ogr2ogr) writes a PostgreSQL dump that is piped into psql, so GDAL doesn't need its
live PostgreSQL driver and no Python database package is needed. psql connects the usual
libpq way (PGHOST, PGPORT, PGUSER, ...); the default is the local server.

Typical usage example:

    python osm_data/load_database.py --data-dir "/path/to/osm_data/data"

A load is skipped when the database already holds this input and this version of the
script. --force loads anyway.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from common import DEFAULT_DATA_DIR, connect_ro, log, read_json, setup_logging

INPUT_NAME = "ski_nature_merged.gpkg"
DEFAULT_DBNAME = "skiing"
# Where a load is built before it replaces the live "osm" schema.
STAGING = "osm_new"

# GeoPackage layer -> PostGIS geometry type. The lines layer mixes LineString and
# MultiLineString (merged lines), so it takes any geometry type.
LAYERS = {
    "points": "POINT",
    "lines": "GEOMETRY",
    "multilinestrings": "MULTILINESTRING",
    "multipolygons": "MULTIPOLYGON",
    "other_relations": "GEOMETRYCOLLECTION",
}

# Columns that get a plain index, on every table that has them.
BTREE_COLUMNS = ["osm_id", "osm_way_id", "piste_type", "aerialway", "natural", "landuse",
                 "waterway"]
# Tables that also get an index on geom::geography, for fast "within N metres" queries.
GEOGRAPHY_LAYERS = ["points", "lines", "multipolygons"]

# aerialway values that carry skiers. Stations, pylons, zip lines, goods and mining
# lifts, proposed or abandoned lifts and unclear values ("yes", "unknown") are left out
# of ski.lifts; they are still in the osm tables.
LIFT_TYPES = ["cable_car", "gondola", "mixed_lift", "chair_lift", "drag_lift", "t-bar",
              "j-bar", "platter", "rope_tow", "magic_carpet"]

# piste:type can list several uses ("downhill;sled"), so match any one of them.
IS_DOWNHILL = "'downhill' = ANY (string_to_array(replace(piste_type, ' ', ''), ';'))"

# Views over the loaded tables: name -> SELECT. Lengths and areas are measured on the
# WGS84 ellipsoid.
VIEWS = {
    # Run centerlines.
    "runs": f"""
        SELECT fid, osm_id, coalesce(name, piste_name) AS name,
               piste_difficulty AS difficulty, piste_grooming AS grooming,
               piste_oneway AS oneway, gladed,
               ST_Length(geom::geography) AS length_m,
               merged_ids, other_tags AS tags, geom
        FROM osm.lines
        WHERE {IS_DOWNHILL}""",
    # Runs mapped as areas. Kept apart from the centerlines, not linked to them.
    "run_areas": f"""
        SELECT fid, osm_id, osm_way_id, coalesce(name, piste_name) AS name,
               piste_difficulty AS difficulty, piste_grooming AS grooming, gladed,
               ST_Area(geom::geography) AS area_m2,
               merged_ids, other_tags AS tags, geom
        FROM osm.multipolygons
        WHERE {IS_DOWNHILL}""",
    # Downhill route relations that weren't merged into their ways. They can overlap
    # rows in ski.runs, so they are not part of that view.
    "run_routes": f"""
        SELECT fid, osm_id, coalesce(name, piste_name) AS name,
               piste_difficulty AS difficulty,
               merged_ids, other_tags AS tags, geom
        FROM osm.multilinestrings
        WHERE {IS_DOWNHILL}""",
    "lifts": f"""
        SELECT fid, osm_id, name, aerialway AS lift_type,
               ST_Length(geom::geography) AS length_m,
               merged_ids, other_tags AS tags, geom
        FROM osm.lines
        WHERE aerialway IN ({", ".join(f"'{t}'" for t in LIFT_TYPES)})""",
    # Ski area outlines. Not every real resort has one in OpenStreetMap.
    "resorts": """
        SELECT fid, osm_id, osm_way_id, name,
               ST_Area(geom::geography) AS area_m2,
               merged_ids, other_tags AS tags, geom
        FROM osm.multipolygons
        WHERE landuse = 'winter_sports'""",
}


def find_tool(name: str) -> str:
    """Find a PostgreSQL command-line tool.

    Homebrew doesn't put its versioned PostgreSQL formulas on PATH, so their folders are
    searched too, newest version first.

    Args:
        name: The program, such as "psql".

    Returns:
        The path to the program. Exits with an install hint if it isn't found.
    """
    found = shutil.which(name)
    if found:
        return found
    kegs = glob.glob(f"/opt/homebrew/opt/postgresql@*/bin/{name}")
    if kegs:
        return max(kegs, key=lambda p: int(p.split("@")[1].split("/")[0]))
    sys.exit(f"Missing {name}. Install PostgreSQL: brew install postgresql@18 postgis")


def psql_cmd(psql: str, dbname: str) -> list[str]:
    """Build the psql command line used for every database call.

    Args:
        psql: Path to psql.
        dbname: Database to connect to.

    Returns:
        The command as a list. It ignores ~/.psqlrc and stops at the first SQL error.
    """
    return [psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname]


def sql(psql: str, dbname: str, script: str) -> str:
    """Run an SQL script and return its output.

    Args:
        psql: Path to psql.
        dbname: Database to connect to.
        script: One or more SQL statements.

    Returns:
        The results as bare text (unaligned, no headers), stripped.

    Raises:
        SystemExit: If psql fails, with its error message.
    """
    result = subprocess.run(psql_cmd(psql, dbname) + ["-At", "-f", "-"], input=script,
                            capture_output=True, text=True)
    if result.returncode:
        sys.exit(f"psql failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def literal(value: str) -> str:
    """Quote a string as an SQL literal.

    Args:
        value: Any text.

    Returns:
        The text in single quotes, with quotes inside it doubled.
    """
    return "'" + value.replace("'", "''") + "'"


def ensure_database(psql: str, dbname: str) -> None:
    """Create the database if it doesn't exist, and enable the extensions it needs.

    Args:
        psql: Path to psql.
        dbname: Name of the database.
    """
    exists = sql(psql, "postgres",
                 f"SELECT 1 FROM pg_database WHERE datname = {literal(dbname)};")
    if not exists:
        quoted = '"' + dbname.replace('"', '""') + '"'
        sql(psql, "postgres", f"CREATE DATABASE {quoted};")
        log.info("created database %s", dbname)
    sql(psql, dbname, "CREATE EXTENSION IF NOT EXISTS postgis;"
                      "CREATE EXTENSION IF NOT EXISTS pg_trgm;")


def input_signature(gpkg: Path) -> dict:
    """Describe what a load would be built from, to tell whether it needs redoing.

    Args:
        gpkg: The input GeoPackage.

    Returns:
        A dict with the input's path, size and modification time, and a hash of this
        script.
    """
    stat = gpkg.stat()
    return {
        "source": str(gpkg.resolve()),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "loader_hash": hashlib.md5(Path(__file__).read_bytes()).hexdigest(),
    }


def loaded_signature(psql: str, dbname: str) -> dict | None:
    """Read what the live database was built from.

    Args:
        psql: Path to psql.
        dbname: Name of the database.

    Returns:
        The stored signature, or None if nothing has been loaded.
    """
    if not sql(psql, dbname, "SELECT to_regclass('osm.load_info');"):
        return None
    stored = sql(psql, dbname, "SELECT signature FROM osm.load_info;")
    return json.loads(stored) if stored else None


def select_for(gpkg: Path, layer: str) -> str:
    """Build the query that reads one layer out of the GeoPackage.

    other_tags is stored as single-quoted JSON5, which PostgreSQL can't parse, so
    SQLite's json() rewrites it as standard JSON on the way out (SQLite 3.42 or newer).

    Args:
        gpkg: The input GeoPackage.
        layer: Name of the layer.

    Returns:
        A SELECT in SQLite's dialect listing every column of the layer.
    """
    with connect_ro(gpkg) as con:
        columns = [row[1] for row in con.execute(f'PRAGMA table_info("{layer}")')]
    parts = [f'json("{c}") AS "{c}"' if c == "other_tags" else f'"{c}"' for c in columns]
    return f'SELECT {", ".join(parts)} FROM "{layer}"'


def load_layer(psql: str, dbname: str, gpkg: Path, layer: str) -> None:
    """Copy one GeoPackage layer into a table in the staging schema.

    ogr2ogr writes a PostgreSQL dump to its output and psql reads it as it is written,
    so no dump file is stored on disk.

    Args:
        psql: Path to psql.
        dbname: Name of the database.
        gpkg: The input GeoPackage.
        layer: Name of the layer.

    Raises:
        SystemExit: If either program fails.
    """
    json_columns = "other_tags=jsonb,merge_conflicts=jsonb"
    export = [
        "ogr2ogr", "-f", "PGDUMP", "/vsistdout/", str(gpkg),
        "-sql", select_for(gpkg, layer), "-nln", layer, "-nlt", LAYERS[layer],
        "-a_srs", "EPSG:4326", "-preserve_fid",
        "-lco", f"SCHEMA={STAGING}", "-lco", "CREATE_SCHEMA=OFF",
        "-lco", "DROP_TABLE=OFF", "-lco", "FID=fid", "-lco", "GEOMETRY_NAME=geom",
        "-lco", "SPATIAL_INDEX=NONE", "-lco", f"COLUMN_TYPES={json_columns}",
        "--config", "PG_USE_COPY", "YES",
    ]
    log.info("loading %s", layer)
    log.info("$ %s | %s", shlex.join(export), shlex.join(psql_cmd(psql, dbname)))
    producer = subprocess.Popen(export, stdout=subprocess.PIPE)
    consumer = subprocess.run(psql_cmd(psql, dbname), stdin=producer.stdout,
                              stdout=subprocess.DEVNULL)
    # Closing our end lets ogr2ogr stop instead of hanging if psql quit early.
    producer.stdout.close()
    producer.wait()
    if consumer.returncode or producer.returncode:
        sys.exit(f"Loading {layer} failed (ogr2ogr exit {producer.returncode}, "
                 f"psql exit {consumer.returncode}). The live database is unchanged.")


def finish_staging(psql: str, dbname: str, gpkg: Path) -> dict[str, int]:
    """Check the staged tables against the input, then index them.

    Args:
        psql: Path to psql.
        dbname: Name of the database.
        gpkg: The input GeoPackage.

    Returns:
        The number of rows in each table.

    Raises:
        SystemExit: If a table's row count differs from its GeoPackage layer.
    """
    counts = {}
    for layer in LAYERS:
        with connect_ro(gpkg) as con:
            expected = con.execute(f'SELECT count(*) FROM "{layer}"').fetchone()[0]
        counts[layer] = int(sql(psql, dbname, f"SELECT count(*) FROM {STAGING}.{layer};"))
        if counts[layer] != expected:
            sys.exit(f"{layer}: loaded {counts[layer]:,} rows but the input has "
                     f"{expected:,}. The live database is unchanged.")

    log.info("building indexes")
    statements = []
    for layer in LAYERS:
        table = f"{STAGING}.{layer}"
        with connect_ro(gpkg) as con:
            columns = {row[1] for row in con.execute(f'PRAGMA table_info("{layer}")')}
        if "merged_ids" in columns:
            # "w1;w2;r3" -> {w1,w2,r3}, so a source OSM object can be looked up by index.
            statements += [
                f"ALTER TABLE {table} ALTER COLUMN merged_ids TYPE text[] "
                "USING string_to_array(merged_ids, ';');",
                f"CREATE INDEX {layer}_merged_ids ON {table} USING gin (merged_ids);",
            ]
        statements.append(f"CREATE INDEX {layer}_geom ON {table} USING gist (geom);")
        if layer in GEOGRAPHY_LAYERS:
            statements.append(f"CREATE INDEX {layer}_geog ON {table} "
                              "USING gist ((geom::geography));")
        statements += [f'CREATE INDEX {layer}_{c} ON {table} ("{c}") '
                       f'WHERE "{c}" IS NOT NULL;'
                       for c in BTREE_COLUMNS if c in columns]
        if "name" in columns:
            # Trigram index: fast case-insensitive and misspelled name searches.
            statements.append(f"CREATE INDEX {layer}_name_trgm ON {table} "
                              "USING gin (name gin_trgm_ops);")
        statements.append(f"ANALYZE {table};")
    sql(psql, dbname, "\n".join(statements))
    return counts


def swap_in(psql: str, dbname: str, signature: dict, counts: dict, merge_info: dict) -> None:
    """Replace the live osm schema and ski views with the staged load.

    Runs as one transaction, so readers see either the old data or the new.

    Args:
        psql: Path to psql.
        dbname: Name of the database.
        signature: What this load was built from (see input_signature).
        counts: Rows per table.
        merge_info: Contents of ski_nature_merged.json, or {} if it is missing.
    """
    snapshot = merge_info.get("input_snapshot")
    old_tables = ", ".join(f"osm.{t}" for t in [*LAYERS, "load_info"])
    statements = [
        # Hides the "already exists, skipping" notices from the IF EXISTS statements.
        "SET client_min_messages = warning;",
        "BEGIN;",
        f"CREATE TABLE {STAGING}.load_info (loaded_at timestamptz, "
        "osm_snapshot timestamptz, row_counts jsonb, signature jsonb);",
        f"INSERT INTO {STAGING}.load_info VALUES (now(), "
        f"{literal(snapshot) if snapshot else 'NULL'}, "
        f"{literal(json.dumps(counts))}, {literal(json.dumps(signature))});",
        "CREATE SCHEMA IF NOT EXISTS ski;",
        # No CASCADE: anything else that depends on these stops the swap (see module docs).
        *[f"DROP VIEW IF EXISTS ski.{name};" for name in VIEWS],
        f"DROP TABLE IF EXISTS {old_tables};",
        "DROP SCHEMA IF EXISTS osm;",
        f"ALTER SCHEMA {STAGING} RENAME TO osm;",
        *[f"CREATE VIEW ski.{name} AS {query};" for name, query in VIEWS.items()],
        "COMMIT;",
    ]
    sql(psql, dbname, "\n".join(statements))


def main() -> None:
    """Load the merged GeoPackage into PostGIS, unless it is already there."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="data folder (default: %(default)s)")
    parser.add_argument("--input", type=Path,
                        help=f"GeoPackage to load (default: <data-dir>/out/{INPUT_NAME})")
    parser.add_argument("--dbname", default=DEFAULT_DBNAME,
                        help="database name (default: %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="load even if the database is up to date")
    args = parser.parse_args()
    setup_logging()

    gpkg = args.input or args.data_dir / "out" / INPUT_NAME
    if not gpkg.exists():
        sys.exit(f"{gpkg} not found. Run merge_features.py first.")
    if not shutil.which("ogr2ogr"):
        sys.exit("Missing ogr2ogr. Install GDAL (conda install gdal).")
    psql = find_tool("psql")
    # Fail with a clear message rather than waiting at a password prompt.
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    ready = subprocess.run([find_tool("pg_isready"), "-q"])
    if ready.returncode:
        sys.exit("PostgreSQL isn't running. Start it: brew services start postgresql@18")

    ensure_database(psql, args.dbname)
    signature = input_signature(gpkg)
    if not args.force and loaded_signature(psql, args.dbname) == signature:
        log.info("database %s is already up to date", args.dbname)
        return

    # A leftover staging schema from an interrupted load is only ever ours to delete.
    sql(psql, args.dbname, f"SET client_min_messages = warning; "
                           f"DROP SCHEMA IF EXISTS {STAGING} CASCADE; "
                           f"CREATE SCHEMA {STAGING};")
    for layer in LAYERS:
        load_layer(psql, args.dbname, gpkg, layer)
    counts = finish_staging(psql, args.dbname, gpkg)
    merge_info = read_json(gpkg.with_suffix(".json")) or {}
    swap_in(psql, args.dbname, signature, counts, merge_info)

    size = sql(psql, args.dbname,
               "SELECT pg_size_pretty(pg_database_size(current_database()));")
    log.info("loaded %s into database %s (%s)", gpkg.name, args.dbname, size)
    for name in VIEWS:
        rows = int(sql(psql, args.dbname, f"SELECT count(*) FROM ski.{name};"))
        log.info("  ski.%-12s %9s rows", name, f"{rows:,}")


if __name__ == "__main__":
    main()
