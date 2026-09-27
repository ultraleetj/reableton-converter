"""Project-root time signature: Ableton packs numerator+denominator into
one integer (`<TimeSignature><Manual Value="N" />`, distinct from a
MidiClip's own separate-fields RemoteableTimeSignature). See PLAN.md
"Time signature" -- formula confirmed against a real Ableton template
file's own numeric bounds plus an independent open-source encoder/decoder
pair (DawVert).

Requires Ableton Live 12 installed at the default templates path; skips if
not present.
"""

import re

import pytest

from rpp_to_als.als_builder import TrackSpec, _encode_time_signature, build_liveset_xml

from conftest import requires_ableton


def test_encode_time_signature_matches_known_real_values():
    # 4/4 confirmed directly against Default Audio Track.als's own
    # <TimeSignature><Manual Value="201" />; 99/16 confirmed against the
    # same file's <MidiControllerRange><Max Value="494" /> (the packing
    # scheme's own documented upper bound).
    assert _encode_time_signature(4, 4) == 201
    assert _encode_time_signature(99, 16) == 494
    assert _encode_time_signature(1, 1) == 0
    assert _encode_time_signature(3, 4) == 200  # chirriado.RPP's real meter


def test_encode_time_signature_rejects_unsupported_values():
    with pytest.raises(ValueError):
        _encode_time_signature(4, 3)  # denominator must be a power of 2 up to 16
    with pytest.raises(ValueError):
        _encode_time_signature(0, 4)
    with pytest.raises(ValueError):
        _encode_time_signature(100, 4)


@requires_ableton
def test_build_liveset_xml_defaults_to_four_four():
    xml = build_liveset_xml([TrackSpec("Audio", "A")], tempo=120.0)
    m = re.search(r'<TimeSignature>\s*<LomId Value="0" />\s*<Manual Value="(\d+)" />', xml)
    assert m is not None
    assert int(m.group(1)) == 201


@requires_ableton
def test_build_liveset_xml_applies_custom_time_signature():
    xml = build_liveset_xml([TrackSpec("Audio", "A")], tempo=120.0, time_signature=(3, 4))
    m = re.search(r'<TimeSignature>\s*<LomId Value="0" />\s*<Manual Value="(\d+)" />', xml)
    assert m is not None
    assert int(m.group(1)) == 200


@requires_ableton
def test_tempo_and_time_signature_shadow_envelopes_are_patched():
    """Regression: Ableton's transport reads Tempo/TimeSignature from a
    SECOND, shadow <AutomationEnvelope> (a stock single-breakpoint
    envelope every project carries at Time="-63072000", even when the
    parameter isn't actually automated) tied to the field's own
    AutomationTarget via matching Ids -- not from the <Manual> value
    directly. <Manual> is only the fallback when no envelope exists.
    Patching only <Manual> produces a well-formed, reload-clean file
    where the transport nonetheless keeps showing the template's
    original 120bpm/4:4 forever, confirmed against a project genuinely
    saved by real Ableton (not just our factory-shipped templates) --
    found by tracing the AutomationTarget Id -> PointeeId link after
    repeatedly patching <Manual> alone had no visible effect even in a
    real project file. See PLAN.md "Tempo/time signature shadow
    envelope"."""
    xml = build_liveset_xml([TrackSpec("Audio", "A")], tempo=167.0, time_signature=(3, 4))

    tempo_target = re.search(r"<Tempo>.*?<AutomationTarget Id=\"(\d+)\"", xml, re.DOTALL).group(1)
    tempo_envelope = re.search(
        rf'<PointeeId Value="{tempo_target}" />\s*</EnvelopeTarget>\s*<Automation>\s*<Events>\s*'
        r'<FloatEvent Id="0" Time="-63072000" Value="([\d.]+)"',
        xml,
    )
    assert tempo_envelope is not None
    assert float(tempo_envelope.group(1)) == 167.0

    ts_target = re.search(r"<TimeSignature>.*?<AutomationTarget Id=\"(\d+)\"", xml, re.DOTALL).group(1)
    ts_envelope = re.search(
        rf'<PointeeId Value="{ts_target}" />\s*</EnvelopeTarget>\s*<Automation>\s*<Events>\s*'
        r'<EnumEvent Id="0" Time="-63072000" Value="(\d+)"',
        xml,
    )
    assert ts_envelope is not None
    assert int(ts_envelope.group(1)) == 200
