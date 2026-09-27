"""Reverse direction extras: Drum Rack -> RS5k chain, --copy-media, and
the FX report in both directions. See PLAN.md "Drum Rack -> RS5k" /
"FX report"."""

import os

from rpp_to_als.als_parse import AlsClip, AlsDevice, AlsProject, AlsTrack, ClipLoop, DrumPad
from rpp_to_als.cli import _forward_fx_report
from rpp_to_als.fx_report import FxEntry, format_report, group
from rpp_to_als.rpp_generate import convert, rs5k_state
from rpp_to_als.rpp_parse import (
    RawRs5kBlock,
    audio_item_file_path,
    extract_rs5k_fields,
    find_items,
    find_rs5k_blocks,
    list_track_fx,
    parse_tracks,
)


def _rack(pads, name="Drum Rack"):
    return AlsDevice(kind="DrumGroupDevice", name=name, pads=pads)


def test_rs5k_state_reads_back_through_the_forward_parser():
    pad = DrumPad(note=42, sample_path=r"C:\kits\hat.wav", volume=0.5, pan=-0.5, transpose=-12.0, loop=True, classic=True)
    header, body = rs5k_state(pad)
    assert int.from_bytes(header[32:36], "little") == len(body)
    f = extract_rs5k_fields(RawRs5kBlock(header=header, body=body, tail=b"", wav_path=pad.sample_path))
    assert (f.note_lo, f.note_hi) == (42, 42)
    assert f.volume == 0.5
    assert f.pan == 0.25
    assert f.pitch_st_param == 0.25  # (-12 + 24) / 48
    assert f.loop and f.obey_note_off


def test_drum_rack_becomes_rs5k_chain_in_the_rpp(tmp_path):
    pads = [DrumPad(36, str(tmp_path / "kick.wav")), DrumPad(38, str(tmp_path / "snare.wav"))]
    track = AlsTrack("MIDI", "drums", 1, -1, devices=[_rack(pads)])
    result = convert(AlsProject(tracks=[track]))
    chunk = parse_tracks(result.text)[0].chunk
    blocks = find_rs5k_blocks(chunk)
    assert [extract_rs5k_fields(b).note_lo for b in blocks] == [36, 38]
    assert [b.wav_path for b in blocks] == [p.sample_path for p in pads]
    assert result.fx_report == [FxEntry("drums", "Drum Rack", True, "-> 2 RS5k instance(s)")]


def test_unmapped_devices_and_return_tracks_are_reported():
    track = AlsTrack("AUDIO", "gtr", 1, -1, devices=[AlsDevice("Reverb", "Reverb")])
    ret = AlsTrack("RETURN", "A-Delay", 2, -1, devices=[AlsDevice("Delay", "Delay")])
    report = convert(AlsProject(tracks=[track, ret])).fx_report
    assert [(e.track, e.fx, e.converted) for e in report] == [("A-Delay", "Delay", False), ("gtr", "Reverb", False)]


def test_choke_groups_are_flagged_as_lost():
    pads = [DrumPad(42, "hh.wav", choke_group=1), DrumPad(46, "oh.wav", choke_group=1)]
    report = convert(AlsProject(tracks=[AlsTrack("MIDI", "d", 1, -1, devices=[_rack(pads)])])).fx_report
    assert "choke groups not carried over" in report[0].note


