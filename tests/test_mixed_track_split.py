"""Ableton has no track that holds both MIDI and audio clips -- a REAPER
track with both must become two Ableton tracks. Uses cli.export_project's
splitting logic indirectly is awkward (it writes a real .als), so these
test the underlying detection helpers directly with synthetic chunks."""

from rpp_to_als.rpp_parse import track_has_audio_item, track_needs_midi

_MIDI_ITEM_SNIPPET = """
    <ITEM
      <SOURCE MIDI
        HASDATA 1 480 QN
        e 0 99 24 54
      >
    >
"""

_AUDIO_ITEM_SNIPPET = """
    <ITEM
      <SOURCE WAVE
        FILE "some.wav"
      >
    >
"""


def test_midi_only_chunk():
    assert track_needs_midi(_MIDI_ITEM_SNIPPET) is True
    assert track_has_audio_item(_MIDI_ITEM_SNIPPET) is False


def test_audio_only_chunk():
    assert track_needs_midi(_AUDIO_ITEM_SNIPPET) is False
    assert track_has_audio_item(_AUDIO_ITEM_SNIPPET) is True


def test_mixed_chunk_detected_as_both():
    mixed = _MIDI_ITEM_SNIPPET + _AUDIO_ITEM_SNIPPET
    assert track_needs_midi(mixed) is True
    assert track_has_audio_item(mixed) is True
