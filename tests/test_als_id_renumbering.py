"""Per-track Id offsetting: structural `Id="N"` attributes must not collide
across cloned track blocks in the same file (Id="0" excepted -- see
test_als_pointee_references.py for why).

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import re
from collections import defaultdict

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml

from conftest import requires_ableton, track_spans


@requires_ableton
def test_id_renumbering_never_collides_across_tracks():
    tracks = [TrackSpec("MIDI", "A"), TrackSpec("Audio", "B"), TrackSpec("MIDI", "C")]
    xml = build_liveset_xml(tracks, tempo=120.0)

    spans = track_spans(xml)
    id_owning_tracks = defaultdict(set)
    for m in re.finditer(r'Id="(-?\d+)"', xml):
        if m.group(1) == "0":
            continue  # null-pointee sentinel, intentionally shared across tracks
        pos = m.start()
        owner = next((i for i, (s, e) in enumerate(spans) if s <= pos <= e), None)
        if owner is not None:
            id_owning_tracks[m.group(1)].add(owner)

    cross_track = {k: v for k, v in id_owning_tracks.items() if len(v) > 1}
    assert cross_track == {}


@requires_ableton
def test_track_root_ids_unique_and_nonzero():
    """Regression: a track's own root element (`<MidiTrack Id="0">`/
    `<AudioTrack Id="0">`) is "0" in Ableton's default templates -- same as
    every OTHER Id="0" in the template, which the cross-track check above
    deliberately skips as the null-pointee sentinel. But tracks are direct
    siblings under <LiveSet><Tracks>, so unlike a scattered automation-
    target Id="0", this one is a real sibling-list membership id: cloning N
    tracks left literally every one of them sharing Id="0", a genuine real-
    Ableton "Non-unique list ids" corruption error on any 2+ track export
    (confirmed via real reload; not caught by the check above precisely
    because it treats Id="0" as always-exempt). See PLAN.md."""
    tracks = [TrackSpec("MIDI", "A"), TrackSpec("Audio", "B"), TrackSpec("MIDI", "C")]
    xml = build_liveset_xml(tracks, tempo=120.0)

    root_ids = re.findall(r'<(?:MidiTrack|AudioTrack) Id="(-?\d+)"', xml)
    assert len(root_ids) == 3
    assert "0" not in root_ids
    assert len(root_ids) == len(set(root_ids))
