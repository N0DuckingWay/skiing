"""Helpers shared by the osm_data scripts."""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
OSMCONF = SCRIPT_DIR / "osmconf.ini"

log = logging.getLogger("osm_data")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def require_tools(*names: str) -> None:
    """Stop early with a clear message if a command-line tool is missing."""
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        sys.exit(
            f"Missing tools: {', '.join(missing)}. "
            "Install osmium-tool (brew install osmium-tool) and GDAL (conda install gdal)."
        )


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, logging it first. Raises if it fails."""
    cmd = [str(c) for c in cmd]
    log.info("$ %s", shlex.join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def tmp_path(final: Path) -> Path:
    """Where a step writes before renaming to `final`.

    The prefix keeps the file extension intact, so tools that detect
    format from the extension (osmium, GDAL) still work.
    """
    return final.with_name("tmp_" + final.name)


def finish(tmp: Path, final: Path) -> None:
    """Move a finished temp file into place."""
    tmp.replace(final)
    log.info("wrote %s (%s)", final.name, human_size(final.stat().st_size))


def snapshot_time(pbf: Path) -> str:
    """The OSM data timestamp stored in a .pbf header ('' if missing)."""
    out = subprocess.run(
        ["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", str(pbf)],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    tmp = tmp_path(path)
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_ro(gpkg: Path) -> sqlite3.Connection:
    """Open a GeoPackage read-only with plain sqlite3."""
    return sqlite3.connect(gpkg.resolve().as_uri() + "?mode=ro", uri=True)


def fid_range(gpkg: Path, layer: str) -> tuple[int | None, int | None]:
    with connect_ro(gpkg) as con:
        return con.execute(f'SELECT MIN(fid), MAX(fid) FROM "{layer}"').fetchone()


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


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
