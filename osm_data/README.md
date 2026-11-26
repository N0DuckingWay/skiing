# OSM data: ski runs and everything within 10 miles

Two scripts turn free OpenStreetMap data for the US and Canada into:
- every downhill ski run;
- every OSM feature of any type (land cover, water, lifts, roads, buildings, boundaries, …) with any part within 10 miles of a run.

## Requirements
The local database also needs PostgreSQL and PostGIS (see "Local database"). The rest is already in the `coding-projects` conda env:
- **osmium-tool:** command-line tool for filtering raw OSM files (`brew install osmium-tool`).
- **GDAL:** converts map file formats; its `ogr2ogr` command writes the GeoPackage layers.
- **Python packages:** geopandas, shapely, pyogrio; `merge_features.py` also uses pyosmium, scipy and rapidfuzz, and its tests use pytest.

## Run it
Keep the data in the main checkout, not in a git worktree, so it survives worktree cleanup:

```bash
python osm_data/download_osm.py --data-dir "/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data"
```

```bash
python osm_data/filter_near_runs.py --data-dir "/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data"
```

**Keeping the data current:** run the same two commands again.
- The first download pulls ~19 GB.
- After that, `download_osm.py` applies Geofabrik's daily change files instead: about 15 MB per day of changes (~11 MB US, ~4 MB Canada).
- If a file is too old to update that way, the script downloads a fresh copy.
- `--no-update` only fetches missing files; `--force` downloads fresh copies.

**Safe to rerun:**
- The filter only rebuilds what's older than the downloaded data, so a rerun with no new data does nothing.
- If the filter is interrupted, rerunning picks up where it stopped.
- `--force` rebuilds everything.

**Temporary files are deleted as soon as they're no longer needed.** For example, the merged US + Canada file (~19 GB) is gone before the GeoPackage step starts. Pass `--keep-work` to keep them; `check_outputs.py` needs them for two of its checks.

## What you get (`<data-dir>/out/`)
| File | Contents |
|---|---|
| `features_near_runs.gpkg` | Every feature with any part within 10 mi of a run. Layers: `points`, `lines`, `multilinestrings`, `multipolygons`, `other_relations`. |
| `runs.gpkg` | Downhill runs only (`piste_type = 'downhill'`), same layers. |
| `zone_10mi.gpkg` | The 10-mile zone around all runs. |
| `runs.osm.pbf`, `near_runs.osm.pbf` | The same data in raw OSM format, for tools like pyosmium. `near_runs` reaches slightly past 10 mi. |
| `manifest.json` | OSM snapshot date, settings, and feature counts per layer. |

Columns:
- Common tags are real columns: `name`, `natural`, `landuse`, `piste_type`, `piste_difficulty`, `aerialway`, `leaf_type`, `ele`, …
- GDAL turns `:` into `_` in column names, so the OSM tag `piste:type` becomes the column `piste_type`.
- Every other tag is in `other_tags` as JSON.
- See `osmconf.ini` for the full list.

Read a layer with:

```python
import geopandas as gpd
woods = gpd.read_file("out/features_near_runs.gpkg", layer="multipolygons", where="natural = 'wood' OR landuse = 'forest'")
```

## Cleaning: keep only ski and nature features
`clean_features.py` is the first cleaning step. It copies `features_near_runs.gpkg` into `out/ski_nature.gpkg`, keeping only skiing and natural features. Buildings, roads, street furniture, boundaries and transit are dropped. The source file is not changed.

```bash
python osm_data/clean_features.py --data-dir "/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data"
```

| File | Contents |
|---|---|
| `ski_nature.gpkg` | Same layers and columns as `features_near_runs.gpkg`, only the kept features. |
| `ski_nature.json` | Input snapshot, a hash of the rules, and counts per layer and per rule. |

