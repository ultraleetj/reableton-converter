"""RS5k (ReaSamplomatic5000) parameter constants and conversions.

Ported from the normalized-param-index table used by ``triaz_browser.lua``
(REAPER Lua API, ``TrackFX_GetParamNormalized``). Params 0-15 of this table
also match the on-disk float64 array inside a saved ``.rpp`` file's RS5k
blob, one param per 8-byte slot in index order — see
``rpp_parse.extract_rs5k_fields`` and PLAN.md for how that offset was
found. Params 16+ don't line up on disk and aren't used by this tool.

**Volume is the one exception**: the on-disk value is 2x what
``TrackFX_GetParamNormalized`` reports for param 0, and the on-disk value
is the *correct* linear gain (unity = 1.0) — dividing it by 2, as the live
API implies, is what caused the "+4.1dB shown for a 0.8 value" mapping bug
noted as a deferred bug in ``reference/ADG_EXPORT_PLAN.md``. When reading
from a ``.rpp`` file directly, use the raw on-disk value as-is.
"""

from dataclasses import dataclass

# Param indices verified via TrackFX_GetParamName dump on RS5k (33 params total)
PARAM = {
    "volume": 0,  # 0..1 (linear gain)
    "pan": 1,  # 0..1 (0=L, 0.5=C, 1=R)
    "gain_min_vel": 2,  # Gain at velocity 0: 0=silent, 1=full
    "note_lo": 3,  # Note range start: 0..1 mapped from MIDI 0..127
    "note_hi": 4,  # Note range end
    "pitch_note_lo": 5,  # Pitch at note_lo, same normalization as pitch_st
    "pitch_note_hi": 6,  # Pitch at note_hi
    "max_voices": 8,  # Max polyphony: normalized N/9 (0=unlimited)
    "attack": 9,  # ADSR attack time: 0=instant, 1=max
    "obey_note_off": 11,  # 0=one-shot, 1=stop on note-off
    "loop": 12,  # 0=no loop, 1=loop
    "pitch_st": 15,  # Pitch adjust: normalized 0..1, 0.5 = 0 semitones
    "release_note_off": 26,  # Release time after note-off
    "use_note_off_rel": 27,  # 0=off, 1=use release_note_off
}


def midi_to_param(midi: int) -> float:
    return midi / 127.0


def param_to_midi(p: float) -> int:
    return round(p * 127)


def pitch_to_param(semitones: float) -> float:
    """RS5k pitch range is -24..+24 semitones, normalized to 0..1."""
    return (semitones + 24) / 48.0


def param_to_pitch(p: float) -> float:
    return p * 48.0 - 24


def rs5k_pan_to_panorama(pan_param: float) -> float:
    """RS5k pan (0..1, 0.5=center) -> Ableton Panorama (-1..1, 0=center)."""
    return (pan_param - 0.5) * 2


@dataclass
class Rs5kInstance:
    """Extracted state for one RS5k instance, ready for .adg pad export."""

    wav_path: str
    note: int  # MIDI note (only single-note pads supported in v1)
    volume: float  # linear 0..1
    pan: float  # RS5k normalized 0..1
    pitch_st: float  # semitones
