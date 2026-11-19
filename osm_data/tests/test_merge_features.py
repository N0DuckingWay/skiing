"""Tests for merge_features.py: name matching, attribute rules and line linking.

Run with:  python -m pytest osm_data/tests
"""

import sys
from pathlib import Path

import numpy as np
import pyproj
import pytest
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import merge_features as mf  # noqa: E402


# ---------------------------------------------------------------- names

def match(a, b):
    """Compare two names as written in OSM.

    Args:
        a: The first name.
        b: The second name.

    Returns:
        compare_names' (match, near_miss) tuple for the two names.
    """
    return mf.compare_names(mf.name_key(a), mf.name_key(b))


@pytest.mark.parametrize("a, b", [
    ("Roller Coaster", "Rollercoaster"),
    ("Bear Claw", "Bearclaw"),
    ("Cassidy Traverse", "Cassidy's Traverse"),
    ("Russ's Street", "Russ' Street"),
    ("Home Run", "Homerun"),
    ("Dream Maker", "Dreammaker"),
    ("La Cascapédia", "La Cascapedia"),
    ("Winnisook", "Winisook"),
    ("West Fork", "Westfork"),
    ("U. Cruiser", "Upper Cruiser"),
    ("Westside", "West Side"),
    ("XC Ski Trail", "X-C Ski Trail"),
])
def test_names_that_match(a, b):
    assert match(a, b)[0] == "fuzzy"


def test_identical_names_are_exact():
    assert match("Lynx", "Lynx") == ("exact", None)


@pytest.mark.parametrize("a, b, reason", [
    ("Upper White Lightning", "Lower White Lightning", "qualifier differs"),
    ("Firecracker East", "Firecracker West", "qualifier differs"),
    ("U. Cruiser", "L. Cruiser", "qualifier differs"),
    ("7A - Bosses - La Dufour", "7 - Bosses - La Dufour", "qualifier differs"),
    ("Sunshine Village Gondola I", "Sunshine Village Gondola II", "qualifier differs"),
    ("Granite Flat Campground Loop A Road", "Granite Flat Campground Loop C Road", "qualifier differs"),
    ("Fuddle Duddle", "Fuddle Duddle Bas", "qualifier differs"),
    ("La Charlevoix (Haut)", "La Charlevoix (Bas)", "qualifier differs"),
    ("The Vain", "The Main", "name too different"),
    ("Eastside", "Westside", "name too different"),
])
def test_names_kept_apart(a, b, reason):
    assert match(a, b) == (None, reason)


@pytest.mark.parametrize("a, b", [
    ("Réaction", "Action"),
    ("Invitation", "Sunvitation"),
    ("Sunrise Blvd.", "Sunset Blvd."),
    ("Lower Walking Boss Woods", "Lower Walking Boss"),
])
def test_names_clearly_different(a, b):
    assert match(a, b)[0] is None


def test_swapped_letters_count_as_one_edit():
    # OSA distance counts two swapped neighbouring letters as one edit. Plain
    # Levenshtein would count two and keep these names apart.
    assert match("Bear Claw", "Baer Claw")[0] == "fuzzy"
    assert match("White Lightning", "White Lihgtning")[0] == "fuzzy"


# ---------------------------------------------------------------- attributes

def test_hardest_difficulty_wins():
    tags, conflicts = mf.merge_tags([({"piste:difficulty": "easy"}, 500), ({"piste:difficulty": "Advanced"}, 10),
                                     ({"piste:difficulty": "intermediate"}, 200)])
    assert tags["piste:difficulty"] == "advanced"
    assert conflicts["piste:difficulty"]["chosen"] == "advanced"


@pytest.mark.parametrize("raw, norm", [("Easy (green circle)", "easy"), ("difficult", "advanced"),
                                       ("hard", "advanced"), ("EXPERT", "expert"), ("bogus", None)])
def test_normalize_difficulty(raw, norm):
    assert mf.normalize_difficulty(raw) == norm


def test_multi_value_and_yes_rules():
    tags, conflicts = mf.merge_tags([
        ({"piste:type": "nordic", "piste:grooming": "classic+skating", "gladed": "no", "lit": "no"}, 100),
        ({"piste:type": "nordic;hike", "piste:grooming": "skating;classic", "gladed": "yes"}, 50),
    ])
    assert tags["piste:type"] == "nordic;hike"
    assert "piste:grooming" not in conflicts                 # same values, written differently
    assert tags["gladed"] == "yes"
    assert tags["lit"] == "no"                               # only one piece has it: no conflict
    assert set(conflicts) == {"piste:type", "gladed"}


