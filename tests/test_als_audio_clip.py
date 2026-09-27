"""build_audio_clip_xml's Loop region math -- see PLAN.md "Audio clips".

No prior coverage existed for this function at all (found the hard way:
a real offset+non-looping item computed a wrong LoopEnd, and a real
offset+warped-but-not-tiling item left LoopEnd/OutMarker far past the
clip's own CurrentEnd, both invisible to every other test in this repo
since none of them exercised a nonzero loop_offset_seconds together with
either a non-looping item or a warped-non-tiling one).
"""

import re

from rpp_to_als.als_builder import build_audio_clip_xml


def _loop_values(xml: str) -> dict[str, float]:
    return {
        tag: float(m.group(1))
        for tag in ("LoopStart", "LoopEnd", "OutMarker", "HiddenLoopStart", "HiddenLoopEnd")
        if (m := re.search(rf'<{tag} Value="([^"]+)"', xml))
    }


def test_unwarped_non_looping_offset_item_loop_end_includes_offset():
    # SOFFS-offset item, no LOOP flag (loop_length_seconds=None): the
    # played region is [offset, offset+length], not [0, length] -- the
    # bug dropped the offset term from LoopEnd entirely.
    xml = build_audio_clip_xml(
        wav_path="C:/x.wav",
        name="x",
        length_beats=8.0,
        duration_frames=44100 * 100,
        sample_rate=44100,
        tempo=120.0,
        loop_offset_seconds=5.0,
        loop_length_seconds=None,
        playrate=1.0,
    )
    loops = _loop_values(xml)
    length_seconds = 8.0 * 60.0 / 120.0  # 4.0
    assert loops["LoopStart"] == 5.0
    assert loops["LoopEnd"] == 5.0 + length_seconds
    assert 'LoopOn Value="false"' in xml


def test_native_playrate_looping_shorter_cycle_tiles_via_warp():
    # A shorter-than-played-length loop cycle forces IsWarped="true" even
    # at native playrate 1.0 -- real Ableton never actually tiles an
    # unwarped clip's Loop bracket across CurrentEnd in Arrangement (see
    # build_audio_clip_xml docstring), so Loop switches to the warped
    # clip's beats coordinate space here, not seconds.
    xml = build_audio_clip_xml(
        wav_path="C:/x.wav",
        name="x",
        length_beats=16.0,
        duration_frames=44100 * 100,
        sample_rate=44100,
        tempo=120.0,
        loop_offset_seconds=0.0,
        loop_length_seconds=2.0,  # much shorter than the 8s item length
        playrate=1.0,
    )
    loops = _loop_values(xml)
    assert 'IsWarped Value="true"' in xml
    assert loops["LoopEnd"] == 4.0  # 2 raw seconds * (16 beats / 8 seconds)
    assert 'LoopOn Value="true"' in xml


def test_warped_non_tiling_item_loop_end_clamped_to_clip_length():
    # playrate != 1.0, LOOP set but the remaining raw file content is far
    # longer than one play-through -- no tiling needed (matches REAPER:
    # LOOP is a no-op when content already exceeds the displayed length).
    # Loop/OutMarker/HiddenLoopEnd must clamp to length_beats, not the
    # raw, much-larger "rest of file" value -- otherwise Ableton keeps the
    # clip's playable/scrollable content mapped past its own CurrentEnd.
    xml = build_audio_clip_xml(
        wav_path="C:/x.wav",
        name="x",
        length_beats=4.0,
        duration_frames=44100 * 200,  # 200s file
        sample_rate=44100,
        tempo=120.0,
        loop_offset_seconds=10.0,
        loop_length_seconds=190.0,  # 200 - 10, way more than one play-through
        playrate=1.4,
    )
    loops = _loop_values(xml)
    assert loops["LoopEnd"] == 4.0
    assert loops["OutMarker"] == 4.0
    assert loops["HiddenLoopEnd"] == 4.0
    assert 'LoopOn Value="false"' in xml


def test_volume_maps_directly_to_sample_volume():
    # REAPER item VOLPAN's linear-gain first field -> Ableton's
    # SampleVolume, same linear-gain convention, no remapping.
    xml = build_audio_clip_xml(
        wav_path="C:/x.wav",
        name="x",
        length_beats=4.0,
        duration_frames=44100 * 10,
        sample_rate=44100,
        tempo=120.0,
        volume=0.707946,
    )
    assert '<SampleVolume Value="0.707946" />' in xml


def test_volume_defaults_to_unity():
    xml = build_audio_clip_xml(
        wav_path="C:/x.wav",
        name="x",
        length_beats=4.0,
        duration_frames=44100 * 10,
        sample_rate=44100,
        tempo=120.0,
    )
    assert '<SampleVolume Value="1" />' in xml


def test_warped_tiling_item_loop_end_shorter_than_clip_length():
    xml = build_audio_clip_xml(
        wav_path="C:/x.wav",
        name="x",
        length_beats=16.0,
        duration_frames=44100 * 10,
        sample_rate=44100,
        tempo=120.0,
        loop_offset_seconds=0.0,
        loop_length_seconds=1.0,  # short raw cycle, genuinely tiles
        playrate=1.0 + 1e-6,  # force is_warped True with ~unity ratio
    )
    loops = _loop_values(xml)
    assert loops["LoopEnd"] < 16.0
    assert 'LoopOn Value="true"' in xml
