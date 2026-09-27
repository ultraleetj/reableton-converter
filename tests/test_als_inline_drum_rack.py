"""Inline Drum Rack embedding: adg_builder.build_inline_drum_group_device()
must produce REAL, populated <DrumBranch> pads (not an empty <Branches />
shell), with each pad's own list-scoped ids kept distinct across pads
-- see PLAN.md's "pending investigation" note this resolves and CLAUDE.md's
"Non-unique list ids" precedent for why this matters.

The DrumGroupDevice-building part only needs this repo's own template
files (no Ableton install required); embedding the result into a real
track block needs the Ableton default templates, same as
test_als_embedded_device.py.
"""

import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

import pytest

from rpp_to_als.adg_builder import Pad, build_inline_drum_group_device, build_rack_xml
from rpp_to_als.als_builder import TrackSpec, build_liveset_xml

from conftest import requires_ableton, track_spans

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "rpp_to_als", "templates", "drum_pad.xml")
_INLINE_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "rpp_to_als", "templates", "drum_pad_inline.xml"
)


def _template() -> str:
    with open(_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def _inline_template() -> str:
    with open(_INLINE_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def _pad(note: int, name: str, **overrides) -> Pad:
    defaults = dict(
        name=name,
        path=f"C:/samples/{name}",
        pad_name=name,
        volume=1.0,
        panorama=0.0,
        transpose_key=0,
        transpose_fine=0,
        frames=1000,
        rate=44100,
    )
    defaults.update(overrides)
    return Pad(note=note, **defaults)


def _three_pads() -> list[Pad]:
    return [
        _pad(36, "kick.wav"),
        _pad(38, "snare.wav", volume=0.8, panorama=-0.2, transpose_key=1, transpose_fine=5),
        _pad(42, "hat.wav", volume=0.5, panorama=0.3, transpose_key=-2, transpose_fine=-10),
    ]


def test_pads_actually_present_not_empty_shell():
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    # This is the exact bug being fixed: the old extract_drum_group_device()
    # path produced a genuinely empty <Branches /> here.
    assert "<Branches />" not in device_xml
    assert device_xml.count("<DrumBranch ") == 3
    assert device_xml.count("<OriginalSimpler") == 3
    assert device_xml.count("<MixerDevice>") == 3


def test_no_leftover_placeholders():
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    assert "{{" not in device_xml


def test_well_formed_xml():
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    ET.fromstring(f"<root>{device_xml}</root>")


def test_receiving_note_inversion_and_sample_names():
    pads = _three_pads()
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", pads)
    for p in pads:
        assert f'<ReceivingNote Value="{128 - p.note}" />' in device_xml
        assert f'<EffectiveName Value="{p.pad_name}" />' in device_xml


def test_choke_group_applied():
    pads = _three_pads()
    device_xml = build_inline_drum_group_device(
        _template(), _inline_template(), "Test Kit", pads, choke_group_by_note={42: 1}
    )
    # Find the hat pad's own DrumBranch (note 42 -> ReceivingNote 86) and
    # confirm it (and only it) carries the choke group.
    hat_branch = re.search(r"<DrumBranch Id=\"2\">.*?</DrumBranch>", device_xml, re.DOTALL).group(0)
    assert '<ChokeGroup Value="1" />' in hat_branch
    kick_branch = re.search(r"<DrumBranch Id=\"0\">.*?</DrumBranch>", device_xml, re.DOTALL).group(0)
    assert '<ChokeGroup Value="0" />' in kick_branch


def test_pad_ids_unique_within_branches_list():
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    ids = re.findall(r'<DrumBranch Id="(-?\d+)"', device_xml)
    assert ids == ["0", "1", "2"]
    assert len(ids) == len(set(ids))


def test_pointee_automation_modulation_targets_are_nonzero_and_unique():
    """Regression: a real reload attempt found that leaving these at the
    cloned template's `Id="0"` (the usual safe null-pointee sentinel
    elsewhere in this codebase) breaks once the same zeroed automation-
    registry subtree is cloned as exact siblings at scale (one pad's
    OriginalSimpler has ~86 such targets, x N pads) -- "Invalid Pointee
    Id" on real Ableton reload, confirmed by diffing against a real
    Ableton-saved Drum Rack where every single one of these is a distinct
    nonzero id, never "0" (see PLAN.md "Pointee/AutomationTarget/
    ModulationTarget must be nonzero in a populated device").
    build_inline_drum_group_device now renumbers all of them
    (_renumber_pointee_family) -- confirm none remain "0" and all are
    globally unique."""
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    for tag in ("Pointee", "AutomationTarget", "ModulationTarget"):
        ids = re.findall(rf'<{tag} Id="(-?\d+)"', device_xml)
        assert ids, f"expected at least one {tag} in a real pad's OriginalSimpler payload"
        assert "0" not in ids, f"{tag} still has Id=\"0\" after renumbering"
        assert len(ids) == len(set(ids)), f"{tag} has duplicate ids"


def test_per_pad_list_scoped_ids_distinct_across_pads():
    """The secondary bug: FileRef/WarpMarker/AudioBranchSendInfo/
    MultiSamplePart ids inside each pad's own OriginalSimpler/MixerDevice
    payload used to be hardcoded to the SAME nonzero value regardless of
    pad index (e.g. every pad's FileRef Id="22") -- same-shaped issue as
    the already-fixed KeyTrack/MidiClip "Non-unique list ids" bug. Confirm
    each pad clone now gets its own distinct values."""
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    for tag in ("MultiSamplePart", "FileRef", "WarpMarker", "AudioBranchSendInfo"):
        nonzero = [m for m in re.findall(rf'<{tag} Id="(-?\d+)"', device_xml) if m != "0"]
        assert len(nonzero) == len(set(nonzero)), f"{tag} has duplicate non-zero ids across pads: {nonzero}"


def test_no_intra_pad_id_collision_between_different_elements():
    """Regression: the real inline template has `<BranchSourceContext
    Id="0">` ALWAYS -- confirmed against a real Ableton file (16 real
    pads, every one has BranchSourceContext Id="0" regardless of its own
    DrumBranch's index, see PLAN.md). The template briefly (wrongly)
    reused {{PAD_ID}} for BOTH `<DrumBranch Id="...">` and
    `<BranchSourceContext Id="...">`, so for any pad after the first,
    those two DIFFERENT element types ended up sharing the same nonzero
    id within one pad -- a real "Invalid Pointee Id" load failure found
    via an actual Ableton reload attempt, not caught by any per-tag
    duplicate check (each tag's own ids were individually unique; the
    collision was ACROSS tag types within one pad). Needs >= 2 pads to
    manifest (pad 0's id is "0", exempt)."""
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Test Kit", _three_pads())
    for pad_xml in re.findall(r"<DrumBranch Id=\"\d+\">.*?</DrumBranch>", device_xml, re.DOTALL):
        ids = re.findall(r'Id="(-?\d+)"', pad_xml)
        nonzero = [i for i in ids if i != "0"]
        assert len(nonzero) == len(set(nonzero)), f"intra-pad id collision: {pad_xml[:200]}"


def test_standalone_adg_path_unaffected():
    """build_rack_xml (the confirmed-working standalone .adg path) must
    keep producing the preset-format DrumBranchPreset/empty-Branches shape
    exactly as before -- this fix is purely additive, a separate function
    for the inline-embed case."""
    rack_xml = build_rack_xml(_template(), "Test Kit", _three_pads())
    assert "<Branches />" in rack_xml
    assert rack_xml.count("<DrumBranchPreset ") == 3
    assert "<DrumBranch " not in rack_xml.replace("<DrumBranchPreset ", "")


@requires_ableton
def test_embedded_in_als_track_pads_survive_and_ids_stay_disjoint():
    """End-to-end: embed the populated device into a real track block (same
    path als_builder._embed_device/build_track_block uses) and confirm the
    pads are still there and no id collides across tracks (Id="0"
    excepted), mirroring test_als_embedded_device.py's checks but with a
    REAL populated device instead of the earlier synthetic fake."""
    device_xml = build_inline_drum_group_device(_template(), _inline_template(), "Kit", _three_pads())
    tracks = [
        TrackSpec("MIDI", "Kit", device_xml=device_xml),
        TrackSpec("Audio", "Vocals"),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    root = ET.fromstring(xml.encode("utf-8"))

    branches = root.findall(".//DrumGroupDevice/Branches/DrumBranch")
    assert len(branches) == 3

    spans = track_spans(xml)
    id_owning_tracks = defaultdict(set)
    for m in re.finditer(r'Id="(-?\d+)"', xml):
        if m.group(1) == "0":
            continue
        pos = m.start()
        owner = next((i for i, (s, e) in enumerate(spans) if s <= pos <= e), None)
        if owner is not None:
            id_owning_tracks[m.group(1)].add(owner)
    cross_track = {k: v for k, v in id_owning_tracks.items() if len(v) > 1}
    assert cross_track == {}