def test_other_conflicts_go_to_longest_piece_and_names_to_most_length():
    tags, conflicts = mf.merge_tags([({"name": "Winisook", "width": "10"}, 100),
                                     ({"name": "Winnisook", "width": "20"}, 80),
                                     ({"name": "Winnisook", "width": "30"}, 70)])
    assert tags["name"] == "Winnisook"                       # 150 m vs 100 m
    assert tags["width"] == "10"                             # longest single piece
    assert "name" not in conflicts and conflicts["width"]["values"] == ["10", "20", "30"]


def test_other_tags_round_trip():
    text = "{'description': \"Russ's run\\nline two\", 'ski': 'yes'}"
    tags = mf.parse_other_tags(text)
    assert tags == {"description": "Russ's run\nline two", "ski": "yes"}
    assert mf.parse_other_tags(mf.format_other_tags(tags)) == tags


# ---------------------------------------------------------------- kinds

def test_area_kinds_group_woods_and_grass():
    assert mf.area_kind({"natural": "wood"}).family == mf.area_kind({"landuse": "forest"}).family == "woodland"
    assert mf.area_kind({"landuse": "meadow"}).family == mf.area_kind({"natural": "grassland"}).family == "grassland"
    assert mf.area_kind({"landuse": "grass"}).family != mf.area_kind({"natural": "sand"}).family
    assert mf.area_kind({"piste:type": "downhill", "landuse": "forest"}).family == "piste"


def test_piste_compatibility():
    nordic, nordic_hike = mf.line_kind({"piste:type": "nordic"}), mf.line_kind({"piste:type": "nordic;hike"})
    assert mf.kinds_compatible(nordic, nordic_hike)
    assert not mf.kinds_compatible(nordic, mf.line_kind({"piste:type": "downhill"}))
    assert not mf.kinds_compatible(mf.line_kind({"piste:type": "hike"}), mf.line_kind({"piste:type": "snowshoe"}))
    assert mf.kinds_compatible(mf.line_kind({"waterway": "stream"}), mf.line_kind({"waterway": "river"}))
    assert not mf.kinds_compatible(mf.line_kind({"aerialway": "chair_lift"}), mf.line_kind({"aerialway": "station"}))


def test_relation_accepts():
    route = mf.relation_kind({"type": "route", "route": "piste", "piste:type": "downhill"})
    assert mf.relation_accepts(route, mf.line_kind({"piste:type": "downhill"}))
    assert not mf.relation_accepts(route, mf.line_kind({"piste:type": "nordic"}))
    river = mf.relation_kind({"type": "waterway", "waterway": "river"})
    assert mf.relation_accepts(river, mf.line_kind({"waterway": "stream"}))
    assert not mf.relation_accepts(river, mf.line_kind({"piste:type": "nordic"}))


# ---------------------------------------------------------------- linking (coordinates in metres)

# Test geometry is drawn in metres east (x) and north (y) of a point in Colorado, then
# turned into longitude/latitude, as the script reads it, through an azimuthal equidistant
# projection centred there, which keeps distances this short true.
FROM_LOCAL = pyproj.Transformer.from_crs("+proj=aeqd +lat_0=39.6 +lon_0=-106 +datum=WGS84", 4326,
                                         always_xy=True)


def lonlat(coords):
    """Turn (x, y) coordinates in metres around the test point into an EPSG:4326 line."""
    x, y = np.asarray(coords, float).T
    return shapely.LineString(np.column_stack(FROM_LOCAL.transform(x, y)))


def metric(coords):
    """Turn (x, y) coordinates in metres around the test point into a METRIC_CRS line."""
    return mf.to_metric([lonlat(coords)])[0]


def links_for(lines, names, tags):
    """Run find_line_links on a few test lines.

    Args:
        lines: One list of (x, y) coordinates per line, in metres around the test point.
        names: The name of each line, or None for an unnamed line.
        tags: The OSM tags of each line, which decide its kind.

    Returns:
        A (links, geom) tuple: the Links found, and the lines as an array of EPSG:4326
        LineStrings.
    """
    geom = np.array([lonlat(c) for c in lines], dtype=object)
    kinds = [mf.line_kind(t) for t in tags]
    keys = [mf.name_key(n) for n in names]
    links = mf.find_line_links(geom, mf.to_metric(geom), kinds, keys, mf.ground_length(geom), collect_near=True)
    return links, geom


