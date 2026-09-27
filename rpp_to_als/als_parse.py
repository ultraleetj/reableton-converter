"""Reads an Ableton `.als` Live Set into plain dataclasses -- the reverse
direction's parser (see PLAN.md ".als -> .rpp export"). Only what
rpp_generate.py needs: tempo, time signature, track shells (name,
volume, pan, mute, group nesting), Arrangement + Session MIDI/audio
clips, Locators, Scene names, and each track's top-level device chain
(names only, plus full pad data for a Drum Rack -- the one device the
reverse direction converts, to RS5k; see rpp_generate.py).

Clips are kept in Ableton's own coordinates (content-relative Loop
region, raw warp markers); turning them into flat REAPER items is
rpp_generate.py's job.
"""

from __future__ import annotations

import gzip
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .model import MidiNote

# Same sentinel the forward direction writes -- the "value before time
# began" breakpoint every Tempo/TimeSignature shadow envelope carries
# (see als_builder._patch_shadow_envelope, CLAUDE.md).
_SHADOW_TIME = -63072000.0
_TIME_SIG_DENOMINATORS = [1, 2, 4, 8, 16]


@dataclass
class ClipLoop:
    """A clip's `<Loop>` block. Units are beats, except an UNWARPED audio
    clip's, which are seconds into the file (PLAN.md "Audio clips"). With
    `on` false, `start`/`end` are the clip's start/end markers."""

    start: float
    end: float
    start_relative: float = 0.0
    on: bool = False


@dataclass
class AlsClip:
    kind: str  # "MIDI" or "AUDIO"
    name: str
    loop: ClipLoop
    time: float | None = None  # Arrangement start (beats); None for a Session clip
    length: float | None = None  # Arrangement length (beats); None for a Session clip
    disabled: bool = False
    notes: list[MidiNote] = field(default_factory=list)  # MIDI: positions in content beats
    velocities: list[float] = field(default_factory=list)
    sample_path: str | None = None  # AUDIO
    warped: bool = False
    warp_markers: list[tuple[float, float]] = field(default_factory=list)  # (sec, beat)
    preserve_pitch: bool = True  # False for Re-Pitch warp mode


@dataclass
class DrumPad:
    """One Drum Rack pad with a single-sample Simpler -- the inverse of
    adg_builder.Pad's fields that RS5k can express."""

    note: int
    sample_path: str
    volume: float = 1.0  # linear gain, unity = 1.0
    pan: float = 0.0  # -1..1
    transpose: float = 0.0  # semitones incl. fine
    loop: bool = False
    classic: bool = False  # PlaybackMode 0: stops on note-off
    choke_group: int = 0


@dataclass
class AlsDevice:
    kind: str  # the XML tag, e.g. "DrumGroupDevice", "PluginDevice", "Reverb"
    name: str  # display name
    pads: list[DrumPad] = field(default_factory=list)  # Drum Rack only
    skipped_pads: list[str] = field(default_factory=list)  # Drum Rack only: why each unconvertible pad was skipped


@dataclass
class AlsTrack:
    kind: str  # "MIDI", "AUDIO", "GROUP", "RETURN"
    name: str
    track_id: int
    group_id: int  # TrackGroupId: -1 when top-level
    volume: float = 1.0
    pan: float = 0.0
    muted: bool = False
    arrangement_clips: list[AlsClip] = field(default_factory=list)
    session_clips: dict[int, AlsClip] = field(default_factory=dict)  # scene index -> clip
    devices: list[AlsDevice] = field(default_factory=list)  # top-level chain, in order


@dataclass
class AlsProject:
    tempo: float = 120.0
    time_signature: tuple[int, int] = (4, 4)
    tempo_is_automated: bool = False
    master_volume: float = 1.0
    master_pan: float = 0.0
    tracks: list[AlsTrack] = field(default_factory=list)
    locators: list[tuple[float, str]] = field(default_factory=list)  # (beats, name)
    scene_names: list[str] = field(default_factory=list)


