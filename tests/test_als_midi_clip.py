"""Embedding a Session-view MidiClip (real note data) into a track's first
empty clip slot. See PLAN.md "MIDI clip embedding" -- template cloned from
a real Ableton-exported MidiClip block (Ableton's own "Drums Lesson.als"
factory content), not hand-written from scratch.

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import xml.etree.ElementTree as ET
from collections import Counter

import pytest

from rpp_to_als.als_builder import MAX_MIDI_CLIPS_PER_TRACK, TrackSpec, build_liveset_xml
from rpp_to_als.model import MidiNote

from conftest import requires_ableton

_NOTES = [
    MidiNote(position=0.0, pitch=36, velocity=100, length=0.25),
    MidiNote(position=0.5, pitch=36, velocity=90, length=0.25),
    MidiNote(position=0.0, pitch=38, velocity=110, length=0.5),
]


@requires_ableton
def test_embeds_one_midi_clip_with_all_notes():
    tracks = [TrackSpec("MIDI", "Kit", midi_notes=_NOTES, midi_clip_length_beats=4.0), TrackSpec("Audio", "Vocals")]
    xml = build_liveset_xml(tracks, tempo=120.0)
    assert "{{" not in xml  # no leftover template placeholders

    root = ET.fromstring(xml.encode("utf-8"))
    clips = root.findall(".//MidiClip")
    assert len(clips) == 1

    events = root.findall(".//MidiNoteEvent")
    assert len(events) == len(_NOTES)
    assert {e.get("Time") for e in events} == {"0", "0.5"}
    assert {e.get("Velocity") for e in events} == {"100", "90", "110"}


@requires_ableton
def test_key_track_ids_unique_within_clip():
    """Regression: _NOTES spans 2 pitches (36, 38) -> 2 sibling KeyTrack
    elements under one clip's KeyTracks list. Real Ableton rejects a file
    with sibling list members sharing the same Id ("Non-unique list ids"
    -- this is a DIFFERENT rule from the Id="0" null-pointee sentinel
    exemption; see PLAN.md "Non-unique list ids"). Found the hard way: a
    real reload of a generated .als failed with exactly this error even
    after the Pointee/LOM Id fix, because every KeyTrack was hardcoded
    Id="0" regardless of how many pitches were in the clip."""
    tracks = [TrackSpec("MIDI", "Kit", midi_notes=_NOTES, midi_clip_length_beats=4.0)]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    key_tracks = root.findall(".//MidiClip/Notes/KeyTracks/KeyTrack")
    assert len(key_tracks) == 2  # pitches 36 and 38
    ids = [kt.get("Id") for kt in key_tracks]
    assert len(set(ids)) == len(ids)


@requires_ableton
def test_midi_note_ids_globally_unique_and_next_id_generator_correct():
    """`NoteId` is its own globally-unique-across-the-document id family
    (community Note-ID-collision fixers treat it this way -- see PLAN.md
    "Pointee/LOM Id uniqueness"), separate from the Pointee/AutomationTarget
    family. `NoteIdGenerator/NextId` is the clip-local analog of the
    LiveSet-root NextPointeeId and must exceed every NoteId used in that
    clip -- same invariant, same failure mode if wrong."""
    tracks = [
        TrackSpec("MIDI", "A", midi_notes=_NOTES),
        TrackSpec("MIDI", "B", midi_notes=_NOTES),  # same note data, different track -- offsets must differ
        TrackSpec("Audio", "C"),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    all_note_ids = [e.get("NoteId") for e in root.findall(".//MidiNoteEvent")]
    counts = Counter(all_note_ids)
    duplicates = {k: v for k, v in counts.items() if v > 1}
    assert duplicates == {}

    for clip in root.findall(".//MidiClip"):
        clip_note_ids = {int(e.get("NoteId")) for e in clip.findall(".//MidiNoteEvent")}
        next_id = int(clip.find(".//NoteIdGenerator/NextId").get("Value"))
        assert next_id > max(clip_note_ids)


@requires_ableton
def test_no_notes_leaves_clip_slot_empty():
    tracks = [TrackSpec("MIDI", "Empty"), TrackSpec("Audio", "Vocals")]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))
    assert root.findall(".//MidiClip") == []


_OTHER_NOTES = [MidiNote(position=1.0, pitch=40, velocity=80, length=1.0)]


@requires_ableton
def test_multiple_midi_items_fill_separate_scene_slots():
    """A REAPER track can have more than one MIDI item -- each gets its own
    Session-view scene slot (up to MAX_MIDI_CLIPS_PER_TRACK), rather than
    only the first being kept. See PLAN.md "Multiple MIDI items per
    track"."""
    tracks = [
        TrackSpec(
            "MIDI",
            "Kit",
            midi_notes=_NOTES,
            midi_clip_length_beats=4.0,
            extra_midi_clips=[(_OTHER_NOTES, 2.0)],
        ),
        TrackSpec("Audio", "Vocals"),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    clips = root.findall(".//MidiClip")
    assert len(clips) == 2
    note_counts = sorted(len(c.findall(".//MidiNoteEvent")) for c in clips)
    assert note_counts == [1, len(_NOTES)]

    # both clips' NoteIds must still be globally unique against each other
    all_note_ids = [e.get("NoteId") for e in root.findall(".//MidiNoteEvent")]
    assert len(set(all_note_ids)) == len(all_note_ids)


@requires_ableton
def test_too_many_midi_clips_raises():
    too_many = [(_OTHER_NOTES, 2.0) for _ in range(MAX_MIDI_CLIPS_PER_TRACK)]  # + midi_notes = one over the cap
    tracks = [TrackSpec("MIDI", "Kit", midi_notes=_NOTES, extra_midi_clips=too_many)]
    with pytest.raises(ValueError):
        build_liveset_xml(tracks, tempo=120.0)
