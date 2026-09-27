"""JS FX (ReaJS/JSFX host) and native ReaPlugs VST2 effect support --
see CLAUDE.md's "JSFX/ReaPlugs export" note and PLAN.md. Real-file-backed
where a real sample is available (skips otherwise), plus a few pure unit
tests that don't need any real project/reference file.
"""

import os
import re

import pytest

from rpp_to_als.als_builder import build_readelay_device_xml, build_reajs_device_xml
from rpp_to_als.cli import _reaplugs_plugin_display_name
from rpp_to_als.rpp_parse import (
    find_js_blocks,
    find_vst_blocks,
    parse_tracks,
    reajs_processor_state_hex,
    vst2_processor_state_hex,
)

SAMPLE_RPP = r"D:\REAPER Media\projs\jotas\carrizal\carrizal.RPP"
pytestmark_sample = pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")


def _tracks_by_name():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    return {t.name: t for t in parse_tracks(text)}


@pytestmark_sample
def test_find_js_blocks_parses_real_jsfx_instances():
    tracks = _tracks_by_name()
    js = find_js_blocks(tracks["chords"].chunk)
    assert len(js) == 1
    assert js[0].name == "SStillwell/delay_pong"
    assert js[0].enabled is True
    assert js[0].params.split()[0] == "0"


@pytestmark_sample
def test_vst2_processor_state_hex_extracts_readelay_body_field():
    tracks = _tracks_by_name()
    blocks = find_vst_blocks(tracks["bass"].chunk)
    readelay = next(b for b in blocks if "ReaDelay" in b.name)
    assert len(readelay.fields) == 3
    state_hex = vst2_processor_state_hex(readelay)
    assert state_hex is not None
    # body field: 76 bytes for a 12-param ReaDelay instance (matches the
    # real Ableton-saved readelay-standalone reference used to build
    # templates/vst2_readelay_device.xml)
    assert len(bytes.fromhex(state_hex)) == 76


def test_reajs_processor_state_hex_is_ascii_name_null_params_null_padded():
    state_hex = reajs_processor_state_hex("SStillwell/delay_pong", "0 -3 -2 -6")
    payload = bytes.fromhex(state_hex)
    assert payload.startswith(b"SStillwell/delay_pong\x000 -3 -2 -6")
    assert payload.endswith(b"\x00\x00\x00\x00\x00")


def test_reaplugs_plugin_display_name_strips_prefix_and_vendor_suffix():
    assert _reaplugs_plugin_display_name("VST: ReaDelay (Cockos)") == "ReaDelay"


def test_build_reajs_device_xml_embeds_state_and_is_well_formed_ids():
    state_hex = reajs_processor_state_hex("SStillwell/delay_pong", "0 -3 -2 -6")
    xml = build_reajs_device_xml(state_hex)
    assert xml.startswith('<PluginDevice Id="0">')
    assert state_hex in xml
    assert "{{PROCESSOR_STATE_HEX}}" not in xml


def test_build_readelay_device_xml_embeds_state_and_is_well_formed_ids():
    state_hex = "AA" * 76
    xml = build_readelay_device_xml(state_hex)
    assert xml.startswith('<PluginDevice Id="0">')
    assert state_hex in xml
    assert "{{PROCESSOR_STATE_HEX}}" not in xml
    # raw template ids are small (< 100_000, confirmed against the real
    # reference source) -- no id-compaction step needed unlike Surge/Waves
    ids = [int(x) for x in re.findall(r'Id="(\d+)"', xml)]
    assert max(ids) < 100_000
