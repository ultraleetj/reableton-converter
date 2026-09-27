"""DAW-agnostic internal data model.

Sits between format parsers (rpp_parse.py, future als_parse.py) and format
generators (adg_builder.py, future als_generate.py, rpp_generate.py) —
neither side knows about the other format. Ported from roadmap.md's
design; kept as the long-term target even though current work
(RS5k -> .adg) only populates a slice of it (tracks with RS5k Devices).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MidiNote:
    position: float  # beats from clip start
    pitch: int  # 0-127
    velocity: int  # 0-127
    length: float  # in beats


@dataclass
class MidiCCEvent:
    position: float  # beats from clip/item start
    cc_number: int  # 0-127
    value: int  # 0-127


@dataclass
class Device:
    device_type: str  # SURGE_VST, SIMPLER, DRUM_RACK, RS5K, EXTERNAL_INSTRUMENT
    name: str
    preset_path: str | None = None  # path to .fxp, .adv, .adg if applicable
    parameters: dict = field(default_factory=dict)


@dataclass
class Clip:
    position: float  # beats from project start
    length: float  # in beats
    clip_type: str  # "MIDI" or "AUDIO"
    name: str = ""
    color: str = ""
    notes: list[MidiNote] = field(default_factory=list)  # MIDI only
    source_path: str | None = None  # AUDIO only: absolute path to audio file
    loop_enabled: bool = False
    loop_start: float = 0.0  # within clip, in beats
    loop_end: float = 0.0


@dataclass
class Track:
    name: str
    color: str = ""  # hex RGB
    track_type: str = "AUDIO"  # MIDI, AUDIO, RETURN, MASTER
    muted: bool = False
    volume: float = 1.0  # 0.0 to 1.0
    pan: float = 0.0  # -1.0 to 1.0
    clips: list[Clip] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)


@dataclass
class Marker:
    position: float  # beats from project start
    name: str = ""
    color: str = ""


@dataclass
class Project:
    tempo: float = 120.0
    time_signature_numerator: int = 4
    time_signature_denominator: int = 4
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
