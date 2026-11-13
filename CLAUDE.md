# Skiing App

## Purpose
An **iOS app that helps skiers find useful information about the mountain they're on**:
- turn-by-turn directions around the mountain,
- slope (run) recommendations,
- current slope conditions,
- and more as the product grows.

Planned order of work:
1. **Dataset of ski runs and resorts** (in progress): built from OpenStreetMap for the US and Canada.
2. **Personalized run ratings and recommendations**: score runs for each skier (ability, preferences, terrain such as glades or groomed).
3. **Localized run condition information**: current conditions per run.
4. The iOS app itself. No app code exists yet.

Keep the dataset work in mind as the foundation: later stages (ratings, routing, conditions) will consume the cleaned runs, lifts and nature layers, so output schemas matter.

## Keeping this file current (required)
This file must stay accurate. **Before finishing any task that changes the repo, update CLAUDE.md in the same change** if it affects anything below: scripts, CLI flags, output files or schemas, rules, tooling, CI, conventions, or project status/roadmap.
- Edit the relevant section; don't append changelog-style notes.
- Move finished roadmap items to "Status" and add new work to "Roadmap".
- When a script gains a new cleaning step, document it under "Data pipeline" and in `osm_data/README.md`.
- `scripts/check_claude_md.sh` is a Stop-hook script that reminds Claude once per turn when files changed but CLAUDE.md didn't. It must be registered as a `Stop` command hook in `.claude/settings.json` (`"$CLAUDE_PROJECT_DIR"/scripts/check_claude_md.sh`); if that entry is missing, add it. If nothing here needs changing, say so and move on.
- Facts derivable from code (every function name) don't belong here; record what is non-obvious: purpose, decisions, gotchas, how to run things.

## Status
- Done: download Geofabrik US + Canada OSM extracts and keep them current; extract downhill runs; extract all features within 10 mi of any run; first cleaning pass (`clean_features.py`) keeping ski and nature features.
- Next cleaning steps (planned, not written): merging split features (same name + touching), per the preferences below.
- Not started: run difficulty/rating model, personalization, conditions data, routing, iOS app.

## Repository layout
```
README.md                      one-line title ("# skiing")
CLAUDE.md                      this file
osm_data/                      OSM data pipeline (all Python so far)
  README.md                    detailed user docs for the pipeline (keep in sync)
  common.py                    shared helpers
  download_osm.py              download/update Geofabrik extracts
  filter_near_runs.py          runs + 10-mile zone + features near runs
  clean_features.py            keep only ski and nature features
  check_outputs.py             sanity checks on filter output
  osmconf.ini                  GDAL OSM driver config (project copy)
  data/                        GIT-IGNORED; multi-GB inputs and outputs
scripts/check_claude_md.sh     Stop-hook helper (CLAUDE.md freshness reminder)
.github/workflows/             ci.yml, claude.yml, claude-code-review.yml
.claude/settings.json          project hooks (Stop hook for CLAUDE.md freshness)
.claude/settings.local.json    local permissions (not shared)
```

## Environment
- Python from the `coding-projects` conda env: geopandas, shapely, pyogrio, numpy (plus pyosmium for `pyosmium-up-to-date`).
- CLI tools: `osmium-tool` (`brew install osmium-tool`), GDAL (`ogr2ogr`), `curl`.
- Scripts import `common` directly, so run them as `python osm_data/<script>.py` (script dir is on `sys.path`).
- **Data directory**: keep real data in the main checkout, not a git worktree, so worktree cleanup doesn't delete it:
  `/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data`. Always pass `--data-dir` explicitly when working from a worktree (default is `osm_data/data` beside the script).
- Disk: ~32 GB kept, ~55 GB peak while filtering (72 GB with `--keep-work`). First download ~19 GB.

## Data pipeline (`osm_data/`)
Data flow:
```
Geofabrik us + canada .osm.pbf  --download_osm.py-->  data/raw/
data/raw  --filter_near_runs.py-->  data/out/{runs.*, zone_10mi.gpkg, near_runs.osm.pbf, features_near_runs.gpkg, manifest.json}
features_near_runs.gpkg  --clean_features.py-->  data/out/{ski_nature.gpkg, ski_nature.json}
data/out  --check_outputs.py-->  PASS/FAIL report
```

### `download_osm.py`
- Downloads `north-america/us` and `north-america/canada` from Geofabrik, verified against `.md5`, resumable via `.part` files.
- Later runs use `pyosmium-up-to-date` (daily diffs, ~15 MB/day) and fall back to a fresh download if too old.
- **Both regions must end at the same snapshot time** (`osmium merge` only dedupes border objects with matching versions); it re-refreshes the laggard once, then exits.
- Flags: `--data-dir`, `--regions`, `--no-update`, `--force`.
- Writes `raw/manifest.json` (files, snapshot, last action).

