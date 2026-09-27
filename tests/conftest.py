"""Shared fixtures/helpers for tests that need real Ableton Live 12 default
templates on disk (als_builder.py reads them directly, no bundled copy --
see als_builder.py docstring)."""

import os
import re

import pytest

from rpp_to_als.als_builder import _AUDIO_TRACK_ALS, _MIDI_TRACK_ALS

ABLETON_AVAILABLE = os.path.exists(_AUDIO_TRACK_ALS) and os.path.exists(_MIDI_TRACK_ALS)

requires_ableton = pytest.mark.skipif(
    not ABLETON_AVAILABLE, reason="Ableton Live 12 default templates not found"
)


def track_spans(xml: str) -> list[tuple[int, int]]:
    """(start, end) char offsets of every top-level MidiTrack/AudioTrack
    block in a built LiveSet XML string."""
    spans = []
    for m in re.finditer(r"<(MidiTrack|AudioTrack)\b[^>]*>", xml):
        tag = m.group(1)
        close = xml.find(f"</{tag}>", m.end())
        spans.append((m.start(), close))
    return spans
