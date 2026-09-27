"""Requires the real chirriado.RPP + TRIAZ sample library (X:\\samplers\\TRIAZ);
skips if either isn't present on disk."""

import gzip
import os
import xml.etree.ElementTree as ET

import pytest

from rpp_to_als.adg_builder import build_pad, build_rack_xml, write_adg
from rpp_to_als.rpp_parse import extract_rs5k_fields, find_rs5k_blocks, parse_tracks
from rpp_to_als.rs5k import param_to_pitch

SAMPLE_RPP = r"D:\REAPER Media\projs\jotas\chirriado\chirriado.RPP"
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "rpp_to_als", "templates", "drum_pad.xml")
TRIAZ_LIBRARY = r"X:\samplers\TRIAZ"


@pytest.mark.skipif(
    not (os.path.exists(SAMPLE_RPP) and os.path.exists(TRIAZ_LIBRARY)),
    reason="sample project or TRIAZ sample library not present",
)
def test_builds_valid_adg_from_real_project(tmp_path):
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    kit_track = parse_tracks(text)[0]
    blocks = find_rs5k_blocks(kit_track.chunk)

    pads = []
    for block in blocks:
        fields = extract_rs5k_fields(block)
        if fields is None or fields.note_lo != fields.note_hi:
            continue
        pad = build_pad(
            note=fields.note_lo,
            wav_path=fields.wav_path,
            volume=fields.volume,
            pan_normalized=fields.pan,
            pitch_st=param_to_pitch(fields.pitch_st_param),
            pad_name=fields.wav_path.rsplit("\\", 1)[-1],
        )
        if pad is not None:
            pads.append(pad)

    assert len(pads) > 0

    template = open(TEMPLATE_PATH, encoding="utf-8").read()
    xml = build_rack_xml(template, "test kit", pads)
    assert "{{" not in xml  # no leftover tokens

    out_path = tmp_path / "test.adg"
    write_adg(xml, str(out_path))

    with gzip.open(out_path, "rb") as f:
        decompressed = f.read()
    root = ET.fromstring(decompressed)  # raises if malformed
    assert len(root.findall(".//DrumBranchPreset")) == len(pads)
