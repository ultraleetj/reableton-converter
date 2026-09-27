"""Track shell generation: names, MIDI-vs-Audio type, tempo.

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import gzip
import xml.etree.ElementTree as ET

import pytest

from rpp_to_als.als_builder import TrackSpec, build_liveset_xml, write_als

from conftest import requires_ableton


@requires_ableton
def test_builds_valid_als_with_correct_names_type_and_tempo(tmp_path):
    tracks = [TrackSpec("MIDI", "Kit"), TrackSpec("Audio", "Vocals"), TrackSpec("MIDI", "Bass")]
    xml = build_liveset_xml(tracks, tempo=167.0)
    assert "{{" not in xml

    out_path = tmp_path / "test.als"
    write_als(xml, str(out_path))

    with gzip.open(out_path, "rb") as f:
        decompressed = f.read()
    root = ET.fromstring(decompressed)  # raises if malformed

    names = [e.get("Value") for e in root.iter("EffectiveName")]
    assert "Kit" in names
    assert "Vocals" in names
    assert "Bass" in names

    assert len(root.findall(".//MidiTrack")) == 2
    assert len(root.findall(".//AudioTrack")) == 1

    tempo_manual = root.find(".//Tempo/Manual")
    assert tempo_manual.get("Value") == "167"


@requires_ableton
def test_track_volume_and_pan_are_patched(tmp_path):
    tracks = [
        TrackSpec("MIDI", "Kit", volume=0.5, pan=-0.25),
        TrackSpec("Audio", "Vocals", volume=1.77827941003892, pan=0.9),
    ]
    xml = build_liveset_xml(tracks, tempo=120.0)
    out_path = tmp_path / "test.als"
    write_als(xml, str(out_path))
    with gzip.open(out_path, "rb") as f:
        root = ET.fromstring(f.read())

    volumes = [float(e.get("Value")) for e in root.iter("Volume") for e in [e.find("Manual")] if e is not None]
    pans = [float(e.get("Value")) for e in root.iter("Pan") for e in [e.find("Manual")] if e is not None]
    assert any(v == pytest.approx(0.5) for v in volumes)
    assert any(v == pytest.approx(1.77827941003892) for v in volumes)
    assert any(p == pytest.approx(-0.25) for p in pans)
    assert any(p == pytest.approx(0.9) for p in pans)


@requires_ableton
def test_track_volume_clamped_to_ableton_fader_range(tmp_path):
    # REAPER allows much louder boosts than Ableton's mixer max -- must
    # clamp rather than write an out-of-range value.
    tracks = [TrackSpec("MIDI", "Loud", volume=50.0), TrackSpec("Audio", "Silent", volume=0.0)]
    xml = build_liveset_xml(tracks, tempo=120.0)
    out_path = tmp_path / "test.als"
    write_als(xml, str(out_path))
    with gzip.open(out_path, "rb") as f:
        root = ET.fromstring(f.read())

    volumes = sorted(float(e.get("Value")) for e in root.iter("Volume") for e in [e.find("Manual")] if e is not None)
    assert volumes[0] == pytest.approx(0.0003162277571)
    assert volumes[-1] == pytest.approx(1.99526238)
