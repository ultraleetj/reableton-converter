"""Ableton's LOM/automation-reference system: `<Pointee Id="N"/>`,
`<AutomationTarget Id="N">`, `<ModulationTarget Id="N">` mark
reference-able targets; `<EnvelopeTarget><PointeeId Value="N"/>` points at
one by matching Id. Per open-source Ableton-format readers/writers
(DawVert, community Note-ID-collision fixers -- see PLAN.md), this Id
namespace is GLOBAL across the whole document (not scoped per track or per
element type), with Id="0" as the sole null/unassigned sentinel. This is
the exact invariant the "pointer IDs not unique" fix targets -- verify it
holds without needing to hand-load the file in Ableton.

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import re
from collections import defaultdict

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml

from conftest import requires_ableton


@requires_ableton
def test_pointee_ids_globally_unique_and_references_resolve():
    tracks = [TrackSpec("MIDI", "A"), TrackSpec("Audio", "B"), TrackSpec("MIDI", "C")]
    xml = build_liveset_xml(tracks, tempo=120.0)

    pointee_ids = [
        m.group(1)
        for m in re.finditer(r'<(?:Pointee|AutomationTarget|ModulationTarget)\b[^>]*\bId="(-?\d+)"', xml)
        if m.group(1) != "0"
    ]
    counts = defaultdict(int)
    for pid in pointee_ids:
        counts[pid] += 1
    duplicates = {k: v for k, v in counts.items() if v > 1}
    assert duplicates == {}

    pointee_id_set = set(pointee_ids)
    for m in re.finditer(r'<PointeeId Value="(-?\d+)"', xml):
        ref = m.group(1)
        if ref == "0":
            continue  # unassigned reference, not required to resolve
        assert ref in pointee_id_set, f"dangling PointeeId reference: {ref}"