def _val(el: ET.Element | None, path: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    found = el.find(path)
    if found is None:
        return default
    return found.get("Value", default)


def _float(el: ET.Element | None, path: str, default: float = 0.0) -> float:
    v = _val(el, path)
    return float(v) if v is not None else default


def _bool(el: ET.Element | None, path: str, default: bool = False) -> bool:
    v = _val(el, path)
    return (v == "true") if v is not None else default


def decode_time_signature(value: int) -> tuple[int, int]:
    """Inverse of als_builder._encode_time_signature."""
    return (value % 99) + 1, _TIME_SIG_DENOMINATORS[value // 99]


def _envelope_events(scope: ET.Element, target_id: str) -> list[tuple[float, float]]:
    """(time, value) events of the AutomationEnvelope pointing at
    `target_id`, sorted by time -- empty when there's no envelope."""
    # Only the track's own envelopes -- clip envelopes live deeper and
    # can point at the same targets.
    for env in scope.iterfind("AutomationEnvelopes/Envelopes/AutomationEnvelope"):
        if _val(env, "EnvelopeTarget/PointeeId") == target_id:
            events = env.find("Automation/Events")
            if events is None:
                return []
            return sorted(
                (float(ev.get("Time", "0")), float(ev.get("Value", "0")))
                for ev in events
                if ev.get("Value") is not None
            )
    return []


def _param_value(scope: ET.Element, param: ET.Element) -> tuple[float, bool]:
    """A mixer parameter's effective static value: its shadow envelope's
    first breakpoint when one exists (Ableton reads that over `<Manual>`,
    see CLAUDE.md), else `<Manual>`. Second value: whether the envelope
    carries real automation (more than the shadow point)."""
    manual = _float(param, "Manual", 0.0)
    target = param.find("AutomationTarget")
    if target is None:
        return manual, False
    events = _envelope_events(scope, target.get("Id", ""))
    if not events:
        return manual, False
    return events[0][1], len(events) > 1


def _parse_notes(clip_el: ET.Element) -> tuple[list[MidiNote], list[float]]:
    notes: list[MidiNote] = []
    velocities: list[float] = []
    for key_track in clip_el.iterfind("Notes/KeyTracks/KeyTrack"):
        pitch = int(_val(key_track, "MidiKey", "60"))
        for ev in key_track.iterfind("Notes/MidiNoteEvent"):
            if ev.get("IsEnabled", "true") != "true":
                continue
            vel = float(ev.get("Velocity", "100"))
            notes.append(
                MidiNote(
                    position=float(ev.get("Time", "0")),
                    pitch=pitch,
                    velocity=max(1, min(127, round(vel))),
                    length=float(ev.get("Duration", "0")),
                )
            )
            velocities.append(vel)
    order = sorted(range(len(notes)), key=lambda i: (notes[i].position, notes[i].pitch))
    return [notes[i] for i in order], [velocities[i] for i in order]


def _resolve_sample_path(clip_el: ET.Element, als_dir: str) -> str | None:
    return _resolve_file_ref(clip_el.find("SampleRef/FileRef"), als_dir)


def _resolve_file_ref(file_ref: ET.Element | None, als_dir: str) -> str | None:
    if file_ref is None:
        return None
    path = _val(file_ref, "Path", "") or ""
    if path and os.path.exists(path):
        return path
    rel = _val(file_ref, "RelativePath", "") or ""
    if rel:
        candidate = os.path.normpath(os.path.join(als_dir, rel))
        if os.path.exists(candidate):
            return candidate
    return path or None


# WarpMode 3 = Re-Pitch (speed and pitch change together, like REAPER's
# PLAYRATE with preserve-pitch off); every other mode keeps pitch.
_WARP_MODE_REPITCH = "3"


def _parse_clip(clip_el: ET.Element, als_dir: str, arrangement: bool) -> AlsClip:
    kind = "MIDI" if clip_el.tag == "MidiClip" else "AUDIO"
    loop_el = clip_el.find("Loop")
    loop = ClipLoop(
        start=_float(loop_el, "LoopStart"),
        end=_float(loop_el, "LoopEnd"),
        start_relative=_float(loop_el, "StartRelative"),
        on=_bool(loop_el, "LoopOn"),
    )
    clip = AlsClip(
        kind=kind,
        name=_val(clip_el, "Name", "") or "",
        loop=loop,
        disabled=_bool(clip_el, "Disabled"),
    )
    if arrangement:
        start = _float(clip_el, "CurrentStart", float(clip_el.get("Time", "0")))
        clip.time = start
        clip.length = _float(clip_el, "CurrentEnd", start) - start
    if kind == "MIDI":
        clip.notes, clip.velocities = _parse_notes(clip_el)
    else:
        clip.sample_path = _resolve_sample_path(clip_el, als_dir)
        clip.warped = _bool(clip_el, "IsWarped")
        clip.warp_markers = sorted(
            (float(m.get("SecTime", "0")), float(m.get("BeatTime", "0")))
            for m in clip_el.iterfind("WarpMarkers/WarpMarker")
        )
        clip.preserve_pitch = _val(clip_el, "WarpMode") != _WARP_MODE_REPITCH
    return clip


# Friendly names for common native devices; anything else shows its tag.
_NATIVE_DEVICE_NAMES = {
    "DrumGroupDevice": "Drum Rack",
    "InstrumentGroupDevice": "Instrument Rack",
    "AudioEffectGroupDevice": "Audio Effect Rack",
    "MidiEffectGroupDevice": "MIDI Effect Rack",
    "OriginalSimpler": "Simpler",
    "MultiSampler": "Sampler",
    "InstrumentVector": "Wavetable",
    "UltraAnalog": "Analog",
    "Eq8": "EQ Eight",
    "Compressor2": "Compressor",
    "StereoGain": "Utility",
    "AutoFilter": "Auto Filter",
    "GlueCompressor": "Glue Compressor",
    "Delay": "Delay",
    "Reverb": "Reverb",
}


def _device_name(dev: ET.Element) -> str:
    user = _val(dev, "UserName", "") or ""
    if dev.tag in ("PluginDevice", "AuPluginDevice"):
        info = dev.find("PluginDesc")
        plug = None
        if info is not None:
            for sub in info:
                plug = _val(sub, "PlugName") or _val(sub, "Name")
                if plug:
                    break
        base = plug or "unknown plugin"
    else:
        base = _NATIVE_DEVICE_NAMES.get(dev.tag, dev.tag)
    return f"{base} ({user})" if user and user != base else base


def _drum_pads(rack: ET.Element, als_dir: str) -> tuple[list[DrumPad], list[str]]:
    pads: list[DrumPad] = []
    skipped: list[str] = []
    for branch in rack.iterfind("Branches/DrumBranch"):
        # ReceivingNote is stored inverted (see adg_builder._pad_substitutions).
        note = 128 - int(_val(branch, "BranchInfo/ReceivingNote", _val(branch, "ZoneSettings/ReceivingNote", "0")) or 0)
        devices = branch.find("DeviceChain/MidiToAudioDeviceChain/Devices")
        simplers = [d for d in (devices if devices is not None else []) if d.tag == "OriginalSimpler"]
        parts = simplers[0].findall("Player/MultiSampleMap/SampleParts/MultiSamplePart") if simplers else []
        label = _val(branch, "Name/EffectiveName") or _val(branch, "Name/UserName") or f"note {note}"
        if len(parts) != 1:
            skipped.append(f"pad {label!r} (note {note}): not a single-sample Simpler")
            continue
        part = parts[0]
        path = _resolve_file_ref(part.find("SampleRef/FileRef"), als_dir)
        if not path:
            skipped.append(f"pad {label!r} (note {note}): no sample file")
            continue
        simpler = simplers[0]
        mixer = branch.find("MixerDevice")
        pad_vol = _float(mixer, "Volume/Manual", 1.0) if mixer is not None else 1.0
        pad_pan = _float(mixer, "Panorama/Manual", 0.0) if mixer is not None else 0.0
        pads.append(
            DrumPad(
                note=note,
                sample_path=path,
                volume=_float(part, "Volume", 1.0) * pad_vol,
                pan=max(-1.0, min(1.0, _float(part, "Panorama", 0.0) + pad_pan)),
                transpose=_float(simpler, "Pitch/TransposeKey/Manual") + _float(simpler, "Pitch/TransposeFine/Manual") / 100.0,
                loop=_bool(simpler, ".//LoopOn/Manual"),
                classic=_val(simpler, ".//PlaybackMode") == "0",
                choke_group=int(_float(branch, "BranchInfo/ChokeGroup")),
            )
        )
    return pads, skipped


def _parse_devices(track_el: ET.Element, als_dir: str) -> list[AlsDevice]:
    chain = track_el.find("DeviceChain/DeviceChain/Devices")
    out: list[AlsDevice] = []
    for dev in chain if chain is not None else []:
        device = AlsDevice(kind=dev.tag, name=_device_name(dev))
        if dev.tag == "DrumGroupDevice":
            device.pads, device.skipped_pads = _drum_pads(dev, als_dir)
        out.append(device)
    return out


_TRACK_KINDS = {"MidiTrack": "MIDI", "AudioTrack": "AUDIO", "GroupTrack": "GROUP", "ReturnTrack": "RETURN"}


def _parse_track(track_el: ET.Element, als_dir: str) -> AlsTrack:
    kind = _TRACK_KINDS[track_el.tag]
    name = _val(track_el, "Name/EffectiveName") or _val(track_el, "Name/UserName") or ""
    track = AlsTrack(
        kind=kind,
        name=name,
        track_id=int(track_el.get("Id", "0")),
        group_id=int(_val(track_el, "TrackGroupId", "-1")),
    )
    mixer = track_el.find("DeviceChain/Mixer")
    if mixer is not None:
        vol = mixer.find("Volume")
        pan = mixer.find("Pan")
        if vol is not None:
            track.volume = _param_value(track_el, vol)[0]
        if pan is not None:
            track.pan = _param_value(track_el, pan)[0]
        # Speaker = the track's activator button; off means muted.
        track.muted = not _bool(mixer, "Speaker/Manual", True)
    track.devices = _parse_devices(track_el, als_dir)

    seq = track_el.find("DeviceChain/MainSequencer")
    if seq is None:
        return track
    arrangement_lists = (
        seq.find("ClipTimeable/ArrangerAutomation/Events"),
        seq.find("Sample/ArrangerAutomation/Events"),
    )
    for events in arrangement_lists:
        if events is None:
            continue
        for clip_el in events:
            if clip_el.tag in ("MidiClip", "AudioClip"):
                track.arrangement_clips.append(_parse_clip(clip_el, als_dir, arrangement=True))
    track.arrangement_clips.sort(key=lambda c: c.time or 0.0)

    slots = seq.find("ClipSlotList")
    if slots is not None:
        for index, slot in enumerate(slots.iterfind("ClipSlot")):
            value = slot.find("ClipSlot/Value")
            if value is None:
                continue
            for clip_el in value:
                if clip_el.tag in ("MidiClip", "AudioClip"):
                    track.session_clips[index] = _parse_clip(clip_el, als_dir, arrangement=False)
    return track


def parse_als(path: str) -> AlsProject:
    with gzip.open(path) as f:
        root = ET.parse(f).getroot()
    live_set = root.find("LiveSet")
    if live_set is None:
        raise ValueError(f"{path}: no <LiveSet> element -- not an Ableton Live Set")
    als_dir = os.path.dirname(os.path.abspath(path))
    project = AlsProject()

    # Live 12 calls it MainTrack, earlier versions MasterTrack.
    main = live_set.find("MainTrack")
    if main is None:
        main = live_set.find("MasterTrack")
    if main is not None:
        mixer = main.find("DeviceChain/Mixer")
        tempo = mixer.find("Tempo") if mixer is not None else None
        if tempo is not None:
            project.tempo, project.tempo_is_automated = _param_value(main, tempo)
        time_sig = mixer.find("TimeSignature") if mixer is not None else None
        if time_sig is not None:
            project.time_signature = decode_time_signature(int(_param_value(main, time_sig)[0]))
        if mixer is not None and mixer.find("Volume") is not None:
            project.master_volume = _param_value(main, mixer.find("Volume"))[0]
        if mixer is not None and mixer.find("Pan") is not None:
            project.master_pan = _param_value(main, mixer.find("Pan"))[0]

    tracks = live_set.find("Tracks")
    if tracks is not None:
        project.tracks = [_parse_track(t, als_dir) for t in tracks if t.tag in _TRACK_KINDS]

    project.locators = sorted(
        (float(_val(loc, "Time", "0")), _val(loc, "Name", "") or "")
        for loc in live_set.iterfind("Locators/Locators/Locator")
    )
    project.scene_names = [_val(s, "Name", "") or "" for s in live_set.iterfind("Scenes/Scene")]
    return project