# Tags of a downhill run, the kind most tests use.
DOWNHILL = {"piste:type": "downhill"}


def edges(links):
    """Return the links as a sorted list of (a, b) node pairs."""
    return sorted(zip(links.a.tolist(), links.b.tolist()))


def test_chain_merges_into_one_line():
    links, geom = links_for([[(0, 0), (100, 0)], [(100, 0), (200, 0)]], ["Lynx", "Lynx"], [DOWNHILL] * 2)
    assert edges(links) == [(0, 1)] and not links.connectors
    assert mf.merge_lines(list(geom)).geom_type == "LineString"


def test_fork_becomes_multilinestring():
    """A run that forks at x=100: all three pieces link, into two lines."""
    links, geom = links_for([[(0, 0), (100, 0)], [(100, 0), (200, 50)], [(100, 0), (200, -50)]],
                            ["Pika"] * 3, [DOWNHILL] * 3)
    assert edges(links) == [(0, 1), (0, 2), (1, 2)]
    assert mf.merge_lines(list(geom)).geom_type == "MultiLineString"


def test_small_gap_gets_a_connector():
    """A 10 m gap (under LINE_GAP_M) is closed by a 10 m connector."""
    links, geom = links_for([[(0, 0), (100, 0)], [(110, 0), (200, 0)]], ["Tempest"] * 2, [DOWNHILL] * 2)
    assert edges(links) == [(0, 1)]
    assert len(links.connectors) == 1
    assert mf.ground_length([links.connectors[0][1]])[0] == pytest.approx(10, abs=0.01)
    merged = mf.merge_lines(list(geom) + [c for _, c in links.connectors])
    assert merged.geom_type == "LineString" and mf.ground_length([merged])[0] == pytest.approx(200, abs=0.01)


def test_connector_takes_the_shortest_way_on_the_ground():
    """A connector to the middle of a diagonal line is as long as the gap: 10 m.

    Drawn as the shortest line in longitude/latitude, which bends angles, it would be 10.3 m.
    """
    s = 10 / np.sqrt(2)
    lines = [[(0, 0), (200, 200)], [(100 - s, 100 + s), (100 - 10 * s, 100 + 10 * s)]]
    links, geom = links_for(lines, ["Bowl"] * 2, [DOWNHILL] * 2)
    assert edges(links) == [(0, 1)] and len(links.connectors) == 1
    connector = links.connectors[0][1]
    assert mf.ground_length([connector])[0] == pytest.approx(10, abs=0.01)
    assert shapely.distance(shapely.get_point(connector, -1), geom[0]) < 1e-9


def test_gap_too_wide_does_not_link():
    """A 20 m gap is more than LINE_GAP_M, so the two pieces stay apart."""
    links, _ = links_for([[(0, 0), (100, 0)], [(120, 0), (200, 0)]], ["Tempest"] * 2, [DOWNHILL] * 2)
    assert edges(links) == []


def test_gaps_are_measured_on_the_ground():
    """LINE_GAP_M is 15 m on the ground whichever way the gap runs.

    In Colorado the projection used before (ESRI:102008) measured east-west gaps 6% short
    and north-south gaps 6% long, so the 15.5 m gap linked and the 14.5 m gap didn't.
    """
    east, _ = links_for([[(0, 0), (100, 0)], [(115.5, 0), (200, 0)]], ["Tempest"] * 2, [DOWNHILL] * 2)
    north, _ = links_for([[(0, 0), (0, 100)], [(0, 114.5), (0, 200)]], ["Tempest"] * 2, [DOWNHILL] * 2)
    assert edges(east) == []
    assert edges(north) == [(0, 1)] and north.dist[0] == pytest.approx(14.5, abs=0.01)


def test_polygon_gaps_are_measured_on_the_ground():
    """POLY_GAP_M is 5 m on the ground: woods 5.3 m apart stay apart, 4.8 m apart link.

    The projection used before got both wrong in Colorado, as in the test above.
    """
    def woods(dx, dy):
        """Link two 100 m squares of wood, the second one moved by (dx, dy) metres."""
        square = np.array([(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)], float)
        geom = np.array([shapely.Polygon(lonlat(c).coords) for c in (square, square + (dx, dy))], dtype=object)
        kinds = [mf.area_kind({"natural": "wood"})] * 2
        keys = [mf.name_key("Stevens Memorial Forest")] * 2
        return edges(mf.find_area_links(geom, mf.to_metric(geom), kinds, keys))

    assert woods(105.3, 0) == []
    assert woods(0, 104.8) == [(0, 1)]