A feature is kept if any of these match. The rules are in `general_rules()` in the script.
- **natural:** any `natural=*` tag: woods, wetlands, glaciers, peaks, single trees, cliffs, ridges, …
  - Except water bodies: `natural=water/bay/strait/lake/pond` areas, and the points that label them, are dropped.
- **piste:** any `piste:*` tag.
- **lifts:**
  - Every `aerialway=*` value: chair lifts, gondolas, T-bars, rope tows, magic carpets, stations, pylons, and also zip lines and goods lifts.
  - Lifecycle keys such as `proposed:aerialway` and `abandoned:aerialway`.
  - `railway=funicular`.
- **ski areas:** `landuse=winter_sports`, `site=piste`, `sport=*ski*` (not water skiing), `ski_resort=*`.
- **ski access:** `ski=*`, `xc_ski=*` and `ski:*=*` tags, unless the value is `no`.
- **ski POIs:** ski shops, ski rental and ski schools, alpine and wilderness huts, viewpoints.
- **ski tags:** any other tag whose value says skiing.
  - That means the word "ski" or "skiing" (not water skiing), or "piste", "nordic", "snowboard", "biathlon", "snow_cannon", "snowmaking" or "winter_sports".
  - This catches ski jumps, snow guns, lift buildings, snowmaking ponds, snowboard parks, biathlon ranges, nordic trail networks, abandoned ski areas, and values like `highway=piste`.
  - Names, notes, addresses, contacts and owners are not checked (`TEXT_KEYS`), so a bus stop on "Ski Hill Road" doesn't count.
- **ski trails:** paths, tracks, footways and bridleways whose name says they are ski trails.
  - Examples: "X-C Ski Trail", "Nordic Loop", "Piste de ski de fond 4", "Cat Track".
  - Road names ("Ski Hill Road", "Ski Area Access") and snowmobile trails ("Ski-Doo") don't count.
- **cutlines:** `man_made=cutline`, the straight clearings through forest (firebreaks, power-line and border cuts, some lift lines).
- **vegetation** (buildings excluded):
  - `landuse=forest/meadow/grass`.
  - Vegetation `landcover` values.
  - `leaf_type` or `wood` tags.
  - `wetland` tags for marshes, swamps and other wetland types.
- **waterways:** streams, rivers, rapids, waterfalls, flowlines, canals and ditches, as lines and points.
  - River areas are dropped with the other water; so are `water=*` tags on their own.
  - Dams, weirs, drains and pipes are dropped.
- **ski routes:** route relations get only this rule.
  - A route is kept if its `piste:type` includes a ski type: anything not in `NON_SKI_PISTE_TYPES`, such as downhill, nordic, skitour or traverse. Snowshoe, sled, fatbike and snowmobile routes are dropped.
  - A route with no `piste:type` is kept if it is a ski, piste or aerialway route.
  - Also kept: routes with ski access tags (`ski=designated`), and hiking, foot or horse routes named as ski trails. Bus and bike routes named after a ski area are not kept.

**Clearcuts are not forest.** In OSM a clearcut (`man_made=clearcut`, `landuse=logging`, `landcover=clearcut`) is often drawn on top of a larger wood or forest polygon.
- Clearcut shapes are cut out of the kept forest polygons (`landuse=forest`, `natural=wood`, tree landcover) that overlap them. A forest that lies entirely inside clearcuts is dropped.
  - Runs, resort areas and other ski features keep their shape, even when tagged as forest.
  - Cut forests are added back at the end, so they get new fids. `osm_id` / `osm_way_id` still identify them.
- A clearcut that is itself tagged as forest is dropped. A clearcut tagged as scrub or grassland is kept.
- Clearcuts on their own are not kept.
- `ski_nature.json` records how many forest polygons were cut and how much area was removed.

Not counted as lifts:
- `barrier=lift_gate` is a boom barrier at a car park.
- `conveying=*` is mostly escalators and moving walkways. Real magic carpets are tagged `aerialway=magic_carpet`.