### `filter_near_runs.py`
- "Runs" = `nwr/piste:type=downhill`. Output: runs, 10-mile zone, and every OSM feature with any part within 10 mi.
- Implemented as a small make-like `Pipeline` of `Target`s: a target is fresh if its files exist and are newer than the raw data; temp targets are deleted once no reader needs them (`--keep-work` disables). Resumable after interruption.
- Steps: merged -> runs -> zones -> pass1 -> areas -> big_ids -> pass2 -> near -> raw_gpkg -> features -> runs_gpkg.
- Buffers each run in its local UTM zone (accurate distances continent-wide); exact zone plus a coarse zone (+1 mi margin, simplified) for pass 1.
- Pass 1 `osmium extract --strategy smart` keeps everything with a point in the coarse zone. Pass 2 finds large areas (national forests, states) overlapping the zone without a node inside, and fetches them complete with `osmium getid -r`.
- Final trim keeps only features intersecting the exact zone (tiled for speed).
- Changing `--miles`, `--margin-miles`, filters or `osmconf.ini` requires `--force` (settings are compared against `manifest.json`).
- Flags: `--data-dir`, `--input` (single file, for testing), `--miles`, `--margin-miles`, `--force`, `--keep-work`.

### `clean_features.py`
- Reads `features_near_runs.gpkg` (never modified), writes `ski_nature.gpkg` (same layers/columns) and `ski_nature.json` (rules hash, per-layer and per-rule counts, clearcut stats).
- Keep rules are SQL built in `general_rules()`; a feature is kept if any matches: natural (not water bodies), piste, lifts, ski_areas, ski_access, ski_pois, ski_tags, ski_trails, cutlines, vegetation, waterways (lines/points only). Route relations are judged only by `ski_routes`. See `osm_data/README.md` for the full rule descriptions.
- Clearcuts are not forest: clearcut shapes are cut out of overlapping forest polygons (re-appended with new fids; `osm_id`/`osm_way_id` still identify them).
- Rebuilds only if the input is newer or the rules hash changed. `--report` prints per-rule match counts without writing; `--force` rebuilds.
- Gotchas: `other_tags` is single-quoted pseudo-JSON (needs SQLite 3.42+ JSON5); the `Layer.tag()` helper does a cheap `LIKE` before `json_extract`. GDAL turns `:` into `_` in column names (`piste:type` -> `piste_type`). `TEXT_KEYS` are excluded from the "tag says ski" rule so street names don't match.

### `check_outputs.py`
- Checks: counts, known resorts have runs, zone covers runs, zone edge distance, sampled exactness, pass-2 areas present, state polygons, `osmium check-refs`.
- Exactness/pass-2 checks need `filter_near_runs.py --keep-work`. State-outline check fails on single-state extracts (expected).
- Exits non-zero on failure.

### `osmconf.ini`
Project copy of GDAL's OSM config: more closed-way polygon keys, ski and land-cover tags promoted to real columns (`piste:*`, `gladed`, `aerialway`, `leaf_type`, `landcover`, `surface`, `ele`, ...). Every layer has `piste_type`. Its md5 is part of the filter settings.

### `common.py`
`DEFAULT_DATA_DIR`, `OSMCONF`, logging, `require_tools`, `run` (logs then runs), `tmp_path`/`finish` (write to `tmp_<name>` then rename atomically), `snapshot_time`, JSON helpers, `human_size`.

### Output layers
`points`, `lines`, `multilinestrings`, `multipolygons`, `other_relations`; common tags as columns, the rest in `other_tags`.

## Data-cleaning preferences (from the project owner)
- **Every cleaning step that changes or merges features must also write a review output** (CSV/HTML) listing OSM identifiers (way/relation ids with openstreetmap.org links, plus map links) before and after, so changes can be verified on the OSM website.
- Treat `natural=wood`, `landuse=forest` and tree landcover as the same kind of thing; likewise `landuse=grass`, `landuse=meadow` and `natural=grassland`.
- "The same feature" = same name (minor misspellings ignored) + touching or very close location. For ski runs, when pieces disagree on difficulty, the hardest wins.
- Run area polygons and run centerlines stay separate (not linked or merged).

## Conventions
- Scripts are re-runnable and idempotent: skip when output is newer than input; write via `tmp_` file then rename; clean intermediates.
- Docs live next to code: update `osm_data/README.md` whenever behavior, flags or outputs change.
- Never commit data (`*.osm.pbf`, `osm_data/data/`, indexes) or credentials (`proxyinfo.txt`).
- No test suite yet; `check_outputs.py` is the validation tool.

## CI and GitHub
- Default branch: `dev`. `ci.yml` runs on pushes to `dev` and on PRs, with a size gate (PRs touching one file and 50 or fewer changed lines, clean merges of `dev`, and workflow-only PRs skip).
  - Lint: `ruff check --isolated --select E4,E9,F osm_data/` (syntax errors, import placement, pyflakes). Run the same locally before pushing.
  - Test: only `python -m compileall -q osm_data` until a pytest suite exists.
- `claude.yml`: responds to `@claude` mentions on issues and PRs. `claude-code-review.yml`: automated PR review.
