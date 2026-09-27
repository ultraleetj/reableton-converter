"""Track-level VOLPAN (mixer fader volume + pan) -- see PLAN.md "Track
volume/pan". Distinct from item_volume: the track's own VOLPAN line
always precedes any nested <ITEM> in the chunk, so a plain first-match
search reliably finds the track's own line, not an item's.
"""

import os

import pytest

from rpp_to_als.rpp_parse import parse_tracks, track_pan, track_volume

SAMPLE_RPP = r"D:\REAPER Media\projs\jotas\chirriado\chirriado.RPP"


def test_track_volume_parses_first_volpan_field():
    chunk = "  <TRACK {GUID}\n    NAME x\n    VOLPAN 0.70794578438414 -0.07 -1 -1 1\n    <ITEM\n      VOLPAN 2.0 0.5\n    >\n"
    assert track_volume(chunk) == 0.70794578438414


def test_track_pan_parses_second_volpan_field():
    chunk = "  <TRACK {GUID}\n    NAME x\n    VOLPAN 0.70794578438414 -0.07 -1 -1 1\n"
    assert track_pan(chunk) == -0.07


def test_track_volume_and_pan_default_when_absent():
    chunk = "  <TRACK {GUID}\n    NAME x\n"
    assert track_volume(chunk) == 1.0
    assert track_pan(chunk) == 0.0


def test_track_volpan_not_confused_with_nested_item_volpan():
    # A track's own line always precedes any nested <ITEM>'s -- an
    # item-level VOLPAN with a wildly different value must not leak into
    # the track-level reading.
    chunk = "  <TRACK {GUID}\n    VOLPAN 1.5 0.25\n    <ITEM\n      VOLPAN 0.01 -0.99\n    >\n"
    assert track_volume(chunk) == 1.5
    assert track_pan(chunk) == 0.25


@pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")
def test_real_project_track_volumes_are_plausible():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    tracks = {t.name: t for t in parse_tracks(text)}
    # Real values from chirriado.RPP's own VOLPAN lines, confirmed by
    # direct inspection.
    assert track_volume(tracks["tp"].chunk) == pytest.approx(0.70794578438414)
    assert track_pan(tracks["tp"].chunk) == pytest.approx(-0.07)
    assert track_volume(tracks["dark"].chunk) == pytest.approx(1.0)
