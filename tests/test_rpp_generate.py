"""Reverse direction: .als -> .rpp (als_parse + rpp_generate). See PLAN.md
".als -> .rpp export". Pure-model tests need nothing on disk; the
round-trip test at the bottom builds a real Live Set with als_builder and
so needs Ableton's default templates."""

import os

import pytest

from rpp_to_als.als_parse import AlsClip, AlsProject, AlsTrack, ClipLoop, decode_time_signature, parse_als
from rpp_to_als.model import MidiNote
from rpp_to_als.rpp_generate import audio_clip_items, convert, midi_clip_items, natural_length
from rpp_to_als.rpp_parse import (
    audio_item_file_path,
    find_items,
    item_length_seconds,
    item_playrate,
    item_position_seconds,
    item_source_offset_seconds,
    parse_midi_item_notes,
    parse_tracks,
    project_markers,
    project_tempo,
    project_time_signature,
)

from conftest import requires_ableton


def _midi(notes, loop_start=0.0, loop_end=4.0, on=False, start_relative=0.0, time=0.0, length=4.0):
    return AlsClip(
        kind="MIDI",
        name="c",
        loop=ClipLoop(loop_start, loop_end, start_relative, on),
        time=time,
        length=length,
        notes=[MidiNote(p, pitch, 100, d) for p, pitch, d in notes],
    )


def _audio(loop_start, loop_end, on=False, warped=False, warp_markers=(), path="x.wav"):
    return AlsClip(
        kind="AUDIO",
        name="a",
        loop=ClipLoop(loop_start, loop_end, 0.0, on),
        sample_path=path,
        warped=warped,
        warp_markers=list(warp_markers),
    )


@pytest.mark.parametrize("value, expected", [(201, (4, 4)), (200, (3, 4)), (301, (5, 8)), (0, (1, 1))])
def test_decode_time_signature_inverts_encoder(value, expected):
    assert decode_time_signature(value) == expected


def test_unlooped_midi_clip_uses_loop_start_as_start_marker():
    clip = _midi([(1.0, 60, 0.5), (2.5, 62, 1.0)], loop_start=1.0, loop_end=3.0)
    (item,) = midi_clip_items(clip, position=8.0, total=2.0)
    assert (item.position, item.length) == (8.0, 2.0)
    assert [(n.position, n.pitch, n.length) for n in item.notes] == [(0.0, 60, 0.5), (1.5, 62, 0.5)]


def test_looped_midi_clip_is_unrolled_and_notes_truncated_at_loop_end():
    clip = _midi([(0.0, 60, 0.5), (1.5, 64, 2.0)], loop_start=0.0, loop_end=2.0, on=True)
    (item,) = midi_clip_items(clip, position=0.0, total=5.0)
    assert [(n.position, n.pitch, n.length) for n in item.notes] == [
        (0.0, 60, 0.5),
        (1.5, 64, 0.5),
        (2.0, 60, 0.5),
        (3.5, 64, 0.5),
        (4.0, 60, 0.5),
    ]


def test_looped_midi_clip_honours_start_relative():
    clip = _midi([(0.0, 60, 0.5), (1.0, 62, 0.5)], loop_start=0.0, loop_end=2.0, on=True, start_relative=1.0)
    (item,) = midi_clip_items(clip, position=0.0, total=2.0)
    assert [(n.position, n.pitch) for n in item.notes] == [(0.0, 62), (1.0, 60)]


def test_unwarped_audio_loop_values_are_seconds():
    # 120bpm: 1 beat = 0.5s. Start marker 3s into the file.
    clip = _audio(3.0, 10.0)
    (item,) = audio_clip_items(clip, position=4.0, total=2.0, tempo=120.0)
    assert (item.position, item.length, item.source_offset) == (4.0, 2.0, 3.0)
    assert item.playrate == pytest.approx(1.0)


def test_looped_audio_becomes_one_item_per_cycle():
    clip = _audio(1.0, 2.0, on=True)  # 1s loop = 2 beats at 120bpm
    items = audio_clip_items(clip, position=0.0, total=5.0, tempo=120.0)
    assert [(i.position, i.length, i.source_offset) for i in items] == [
        (0.0, 2.0, 1.0),
        (2.0, 2.0, 1.0),
        (4.0, 1.0, 1.0),
    ]


def test_audio_start_before_file_is_trimmed_not_negative_soffs():
    clip = _audio(-1.0, 3.0)  # 1s (2 beats) of leading silence
    (item,) = audio_clip_items(clip, position=0.0, total=8.0, tempo=120.0)
    assert (item.position, item.length, item.source_offset) == (pytest.approx(2.0), pytest.approx(6.0), 0.0)


def test_warped_audio_playrate_from_warp_markers():
    # File plays 1s of audio per beat; at 120bpm a beat is 0.5s -> 2x speed.
    clip = _audio(0.0, 4.0, warped=True, warp_markers=[(0.0, 0.0), (1.0, 1.0)])
    (item,) = audio_clip_items(clip, position=0.0, total=4.0, tempo=120.0)
    assert item.playrate == pytest.approx(2.0)
    assert item.source_offset == pytest.approx(0.0)