def test_stretch_is_largest_at_the_southern_or_northern_edge():
    """stretch relies on the scale factor falling and then rising from south to north."""
    lat = np.arange(20, 85, 0.05)
    scale = mf.stretch(shapely.points(np.full_like(lat, -106), lat))
    low = scale.argmin()
    assert (np.diff(scale[:low + 1]) < 0).all() and (np.diff(scale[low:]) > 0).all()
    line = shapely.LineString([(-106, 30), (-106, 50)])     # crosses the lowest point
    assert mf.stretch([line])[0] == pytest.approx(scale[(lat >= 30) & (lat <= 50)].max())


def test_line_drawn_twice_is_a_copy():
    """A slightly shorter line 1 m beside the first is a copy of it."""
    links, _ = links_for([[(0, 0), (100, 0)], [(0, 1), (98, 1)]], ["Pinnacles"] * 2, [DOWNHILL] * 2)
    assert links.copy_of == {1: 0}


def test_twin_lifts_stay_apart():
    """Two same-length rope tows 8 m apart are two lifts, not one."""
    tow = {"aerialway": "rope_tow"}
    links, _ = links_for([[(0, 0), (150, 0)], [(0, 8), (150, 8)]], ["Dual Tow"] * 2, [tow] * 2)
    assert edges(links) == []
    assert [n.reason for n in links.near] == ["twin lifts"]


def test_twin_lifts_drawn_in_opposite_directions_stay_apart():
    """Like the Sandia Peak Tramway: two cables 10-16 m apart, drawn in opposite directions.

    Only one pair of ends is within LINE_GAP_M, so the two used to join into one U-shaped lift.
    """
    tram = {"aerialway": "cable_car"}
    links, _ = links_for([[(0, 0), (1000, 0)], [(1000, -16), (0, -10)]], ["Peak Tramway"] * 2, [tram] * 2)
    assert edges(links) == []
    assert [n.reason for n in links.near] == ["twin lifts"]


def test_runs_alongside():
    """Side-by-side lines run alongside; sections in a row and a short spur don't."""
    line = metric([(0, 0), (1000, 0)])
    assert mf.runs_alongside(line, metric([(1000, 9), (0, 9)])) == pytest.approx(9, abs=0.01)
    assert mf.runs_alongside(line, metric([(1000, 0), (1700, 300)])) is None
    assert mf.runs_alongside(line, metric([(500, 0), (500, 400)])) is None


def test_lift_sections_join_end_to_end_but_not_mid_way():
    """Gondola sections meeting end to end link; chairs touching mid-way don't."""
    gondola = {"aerialway": "gondola"}
    links, _ = links_for([[(0, 0), (1000, 0)], [(1000, 0), (1700, 300)]], ["Sunshine Gondola"] * 2, [gondola] * 2)
    assert edges(links) == [(0, 1)]
    chair = {"aerialway": "chair_lift"}
    links, _ = links_for([[(0, 0), (500, 0)], [(250, 0), (250, 400)]], ["Chair 7"] * 2, [chair] * 2)
    assert edges(links) == [] and [n.reason for n in links.near] == ["lifts touch mid-way"]


def test_lift_does_not_merge_with_its_station():
    links, _ = links_for([[(0, 0), (700, 0)], [(-30, 0), (0, 0)]], ["Chair 7"] * 2,
                         [{"aerialway": "chair_lift"}, {"aerialway": "station"}])
    assert edges(links) == [] and [n.reason for n in links.near] == ["different kind"]


def test_upper_and_lower_runs_stay_apart():
    links, _ = links_for([[(0, 0), (100, 0)], [(100, 0), (200, 0)]],
                         ["Upper White Lightning", "Lower White Lightning"], [DOWNHILL] * 2)
    assert edges(links) == [] and [n.reason for n in links.near] == ["qualifier differs"]


def test_unnamed_lines_never_link():
    links, _ = links_for([[(0, 0), (100, 0)], [(100, 0), (200, 0)]], [None, None], [DOWNHILL] * 2)
    assert edges(links) == []
