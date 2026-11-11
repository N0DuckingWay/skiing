# OSM data: ski runs and everything within 10 miles

Two scripts turn free OpenStreetMap data for the US and Canada into:
- every downhill ski run;
- every OSM feature of any type (land cover, water, lifts, roads, buildings, boundaries, …) with any part within 10 miles of a run.

## Requirements
These are already in the `coding-projects` conda env:
- **osmium-tool:** command-line tool for filtering raw OSM files (`brew install osmium-tool`).
- **GDAL:** converts map file formats; its `ogr2ogr` command writes the GeoPackage layers.
- **Python packages:** geopandas, shapely, pyogrio.

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
