"""Embedding a device (e.g. DrumGroupDevice) into a track's empty
<Devices /> list: its own renumbered Ids must not collide with the host
track's Ids, other tracks' Ids, or each other (Id="0" excepted).

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml

from conftest import requires_ableton, track_spans

_FAKE_DRUM_GROUP_DEVICE = (
    '<DrumGroupDevice Id="0"><LomId Value="0" /><SomeChild Id="1" /><Other Id="3" /></DrumGroupDevice>'
)


@requires_ableton
def test_embedded_device_present_and_ids_disjoint_from_track_and_other_tracks():
    tracks = [
        TrackSpec("MIDI", "Kit", device_xml=_FAKE_DRUM_GROUP_DEVICE),
        TrackSpec("Audio", "Vocals"),
        TrackSpec("MIDI", "Bass"),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    devices = root.findall(".//DrumGroupDevice")
    assert len(devices) == 1

    # device's own renumbered Ids must not collide with anything belonging
    # to a different track (same check as test_als_id_renumbering.py,
    # extended to include the embedded device's Ids).
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

    # device Ids must also not collide with its OWN track's other Ids
    # (Id="0" excluded -- null-pointee sentinel, legitimately shared)
    kit_span = spans[0]
    kit_block = xml[kit_span[0] : kit_span[1]]
    device_block_start = kit_block.find("<DrumGroupDevice")
    device_ids = {int(x) for x in re.findall(r'Id="(-?\d+)"', kit_block[device_block_start:]) if x != "0"}
    non_device_ids = {int(x) for x in re.findall(r'Id="(-?\d+)"', kit_block[:device_block_start]) if x != "0"}
    assert device_ids.isdisjoint(non_device_ids)