def test_copy_media_points_items_and_pads_into_the_subfolder(tmp_path):
    src = tmp_path / "lib"
    src.mkdir()
    (src / "loop.wav").write_bytes(b"x")
    (src / "kick.wav").write_bytes(b"x")
    other = tmp_path / "other"
    other.mkdir()
    (other / "loop.wav").write_bytes(b"y")  # same name, different file
    out = tmp_path / "proj"

    def clip(path, time):
        return AlsClip("AUDIO", "a", ClipLoop(0.0, 1.0), time=time, length=2.0, sample_path=str(path))

    track = AlsTrack(
        "AUDIO",
        "t",
        1,
        -1,
        arrangement_clips=[clip(src / "loop.wav", 0.0), clip(src / "loop.wav", 4.0), clip(other / "loop.wav", 8.0)],
    )
    drums = AlsTrack("MIDI", "d", 2, -1, devices=[_rack([DrumPad(36, str(src / "kick.wav"))])])
    result = convert(AlsProject(tracks=[track, drums]), out_dir=str(out), media_subdir="mediaFiles")

    assert 'RECORD_PATH "mediaFiles" ""' in result.text
    tracks = parse_tracks(result.text)
    files = [audio_item_file_path(i) for i in find_items(tracks[0].chunk)]
    assert files == [r"mediaFiles\loop.wav", r"mediaFiles\loop.wav", r"mediaFiles\loop (2).wav"]
    assert find_rs5k_blocks(tracks[1].chunk)[0].wav_path == os.path.abspath(out / "mediaFiles" / "kick.wav")
    assert sorted(os.path.basename(d) for _s, d in result.media_copies) == ["kick.wav", "loop (2).wav", "loop.wav"]


def test_list_track_fx_names_every_kind():
    chunk = "\n".join(
        [
            "<TRACK",
            '  <VST "VST3: Pro-Q 3 (FabFilter)" x.vst3 0 "" 1{ABC} ""',
            "  >",
            "  <JS SStillwell/delay_pong \"\"",
            "  >",
            '  <CLAP "CLAPi: Vital (Vital Audio)" com.vital ""',
            "  >",
            ">",
        ]
    )
    assert list_track_fx(chunk) == ["VST3: Pro-Q 3 (FabFilter)", "JS: SStillwell/delay_pong", "CLAPi: Vital (Vital Audio)"]


def test_forward_report_counts_consumed_instances():
    rs5k = '  <VST "VSTi: ReaSamplOmatic5000 (Cockos)" r.dll 0 "" 1<AB> ""\n  >'
    chunk = "\n".join(["<TRACK", rs5k, rs5k, rs5k, '  <VST "VSTi: ReaSynth (Cockos)" s.dll 0 "" 1<AB> ""', "  >", ">"])
    name = "VSTi: ReaSamplOmatic5000 (Cockos)"
    report = _forward_fx_report("d", chunk, [name, name], {name: "-> Drum Rack pad"})
    assert [(e.fx, e.converted, e.count) for e in report] == [
        (name, True, 2),
        (name, False, 1),
        ("VSTi: ReaSynth (Cockos)", False, 1),
    ]


def test_group_and_format_report():
    entries = group([FxEntry("d", "RS5k", True), FxEntry("d", "RS5k", True), FxEntry("d", "Verb", False, "nope")])
    assert format_report(entries) == [
        "FX report: 2 converted, 1 not converted",
        "  [converted] d: RS5k (x2)",
        "  [NOT converted] d: Verb -- nope",
    ]


def test_simpler_sine_template_is_a_bare_sine():
    import xml.etree.ElementTree as ET

    from rpp_to_als.als_builder import build_simpler_sine_device_xml

    simpler = ET.fromstring(build_simpler_sine_device_xml())
    assert simpler.tag == "OriginalSimpler" and simpler.get("Id") == "0"
    assert simpler.find("Filter/IsOn/Manual").get("Value") == "false"
    assert simpler.find("Lfo/IsOn/Manual").get("Value") == "false"
    ref = simpler.find("Player/MultiSampleMap/SampleParts/MultiSamplePart/SampleRef/FileRef")
    assert ref.find("RelativePathType").get("Value") == "5"  # Core-Library-relative, portable
    assert ref.find("RelativePath").get("Value").endswith("Sine Saw Square Sine.aif")


def test_simpler_sine_targets_are_numbered_for_a_top_level_device():
    # Id 0 targets are only OK inside a Drum Rack pad; directly on a
    # track they made Ableton reject the Set ("Invalid Pointee Id").
    import re

    from rpp_to_als.als_builder import build_simpler_sine_device_xml

    ids = re.findall(r'<(?:AutomationTarget|ModulationTarget|Pointee) Id="(\d+)"', build_simpler_sine_device_xml())
    assert ids and "0" not in ids
    assert len(ids) == len(set(ids))
