#!/usr/bin/env python3
"""Download OpenStreetMap data for the US and Canada, and keep it up to date.

Data comes from Geofabrik (https://download.geofabrik.de), which is free and
rebuilt daily.

First run:  downloads the us and canada files (~19 GB), each checked against
            Geofabrik's .md5 checksum.
Later runs: applies Geofabrik's daily change files instead (~15 MB per day of
            changes), so the data stays current without downloading it again.

Either way, both files must end up at the same snapshot time, because the
filter script merges them and duplicates near the border only line up when
both files are from the same moment.

Usage:
    python osm_data/download_osm.py --data-dir "/path/to/osm_data/data"

Result:
    <data-dir>/raw/us.osm.pbf, <data-dir>/raw/canada.osm.pbf
    <data-dir>/raw/manifest.json   (files, snapshot time, how each was refreshed)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

from common import (
    DEFAULT_DATA_DIR,
    human_size,
    log,
    now_iso,
    read_json,
    require_tools,
    run,
    setup_logging,
    snapshot_time,
    write_json,
)

GEOFABRIK = "https://download.geofabrik.de"
DEFAULT_REGIONS = ["north-america/us", "north-america/canada"]
MAX_ATTEMPTS = 3
MAX_UPDATE_ROUNDS = 20  # each round applies up to --size MB of changes


def region_url(region: str) -> str:
    return f"{GEOFABRIK}/{region}-latest.osm.pbf"


def region_name(region: str) -> str:
    return region.rsplit("/", 1)[-1]


def region_file(raw: Path, region: str) -> Path:
    return raw / f"{region_name(region)}.osm.pbf"


def md5_sidecar(pbf: Path) -> Path:
    """File holding the verified checksum of a freshly downloaded region."""
    return pbf.with_name(pbf.name + ".md5")


def fetch_md5(url: str) -> str:
    out = subprocess.run(
        ["curl", "-sL", "--fail", "--retry", "5", f"{url}.md5"],
        check=True,
        capture_output=True,
        text=True,
    )
    md5 = out.stdout.split()[0].lower() if out.stdout.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{32}", md5):
        sys.exit(f"Could not read checksum from {url}.md5")
    return md5


def remote_size(url: str) -> int | None:
    out = subprocess.run(
        ["curl", "-sIL", "--fail", "--retry", "5", url],
        check=True,
        capture_output=True,
        text=True,
    )
    sizes = re.findall(r"(?im)^content-length:\s*(\d+)", out.stdout)
    return int(sizes[-1]) if sizes else None


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(16 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def download_region(region: str, raw: Path) -> str:
    """Download one Geofabrik region, resuming if interrupted, and verify it."""
    url = region_url(region)
    dest = region_file(raw, region)
    part = raw / f"{dest.name}.part"
    part_md5 = raw / f"{dest.name}.part.md5"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        expected = fetch_md5(url)

        # A partial file from an older Geofabrik build can't be resumed.
        if part.exists() and (not part_md5.exists() or part_md5.read_text().strip() != expected):
            log.info("discarding partial download from an older build")
            part.unlink()
        part_md5.write_text(expected + "\n")

        size = remote_size(url)
        have = part.stat().st_size if part.exists() else 0
        if size is not None and have > size:
            part.unlink()
            have = 0
        if size is None or have < size:
            log.info("downloading %s (%s)", url, human_size(size) if size else "unknown size")
            run(["curl", "-L", "--fail", "--retry", "5", "--retry-delay", "10",
                 "-C", "-", "-o", part, url])

        log.info("verifying checksum of %s", part.name)
        if md5_of(part) == expected:
            part.replace(dest)
            part_md5.replace(md5_sidecar(dest))
            log.info("downloaded %s (%s)", dest.name, human_size(dest.stat().st_size))
            return "downloaded"

        # Usually means Geofabrik rebuilt the file mid-download.
        log.warning("checksum mismatch (attempt %d/%d); starting over", attempt, MAX_ATTEMPTS)
        part.unlink()

    sys.exit(f"Could not get a valid copy of {url} after {MAX_ATTEMPTS} attempts.")


def update_region(region: str, raw: Path) -> str:
    """Bring a downloaded region up to date with Geofabrik's daily change files.

    Falls back to a fresh download if the file can't be updated (for example
    if it is too old for the change files Geofabrik still keeps).
    """
    dest = region_file(raw, region)
    before = snapshot_time(dest)
    log_file = raw / f"{region_name(region)}.update.log"

    for round_ in range(1, MAX_UPDATE_ROUNDS + 1):
        # A short socket timeout makes a stalled request retry sooner.
        cmd = ["pyosmium-up-to-date", "-v", "--socket-timeout", "30", "--tmpdir", str(raw), str(dest)]
        log.info("$ %s", " ".join(cmd))
        with log_file.open("w" if round_ == 1 else "a") as out:
            code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
        if code == 0:
            break
        if code == 1:
            log.info("more changes available; applying the next batch (round %d)", round_ + 1)
            continue
        log.warning("could not update %s (exit %d, see %s); downloading a fresh copy instead",
                    dest.name, code, log_file.name)
        return download_region(region, raw)
    else:
        sys.exit(f"{dest.name} still isn't current after {MAX_UPDATE_ROUNDS} rounds of updates.")

    after = snapshot_time(dest)
    if after != before:
        # The file changed, so the checksum from the original download no longer applies.
        md5_sidecar(dest).unlink(missing_ok=True)
        log.info("updated %s: %s -> %s (%s)", dest.name, before, after, human_size(dest.stat().st_size))
        return "updated"
    log.info("%s is already current (%s)", dest.name, after)
    return "current"


def refresh_region(region: str, raw: Path, force: bool, update: bool) -> str:
    dest = region_file(raw, region)
    if force or not dest.exists():
        return download_region(region, raw)
    if update:
        return update_region(region, raw)
    log.info("skip: %s exists (updates turned off)", dest.name)
    return "kept"


def ensure_same_snapshot(regions: list[str], raw: Path, actions: dict[str, str], update: bool) -> dict[str, str]:
    """Make sure every region is at the same snapshot time.

    osmium merge only removes duplicate objects when their versions match,
    so files from different moments would leave duplicates near borders.
    """
    for attempt in range(2):
        times = {r: snapshot_time(region_file(raw, r)) for r in regions}
        for r, t in times.items():
            log.info("snapshot %-20s %s", region_name(r), t or "(missing)")
        if len(set(times.values())) == 1 and all(times.values()):
            return times
        if attempt == 0:
            newest = max(times.values())
            for r, t in times.items():
                if t != newest:
                    log.info("%s is behind the others; refreshing it again", region_name(r))
                    actions[r] = refresh_region(r, raw, force=not update, update=update)
    sys.exit(
        "The regions are still at different snapshot times. Geofabrik is probably "
        "partway through its daily rebuild; try again in a few hours."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                   help=f"where data is stored (default: {DEFAULT_DATA_DIR})")
    p.add_argument("--regions", nargs="+", default=DEFAULT_REGIONS,
                   help="Geofabrik region paths (default: %(default)s)")
    p.add_argument("--no-update", action="store_true",
                   help="only download missing files; don't apply daily changes to existing ones")
    p.add_argument("--force", action="store_true",
                   help="download fresh copies even if files exist")
    args = p.parse_args()

    setup_logging()
    require_tools("curl", "osmium", *([] if args.no_update else ["pyosmium-up-to-date"]))

    raw = args.data_dir.resolve() / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    update = not args.no_update

    actions = {r: refresh_region(r, raw, args.force, update) for r in args.regions}
    times = ensure_same_snapshot(args.regions, raw, actions, update)

    previous = {e["region"]: e for e in (read_json(raw / "manifest.json") or {}).get("regions", [])}
    entries = []
    for r in args.regions:
        f = region_file(raw, r)
        sidecar = md5_sidecar(f)
        entries.append({
            "region": r,
            "url": region_url(r),
            "file": f.name,
            "bytes": f.stat().st_size,
            "snapshot": times[r],
            "last_action": actions[r],
            "downloaded": now_iso() if actions[r] == "downloaded" else previous.get(r, {}).get("downloaded"),
            "md5_at_download": sidecar.read_text().strip() if sidecar.exists() else None,
        })
    write_json(raw / "manifest.json", {
        "updated": now_iso(),
        "snapshot": next(iter(times.values())),
        "regions": entries,
    })
    log.info("done: %d region(s) at snapshot %s", len(entries), next(iter(times.values())))


if __name__ == "__main__":
    main()