def test_natural_length_unwarped_audio_converts_seconds_to_beats():
    assert natural_length(_audio(0.0, 3.0), tempo=120.0) == pytest.approx(6.0)


def _project(**kw):
    return AlsProject(tempo=120.0, **kw)


def test_group_tracks_become_reaper_folders():
    tracks = [
        AlsTrack("GROUP", "Bus", 10, -1),
        AlsTrack("MIDI", "A", 11, 10),
        AlsTrack("GROUP", "Inner", 12, 10),
        AlsTrack("AUDIO", "B", 13, 12),
        AlsTrack("MIDI", "C", 14, -1),
    ]
    text = convert(_project(tracks=tracks)).text
    isbus = [line.strip() for line in text.splitlines() if "ISBUS" in line]
    assert isbus == ["ISBUS 1 1", "ISBUS 0 0", "ISBUS 1 1", "ISBUS 2 -2", "ISBUS 0 0"]


def test_return_tracks_skipped_with_warning():
    result = convert(_project(tracks=[AlsTrack("MIDI", "A", 1, -1), AlsTrack("RETURN", "Rev", 2, -1)]))
    assert result.track_count == 1
    assert any("Rev" in w for w in result.warnings)


def test_session_clips_ignored_without_scenes_flag():
    track = AlsTrack("MIDI", "A", 1, -1, session_clips={0: _midi([(0, 60, 1)])})
    result = convert(_project(tracks=[track]))
    assert result.item_count == 0
    assert any("--scenes" in w for w in result.warnings)


def test_scenes_laid_out_after_arrangement_as_marker_regions():
    arr = _midi([(0, 60, 1)], time=0.0, length=6.0)  # ends mid bar 2
    track_a = AlsTrack(
        "MIDI",
        "A",
        1,
        -1,
        arrangement_clips=[arr],
        session_clips={0: _midi([(0, 60, 1)], loop_end=4.0, on=True), 2: _midi([(0, 62, 1)], loop_end=2.0)},
    )
    track_b = AlsTrack("MIDI", "B", 2, -1, session_clips={0: _midi([(0, 64, 1)], loop_end=8.0)})
    project = _project(tracks=[track_a, track_b], scene_names=["Intro", "", "Drop"], locators=[(0.0, "Start")])
    result = convert(project, session_scenes=True)
    assert result.scene_count == 2  # empty scene 1 skipped
    # Arrangement ends at beat 6 -> next bar is 8, plus one bar gap -> 12.
    # Intro region = 8 beats (B's clip) -> Drop at 20. 120bpm: beat = 0.5s.
    assert project_markers(result.text) == [(0.0, "Start"), (6.0, "Intro"), (10.0, "Drop")]
    tracks = parse_tracks(result.text)
    a_items = find_items(tracks[0].chunk)
    # A: arrangement clip, looped Intro clip filling the 8-beat region, unlooped Drop clip.
    assert [(item_position_seconds(i), item_length_seconds(i)) for i in a_items] == [(0, 3), (6, 4), (10, 1)]
    assert len(parse_midi_item_notes(a_items[1])) == 2  # 4-beat loop x2


def test_marker_and_track_names_with_quotes_round_trip():
    result = convert(_project(tracks=[AlsTrack("MIDI", 'say "hi"', 1, -1)], locators=[(0.0, 'a "b"')]))
    assert project_markers(result.text) == [(0.0, 'a "b"')]
    assert parse_tracks(result.text)[0].name == 'say "hi"'


@requires_ableton
def test_round_trip_through_real_live_set(tmp_path):
    from rpp_to_als.als_builder import TrackSpec, build_liveset_xml, write_als

    notes = [MidiNote(0.0, 60, 100, 1.0), MidiNote(2.0, 67, 80, 0.5)]
    tracks = [TrackSpec("MIDI", "Keys", arrangement_midi_clips=[(notes, 8.0, 4.0)], volume=0.5, pan=-0.25)]
    xml = build_liveset_xml(tracks, tempo=100.0, time_signature=(3, 4), locators=[(0.0, "A"), (12.0, "B")])
    als = os.path.join(tmp_path, "rt.als")
    write_als(xml, als)

    project = parse_als(als)
    assert project.tempo == pytest.approx(100.0)
    assert project.time_signature == (3, 4)
    text = convert(project).text
    assert project_tempo(text) == pytest.approx(100.0)
    assert project_time_signature(text) == (3, 4)
    assert project_markers(text) == [(0.0, "A"), (pytest.approx(7.2), "B")]
    (track,) = parse_tracks(text)
    assert track.name == "Keys"
    (item,) = find_items(track.chunk)
    assert item_position_seconds(item) == pytest.approx(4.8)  # beat 8 at 100bpm
    got = [(n.position, n.pitch, n.velocity, n.length) for n in parse_midi_item_notes(item)]
    assert got == [(0.0, 60, 100, 1.0), (2.0, 67, 80, 0.5)]
    assert "VOLPAN 0.5 -0.25" in track.chunk
