"""Embedding MIDI clips into a track's Arrangement view, each at its own
real project-timeline position -- unlike Session-view clip slots, there's
no fixed capacity here. Structure (ClipTimeable/ArrangerAutomation/Events,
and the clip's own Time/CurrentStart absolute-position attributes)
confirmed against a real Ableton project with genuine Arrangement clips:
Ableton's own "Chuck Sutton - Patience" demo song (Help menu content,
ships in Resources\\Core Library\\Lessons\\Demo Songs). See PLAN.md
"Arrangement-view clip placement" -- an earlier version of this code
wrongly targeted `<TakeLanes>` (the take-comping overlay for alternate
takes, not the real Arrangement clip list); that produced structurally
valid, reload-clean files where the clips were nonetheless invisible in
the Arrangement, only caught by an actual visual check in real Ableton.

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import xml.etree.ElementTree as ET
from collections import Counter

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml
from rpp_to_als.model import MidiNote

from conftest import requires_ableton

_NOTES_A = [MidiNote(position=0.0, pitch=36, velocity=100, length=0.25)]
_NOTES_B = [MidiNote(position=0.0, pitch=38, velocity=90, length=0.5)]


@requires_ableton
def test_embeds_clips_at_correct_absolute_positions():
    tracks = [
        TrackSpec("MIDI", "Kit", arrangement_midi_clips=[(_NOTES_A, 0.0, 4.0), (_NOTES_B, 16.0, 4.0)]),
        TrackSpec("Audio", "Vocals"),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    assert "{{" not in xml

    root = ET.fromstring(xml.encode("utf-8"))
    clips = root.findall(".//MidiClip")
    assert len(clips) == 2

    positions = sorted(float(c.get("Time")) for c in clips)
    assert positions == [0.0, 16.0]
    for clip in clips:
        start = float(clip.get("Time"))
        assert float(clip.find("CurrentStart").get("Value")) == start
        assert float(clip.find("CurrentEnd").get("Value")) == start + 4.0


@requires_ableton
def test_no_fixed_capacity_many_clips_on_one_track():
    """Unlike Session-view scene slots (MAX_MIDI_CLIPS_PER_TRACK), the
    Arrangement take lane's <Events> list can hold arbitrarily many clips
    -- this is what actually fixes the earlier "only using the first N
    MIDI items" data-loss gap."""
    clips_in = [([MidiNote(position=0.0, pitch=36 + i, velocity=100, length=0.25)], float(i * 4), 4.0) for i in range(10)]
    tracks = [TrackSpec("MIDI", "Busy", arrangement_midi_clips=clips_in)]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))
    assert len(root.findall(".//MidiClip")) == 10


@requires_ableton
def test_midi_clip_ids_unique_within_take_lane():
    """Regression: multiple MidiClips in one Arrangement clip list's
    <Events> list are direct XML siblings -- real Ableton rejects a file
    where sibling list members share the same Id ("Non-unique list ids",
    a different rule from the Id="0" null-pointee sentinel exemption; see
    PLAN.md "Non-unique list ids"). Found the hard way: every clip built
    by build_midi_clip_xml was hardcoded Id="0" before this was fixed."""
    clips_in = [([MidiNote(position=0.0, pitch=36, velocity=100, length=0.25)], float(i * 4), 4.0) for i in range(5)]
    tracks = [TrackSpec("MIDI", "Busy", arrangement_midi_clips=clips_in)]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    events = root.find(".//ClipTimeable/ArrangerAutomation/Events")
    clips = events.findall("./MidiClip")
    assert len(clips) == 5
    ids = [c.get("Id") for c in clips]
    assert len(set(ids)) == len(ids)


@requires_ableton
def test_note_ids_globally_unique_across_arrangement_clips_and_tracks():
    tracks = [
        TrackSpec("MIDI", "A", arrangement_midi_clips=[(_NOTES_A, 0.0, 4.0), (_NOTES_B, 16.0, 4.0)]),
        TrackSpec("MIDI", "B", arrangement_midi_clips=[(_NOTES_A, 0.0, 4.0)]),
        TrackSpec("Audio", "C"),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    all_note_ids = [e.get("NoteId") for e in root.findall(".//MidiNoteEvent")]
    duplicates = {k: v for k, v in Counter(all_note_ids).items() if v > 1}
    assert duplicates == {}


@requires_ableton
def test_session_and_arrangement_clips_coexist_on_same_track():
    """A track can use both mechanisms at once without Id collisions --
    exercises both _CLIP_ID_SUB_OFFSET and _ARRANGEMENT_CLIP_ID_SUB_OFFSET
    zones together."""
    tracks = [
        TrackSpec(
            "MIDI",
            "Kit",
            midi_notes=_NOTES_A,
            midi_clip_length_beats=4.0,
            arrangement_midi_clips=[(_NOTES_B, 16.0, 4.0)],
        ),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))
    assert len(root.findall(".//MidiClip")) == 2

    all_note_ids = [e.get("NoteId") for e in root.findall(".//MidiNoteEvent")]
    assert len(set(all_note_ids)) == len(all_note_ids)


@requires_ableton
def test_no_arrangement_clips_leaves_take_lane_empty():
    tracks = [TrackSpec("MIDI", "Empty"), TrackSpec("Audio", "Vocals")]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))
    assert root.findall(".//MidiClip") == []


@requires_ableton
def test_arrangement_clips_go_in_arranger_automation_not_take_lane():
    """Regression: clips must land in ClipTimeable/ArrangerAutomation/
    Events (the track's real, always-visible Arrangement clip list), NOT
    in TakeLanes/TakeLanes/TakeLane (the take-comping overlay for
    alternate takes -- hidden by default). A prior version of this code
    populated TakeLanes instead; the file was well-formed and reload-clean
    but the clips were invisible in the Arrangement, only caught by an
    actual visual check in real Ableton. See PLAN.md "Arrangement-view
    clip placement"."""
    tracks = [TrackSpec("MIDI", "Kit", arrangement_midi_clips=[(_NOTES_A, 0.0, 4.0)])]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    assert root.find(".//ClipTimeable/ArrangerAutomation/Events/MidiClip") is not None
    assert root.find(".//TakeLanes//MidiClip") is None
