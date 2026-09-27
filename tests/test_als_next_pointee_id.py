"""Regression: a real export failed to load in Ableton with "Document ...
is damaged" / "NextPointeeId is too low" -- the template's small default
value wasn't bumped past our renumbered (much larger) Ids. See PLAN.md.

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import re

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml

from conftest import requires_ableton


@requires_ableton
def test_next_pointee_id_exceeds_every_id_used():
    tracks = [TrackSpec("MIDI", n) for n in "ABCDEFG"]  # enough tracks for large offsets
    xml = build_liveset_xml(tracks, tempo=120.0)

    next_pointee = int(re.search(r'<NextPointeeId Value="(\d+)" />', xml).group(1))
    max_id = max(int(x) for x in re.findall(r'Id="(-?\d+)"', xml))
    assert next_pointee > max_id
