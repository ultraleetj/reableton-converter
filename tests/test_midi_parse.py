"""Requires the real chirriado.RPP; skips if not present on disk."""

import os

import pytest

from rpp_to_als.rpp_parse import (
    find_items,
    is_midi_item,
    item_loop_enabled,
    item_playrate,
    item_source_offset_beats,
    item_volume,
    parse_midi_item_notes,
    parse_tracks,
)

SAMPLE_RPP = r"D:\REAPER Media\projs\jotas\chirriado\chirriado.RPP"


@pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")
def test_extracts_plausible_drum_notes():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    kit_track = parse_tracks(text)[0]
    items = [i for i in find_items(kit_track.chunk) if is_midi_item(i)]
    assert len(items) > 0

    notes = parse_midi_item_notes(items[0])
    assert len(notes) > 0
    for n in notes:
        assert 0 <= n.pitch <= 127
        assert 0 <= n.velocity <= 127
        assert n.position >= 0
        assert n.length > 0
    # kick (36) and closed hihat (42) are the densest voices in this groove
    pitches = {n.pitch for n in notes}
    assert 36 in pitches
    assert 42 in pitches


@pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")
def test_note_positions_are_bounded_not_runaway_accumulation():
    # notes are appended in note-OFF order, not note-ON order, so position
    # isn't strictly sorted -- this just guards against a delta-tick
    # accumulation bug producing nonsense (negative or huge positions).
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    kit_track = parse_tracks(text)[0]
    items = [i for i in find_items(kit_track.chunk) if is_midi_item(i)]
    notes = parse_midi_item_notes(items[0])
    assert all(n.position >= 0 for n in notes)
    assert max(n.position for n in notes) < 1000


def test_item_playrate_parses_first_field():
    # "PLAYRATE <rate> <pitch-preserve> <mode> ..." -- only the first field
    # matters for note-timing scaling; a real chirriado.RPP item line, see
    # PLAN.md "Item PLAYRATE + LOOP".
    chunk = "    <ITEM\n      PLAYRATE 0.5 1 0 -1 0 0.0025\n      LOOP 1\n"
    assert item_playrate(chunk) == 0.5


def test_item_playrate_defaults_to_one_when_absent():
    assert item_playrate("    <ITEM\n      POSITION 0\n") == 1.0


def test_item_loop_enabled():
    assert item_loop_enabled("    <ITEM\n      LOOP 1\n") is True
    assert item_loop_enabled("    <ITEM\n      LOOP 0\n") is False
    assert item_loop_enabled("    <ITEM\n      POSITION 0\n") is False


def test_item_volume_parses_first_volpan_field():
    # "VOLPAN <vol> <pan> <?> <?>" -- only the linear-gain first field is
    # used, matches Ableton's AudioClip SampleVolume convention directly.
    # A real chirriado.RPP item line.
    chunk = "    <ITEM\n      VOLPAN 0.707946 0 1 -1\n"
    assert item_volume(chunk) == 0.707946


def test_item_volume_defaults_to_unity_when_absent():
    assert item_volume("    <ITEM\n      POSITION 0\n") == 1.0


def test_item_source_offset_beats_parses_second_soffs_field():
    # "SOFFS <seconds> <beats>" -- only the beats/QN field matters here,
    # it's already in the same raw tick/ppq coordinate space as note
    # positions. A real chirriado.RPP item line, see PLAN.md "Item SOFFS
    # (source offset / pre-roll)".
    chunk = "    <ITEM\n      SOFFS 1.05576347305389 2.93854166666665\n"
    assert item_source_offset_beats(chunk) == pytest.approx(2.93854166666665)


def test_item_source_offset_beats_defaults_to_zero_when_absent():
    assert item_source_offset_beats("    <ITEM\n      POSITION 0\n") == 0.0


@pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")
def test_real_playrate_item_detected_on_swing_th1():
    # chirriado.RPP's first track's first item is a real PLAYRATE 0.5,
    # LOOP 1 item -- the exact case that motivated this parsing (a REAPER
    # item time-stretched to half speed with its short source pattern
    # looped to fill a longer displayed length). See PLAN.md "Item
    # PLAYRATE + LOOP".
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    kit_track = parse_tracks(text)[0]
    items = [i for i in find_items(kit_track.chunk) if is_midi_item(i)]
    assert item_playrate(items[0]) == 0.5
    assert item_loop_enabled(items[0]) is True
