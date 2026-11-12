"""Tests for merge_features.py: name matching, attribute rules and line linking.

Run with:  python -m pytest osm_data/tests
"""

import sys
from pathlib import Path

import numpy as np
import pytest
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import merge_features as mf  # noqa: E402


# ---------------------------------------------------------------- names

def match(a, b):
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


def test_osa_distance():
    assert mf.osa_distance("abcd", "abdc", 3) == 1          # adjacent swap counts once
    assert mf.osa_distance("kitten", "sitting", 5) == 3
    assert mf.osa_distance("a", "abcdef", 2) == 3            # capped at limit + 1


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

def links_for(lines, names, tags):
    geom = np.array([shapely.LineString(c) for c in lines], dtype=object)
    kinds = [mf.line_kind(t) for t in tags]
    keys = [mf.name_key(n) for n in names]
    return mf.find_line_links(geom, geom, kinds, keys, shapely.length(geom), collect_near=True), geom


DOWNHILL = {"piste:type": "downhill"}


def edges(links):
    return sorted(zip(links.a.tolist(), links.b.tolist()))


def test_chain_merges_into_one_line():
    links, geom = links_for([[(0, 0), (100, 0)], [(100, 0), (200, 0)]], ["Lynx", "Lynx"], [DOWNHILL] * 2)
    assert edges(links) == [(0, 1)] and not links.connectors
    assert mf.merge_lines(list(geom)).geom_type == "LineString"


def test_fork_becomes_multilinestring():
    links, geom = links_for([[(0, 0), (100, 0)], [(100, 0), (200, 50)], [(100, 0), (200, -50)]],
                            ["Pika"] * 3, [DOWNHILL] * 3)
    assert edges(links) == [(0, 1), (0, 2), (1, 2)]
    assert mf.merge_lines(list(geom)).geom_type == "MultiLineString"


def test_small_gap_gets_a_connector():
    links, geom = links_for([[(0, 0), (100, 0)], [(110, 0), (200, 0)]], ["Tempest"] * 2, [DOWNHILL] * 2)
    assert edges(links) == [(0, 1)]
    assert len(links.connectors) == 1 and links.connectors[0][1].length == pytest.approx(10)
    merged = mf.merge_lines(list(geom) + [c for _, c in links.connectors])
    assert merged.geom_type == "LineString" and merged.length == pytest.approx(200)


def test_gap_too_wide_does_not_link():
    links, _ = links_for([[(0, 0), (100, 0)], [(120, 0), (200, 0)]], ["Tempest"] * 2, [DOWNHILL] * 2)
    assert edges(links) == []


def test_line_drawn_twice_is_a_copy():
    links, _ = links_for([[(0, 0), (100, 0)], [(0, 1), (98, 1)]], ["Pinnacles"] * 2, [DOWNHILL] * 2)
    assert links.copy_of == {1: 0}


def test_twin_lifts_stay_apart():
    tow = {"aerialway": "rope_tow"}
    links, _ = links_for([[(0, 0), (150, 0)], [(0, 8), (150, 8)]], ["Dual Tow"] * 2, [tow] * 2)
    assert edges(links) == []
    assert [n.reason for n in links.near] == ["twin lifts"]


def test_lift_sections_join_end_to_end_but_not_mid_way():
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