**Safe to rerun:** it rebuilds only when `features_near_runs.gpkg` is newer than the output or the rules have changed. `--force` rebuilds anyway.

**Checking rule changes:** `--report` prints how many features each rule matches, per layer, without writing anything.

## Cleaning: merge split features
`merge_features.py` is the second cleaning step. OSM often stores one real thing as several objects: a run cut into pieces where it crosses a lift, a gondola drawn as two sections, a river cut into hundreds of ways, or a piste route relation drawn on top of its own ways. This step joins them into one feature, matching on **name + location**. It reads `ski_nature.gpkg`, plus `near_runs.osm.pbf` for relation member lists (the GeoPackage doesn't keep them). Neither input is changed.

```bash
python osm_data/merge_features.py --data-dir "/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data"
```

| File | Contents |
|---|---|
| `ski_nature_merged.gpkg` | Same layers and columns as `ski_nature.gpkg`; each merged feature is one row. Untouched rows keep their fid. |
| `ski_nature_merged.json` | Inputs, a hash of the rules, and counts per layer and per kind of feature. |
| `merge_groups.csv` | One row per merged feature (the "after"): its row in the output, the OSM objects it came from with openstreetmap.org links, a map link, names and difficulty before and after, and every attribute conflict with how it was resolved. |
| `merge_pieces.csv` | One row per source object (the "before"): OSM link, its fid in `ski_nature.gpkg`, name, kind and key tags, and its role (`representative`, `merged`, `name inherited`, `copy dropped`, `relation absorbed`). |
| `merge_near_misses.csv` | Pairs that touch but were *not* merged, with the reason (`qualifier differs`, `name too different`, `different kind`, `twin lifts`, `lifts touch mid-way`, `relation kept`). Use it to check nothing was missed. It also lists merged pieces that run side by side (`drawn twice?`, see below). |
| `merge_review.html` | The groups and near misses as one page you can filter, with clickable links. Open it in a browser. |

All links point at the objects as they were in the OSM snapshot. If an object changed since then, the History tab on its openstreetmap.org page shows the version used.

New columns on every layer except `points` (empty on untouched rows):
- `merge_group`: joins to `merge_groups.csv`.
- `merged_ids`: the source objects, e.g. `w123;w456;r789` (w = way, r = relation). The first is the one whose id the row keeps in `osm_id` / `osm_way_id`: the longest (or largest) piece.
- `merged_count`, `merge_match` (`exact`, `fuzzy` or `inherited`), `merge_conflicts` (JSON: each conflicting tag's values and the one chosen).

**The `lines` layer now holds both LineStrings and MultiLineStrings.** A run that forks, splits around trees and rejoins, or has a piece joining mid-run becomes one MultiLineString feature.

**When two features merge.** All of these must hold:
- **Names match.** Names are compared after normalizing accents, case, punctuation, spacing and possessives ("Cassidy's Traverse" = "Cassidy Traverse", "Bear Claw" = "Bearclaw"), and after expanding abbreviations ("U. Cruiser" = "Upper Cruiser").
  - Small misspellings are allowed: one letter in names of 8+ letters, two in names of 16+ ("Winnisook" = "Winisook").
  - Qualifier words must be the same: upper/lower/middle, north/south/east/west (and French equivalents), numbers, single letters and roman numerals. So "Upper White Lightning" ≠ "Lower White Lightning" and "Gondola I" ≠ "Gondola II".
  - `piste:name` is used when there is no `name`.
- **They touch.** For lines, an end of one is within 15 m of the other; any gap is closed with a short straight connector. For polygons, they are within 5 m or overlap.
  - Distances and lengths are measured on the ground (on the WGS84 ellipsoid), so 15 m is 15 m everywhere. A single map projection of North America would be off by up to 6% in the contiguous US, and more in the far north.
- **Same kind of feature:**
  - Ski pistes need a ski type in common (`nordic` merges with `nordic;hike`; downhill never merges with nordic).
  - Lifts need the same `aerialway` value and must meet end to end. A lift never merges with its station. Two lifts of similar length (within 80%) that run side by side, no more than 30 m apart, stay separate: twin rope tows, dual zip lines, or the two cables of a tram drawn in opposite directions, where only one pair of ends meets (the Sandia Peak Aerial Tramway).
  - Streams, rivers and flowlines (a river's path through a lake) merge with each other; other waterways need the same value. Waterfalls always stay separate, even when they are part of a river's relation.
  - Woods and forests (`natural=wood`, `landuse=forest`, tree landcover) merge with each other, as do grass, meadow and grassland. A merged polygon keeps every tag its pieces had.
  - Everything else needs the same main tag (`natural=cliff` with `natural=cliff`, …).
- **Unnamed features are never merged**, with one exception: an unnamed way in exactly one named route or waterway relation takes that relation's name.

**Relations.** If every way of a route or waterway relation that is in the `lines` layer carries the relation's name, the relation and those ways are the same feature. They become one row in `lines`, and the relation's ways that aren't in `lines` (untagged ways, or ways past the 10-mile zone) are added to its geometry, so a long river keeps its whole length. A waterway relation also takes in canal and ditch ways that carry its name, but a waterfall stays a feature of its own. Relations whose ways carry other names stay as they are: for example nordic loops built from shared, separately named trails. Leftover route relations with the same name that touch are merged with each other.

**Combining attributes:**
- The hardest `piste:difficulty` wins: novice < easy < intermediate < advanced < expert < freeride < extreme.
- Multi-value tags (`piste:type`, `piste:grooming`, `ref`, …) are combined, e.g. `classic` + `skating` → `classic;skating`.
- `gladed`, `lit` and `piste:snowmaking` are `yes` if any piece says yes.
- The name is the spelling covering the most length.
- Any other disagreement goes to the longest (or largest) piece.
- A tag only some pieces have is kept.

**Not changed:** points, and run areas vs. run lines (they're left as separate features).

**Lines drawn twice:**
- A line that lies within 3 m of another for 90% of its length is a copy: its tags are merged, but only the longer drawing is kept ("copy dropped"). Either both its ends are within 3 m too, or one is and it runs along the other line, as where a run was redrawn with a different end (Upper Buzzsaw).
- Pieces of one feature that run side by side further apart (80% of the shorter within 15 m, without rejoining) are both kept, and the pair is listed in `merge_near_misses.csv` as `drawn twice?`. Some are one thing drawn twice, such as a resort redrawn without deleting the old runs, or an imported creek beside a newer drawing. Others are real, such as tubing lanes, twin culverts and one-way ski tracks. Their shapes don't tell which, so fix the first kind in OSM itself. Until then, the merged length counts both drawings.

**Self-checks** (the script stops without writing if one fails):
- Merged lengths equal the pieces plus connectors, and merged areas lie between the largest piece and the sum of the pieces.
- Every replaced row is in exactly one merged feature.
- The row counts add up.
- Running the matching again on the result finds nothing left to merge.

**Safe to rerun:** it rebuilds only when an input is newer than the output or the script changed. `--force` rebuilds anyway, and `--report` prints what would be merged per kind of feature without writing anything.

**Tests:** `python -m pytest osm_data/tests` checks the name matching, attribute rules and linking on small examples.

## Local database (PostGIS)
`load_database.py` loads `ski_nature_merged.gpkg` into a local PostgreSQL database with the PostGIS extension, so the data can be queried with SQL. The GeoPackage isn't changed, and no features are changed, dropped or merged on the way in.

**One-time setup** (installs PostgreSQL 18 and PostGIS, then runs the server in the background, also after restarts):

```bash
brew install postgresql@18 postgis
```

```bash
brew services start postgresql@18
```

**Load or reload** after the pipeline has produced new data (about 2½ minutes, 2.1 GB):

```bash
python osm_data/load_database.py --data-dir "/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data"
```

**Open a SQL prompt:**

```bash
/opt/homebrew/bin/psql skiing
```

Desktop tools such as QGIS (to see the data on a map) connect to host `localhost`, port `5432`, database `skiing`, with your macOS user name and no password. Homebrew's setup accepts only connections from this Mac, and trusts all of them without a password.

### What's in it
| Schema | Contents |
|---|---|
| `osm` | `points`, `lines`, `multilinestrings`, `multipolygons`, `other_relations`: one table per GeoPackage layer, with the same `fid`s and columns. `load_info` records when it was loaded, the OSM snapshot, row counts and the input file. |
| `ski` | Views over those tables for common questions (below). |

Differences from the GeoPackage:
- `other_tags` and `merge_conflicts` are `jsonb`: `other_tags->>'aerialway:capacity'`.
- `merged_ids` is a text array: `merged_ids @> array['w4271938']` finds the merged feature containing an OSM way. Unmerged rows have no `merged_ids`; find them by `osm_id` (or `osm_way_id`).
- `natural` is a reserved word in SQL, so quote it: `"natural" = 'wood'`.
- Geometry is in `geom` (longitude/latitude, EPSG:4326).

| View | Rows | Contents |
|---|---|---|
| `ski.runs` | lines whose `piste:type` includes `downhill` | `name`, `difficulty`, `grooming`, `oneway`, `gladed`, `length_m` |
| `ski.run_areas` | downhill areas | `name`, `difficulty`, `grooming`, `gladed`, `area_m2` |
| `ski.run_routes` | downhill route relations not merged into their ways; they can overlap `ski.runs` | `name`, `difficulty` |
| `ski.lifts` | `aerialway` lines that carry skiers: cable car, gondola, mixed, chair, drag, T-bar, J-bar, platter, rope tow, magic carpet. Not stations, zip lines, goods lifts, proposed or abandoned lifts, `yes`/`unknown`, or funiculars. | `name`, `lift_type`, `length_m` |
| `ski.resorts` | `landuse=winter_sports` areas. Some real resorts have no such outline in OSM. | `name`, `area_m2` |

Every view also has `fid`, `osm_id`, `merged_ids`, `tags` (the `other_tags`) and `geom`.

**Runs aren't assigned to resorts yet.** Join them by location:

```sql
SELECT run.difficulty, count(*), round(sum(run.length_m) / 1000) AS km
FROM ski.resorts AS resort
JOIN ski.runs AS run ON ST_Intersects(resort.geom, run.geom)
WHERE resort.name = 'Vail'
GROUP BY run.difficulty;
```

**Runs within 300 m of a position** (longitude first). Casting to `geography` gives distances in metres and uses an index:

```sql
SELECT name, difficulty,
       ST_Distance(geom::geography, ST_Point(-106.3742, 39.6061, 4326)::geography) AS metres
FROM ski.runs
WHERE ST_DWithin(geom::geography, ST_Point(-106.3742, 39.6061, 4326)::geography, 300)
ORDER BY metres;
```

**Search by name, allowing misspellings:**

```sql
SELECT name, difficulty FROM ski.runs
WHERE name % 'Riva Ridg'
ORDER BY similarity(name, 'Riva Ridg') DESC;
```

### Reloading
- **Safe to rerun:** it does nothing if the database already holds this input and this version of the script. `--force` reloads anyway.
- **All or nothing:** the new data is built in a staging schema (`osm_new`), checked against the GeoPackage row counts, then swapped in at once. If anything fails, the old data stays. A failed load can leave `osm_new` taking up disk space; the next load removes it.
- **Your own tables belong in another schema.** A reload replaces the `osm` schema and the views above. If something of yours depends on them (a view or a foreign key), the reload stops with an error naming it rather than deleting it; drop it, reload, then recreate it. Don't add your own objects inside `osm`.
- `fid`s can change when the pipeline is rerun, so don't use them as permanent ids.

Flags: `--data-dir`, `--input` (another GeoPackage), `--dbname` (default `skiing`), `--force`. To use another server, set the usual `PGHOST`, `PGPORT` and `PGUSER` variables.

## How it works
1. **Download** (`download_osm.py`):
   - Fetches Geofabrik's `us` and `canada` extracts (~19 GB) and checks each against its `.md5`.
   - On later runs it updates them with `pyosmium-up-to-date` instead.
   - Confirms both are at the same snapshot time.
2. **Merge** (filter script, temporary): combines the two files so features that cross the border stay complete.
3. **Runs:** `osmium tags-filter` pulls out `piste:type=downhill`.
4. **Zones:**
   - Buffers runs by 10 mi, each in its local UTM projection, so distances are accurate everywhere.
   - Also draws a coarse 11-mile zone for the next step.
5. **Pass 1:** `osmium extract --strategy smart` keeps everything with a point inside the coarse zone. Multipolygon and boundary relations (forests, lakes, protected areas) are kept complete.
6. **Pass 2:**
   - Finds large areas that overlap the zone but have no point inside it, such as a national forest or the state around an inland resort.
   - Then fetches them complete with `osmium getid -r`.
7. **Trim:** converts to GeoPackage with GDAL and keeps only features that intersect the exact 10-mile zone.

## Cost (full US + Canada)
Measured on the first full run (snapshot 2026-11-08), on a 12-core Mac with 16 GB RAM.
- **Kept between runs:** 32 GB.
  - `raw/`: the two country files, 17.5 GB. They stay so they can be updated.
  - `out/`: results, 14 GB. Almost all of it is `features_near_runs.gpkg` (12.3 GB); `near_runs.osm.pbf` is 2.1 GB.
  - `work/`: about 5 MB after a run.
- **Peak while filtering:** roughly 55 GB, during pass 2. At that point the temporary merged file (17.5 GB), the candidate areas file (6.1 GB) and a node index all exist at once. With `--keep-work`, about 72 GB stays until a rerun without it cleans up.
- **While updating:** `pyosmium-up-to-date` briefly writes a second copy of the file it's updating (up to ~12 GB for the US).
- **Time:**
  - First download: 32 min (~10 MB/s).
  - Filter: 1 h 12 min. The longest step is pass 2's scan of 33 million candidate areas (36 min).
  - Checks: about 15 min.
  - Daily update: usually seconds, longer if Geofabrik is slow to respond.

## Checking the results
```bash
python osm_data/check_outputs.py --data-dir "/Users/zdhoffman/Coding Projects/Skiing App/osm_data/data"
```
For the full set of checks, run the filter with `--keep-work` first. Without it, the "exactness" and "pass 2" checks are skipped, because the files they compare against have been cleaned up.

This checks:
- Every run is inside the zone.
- The zone edge is 10 mi from the nearest run.
- In random samples, kept features are within 10 mi and dropped features are not.
- Large areas found by pass 2 reached the final file.
- State and province outlines are present.
- No way in the output is missing nodes.

When testing on a single-state Geofabrik extract, the state outline check usually fails. That's expected: those extracts cut off parts of the state's own border, so its shape can't be built. The full US file doesn't have this problem.

## Accuracy and known limitations
- **Distances:** accurate to about 50 m (0.3% of 10 mi). Circles are drawn with straight segments, and GEOS simplifies long lines slightly when buffering.
- **Long straight lines:** a plain line (not an area) can be missed if it crosses into the 10-mile zone along a segment longer than about a mile with no vertex inside the 11-mile margin, for example a very straight power line. Areas are not affected.
- **Broken areas:** areas that are broken in OSM itself, such as a forest outline with a gap, can't be built into shapes. They're counted in `work/area_errors.log`.
