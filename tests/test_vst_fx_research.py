"""FX/device-matching groundwork (2026-07-24) -- see PLAN.md "FX/device
matching research". find_vst_blocks generalizes find_rs5k_blocks to any
plugin; extract_embedded_preset_xml pulls out a plugin's own
self-describing XML state when it has one (confirmed for JUCE-based
plugins like Surge XT, and for Waves' WaveShell VST3 shell). Requires the
real chirriado.RPP; skips if not present on disk.
"""

import os

import pytest

from rpp_to_als.rpp_parse import extract_embedded_preset_xml, find_vst_blocks, parse_tracks

SAMPLE_RPP = r"D:\REAPER Media\projs\jotas\chirriado\chirriado.RPP"
pytestmark = pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")


def _tracks_by_name():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    return {t.name: t for t in parse_tracks(text)}


def test_finds_rs5k_and_surge_blocks_on_same_project_with_correct_vst3_flag():
    tracks = _tracks_by_name()
    kit_blocks = find_vst_blocks(tracks["swing th1 003 080 KICK HAT TOM SNAR CONG 20"].chunk)
    assert len(kit_blocks) > 0
    assert all(not b.is_vst3 for b in kit_blocks)  # RS5k is VST2
    assert all(b.name.startswith("VSTi: ReaSamplOmatic5000") for b in kit_blocks)

    acid_blocks = find_vst_blocks(tracks["acid bass"].chunk)
    surge = [b for b in acid_blocks if "Surge XT" in b.name]
    assert len(surge) == 1
    assert surge[0].is_vst3 is True


def test_surge_xt_state_is_readable_xml_patch():
    tracks = _tracks_by_name()
    blocks = find_vst_blocks(tracks["acid bass"].chunk)
    surge = next(b for b in blocks if "Surge XT" in b.name)
    xml = extract_embedded_preset_xml(surge.fields[1])
    assert xml is not None
    assert xml.startswith("<?xml")
    assert "<patch " in xml
    assert xml.rstrip().endswith("</patch>")


def test_waves_shell_state_is_readable_preset_chunk_xml_tree():
    tracks = _tracks_by_name()
    blocks = find_vst_blocks(tracks["tp"].chunk)
    waves = [b for b in blocks if "(Waves)" in b.name]
    assert len(waves) >= 2
    for b in waves:
        xml = extract_embedded_preset_xml(b.fields[1])
        assert xml is not None
        assert xml.startswith("<PresetChunkXMLTree")
        assert xml.rstrip().endswith("</PresetChunkXMLTree>")


def test_reapers_own_native_fx_has_no_embedded_preset_xml():
    # ReaDelay (Cockos) is REAPER-native, not a real third-party VST
    # chunk in either the JUCE or PresetChunkXMLTree convention -- a
    # real negative case, not every plugin has a self-describing wrapper.
    tracks = _tracks_by_name()
    blocks = find_vst_blocks(tracks["tp"].chunk)
    readelay = next(b for b in blocks if "ReaDelay" in b.name)
    assert extract_embedded_preset_xml(readelay.fields[1]) is None


def test_extract_embedded_preset_xml_returns_none_for_opaque_bytes():
    assert extract_embedded_preset_xml(b"\x00\x01\x02\x03not xml at all\xff\xfe") is None
