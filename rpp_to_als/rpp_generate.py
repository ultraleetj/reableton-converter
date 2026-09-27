"""AlsProject (als_parse.py) -> REAPER `.rpp` text -- the reverse
direction's generator (see PLAN.md ".als -> .rpp export").

Every Ableton clip becomes plain NON-looping REAPER items: a looped MIDI
clip has its notes unrolled across the clip's full length; a looped
audio clip becomes one item per loop cycle. REAPER's own item LOOP
repeats the whole source file, so it can't express an Ableton loop
brace that covers only part of the content -- unrolling plays back
identically either way.

Drum Racks become a chain of RS5k instances, one per pad (the inverse
of adg_builder's RS5k -> Drum Rack); no other device is converted, but
every one is listed in `Conversion.fx_report`.

Session clips (opt-in, `session_scenes=True`) are laid out after the
Arrangement content, one region per non-empty Scene with a marker named
after it -- the reverse of `export-project --scenes`.
"""

from __future__ import annotations

import base64
import bisect
import math
import ntpath
import os
import struct
import uuid
from dataclasses import dataclass, field

from .als_parse import AlsClip, AlsProject, AlsTrack, DrumPad
from .fx_report import FxEntry
from .model import MidiNote

_EPS = 1e-9
_PPQ = 960


@dataclass
class RppItem:
    position: float  # beats from project start
    length: float  # beats
    name: str = ""
    muted: bool = False
    notes: list[MidiNote] = field(default_factory=list)  # MIDI: positions in beats from item start
    source_path: str | None = None  # AUDIO
    source_offset: float = 0.0  # AUDIO: seconds into the file
    playrate: float = 1.0
    preserve_pitch: bool = True


def _content_start(clip: AlsClip) -> float:
    # With looping off, LoopStart IS the start marker; StartRelative only
    # means "start marker relative to loop start" when looping is on (our
    # own forward export writes StartRelative=-LoopStart on unlooped
    # clips, real Ableton writes 0 -- both mean start == LoopStart).
    loop = clip.loop
    return loop.start + loop.start_relative if loop.on else loop.start


def _segments(clip: AlsClip, total: float, k: float) -> list[tuple[float, float, float, float]]:
    """Split `total` timeline beats of playback into contiguous pieces of
    clip content: (timeline_offset_beats, length_beats, content_from,
    content_to). `k` = content units per timeline beat (1 for beats,
    60/tempo for an unwarped audio clip's seconds)."""
    loop = clip.loop
    looping = loop.on and loop.end - loop.start > _EPS
    c = _content_start(clip)
    if looping and not (loop.start - _EPS <= c < loop.end - _EPS):
        c = loop.start
    out: list[tuple[float, float, float, float]] = []
    t = 0.0
    while t < total - _EPS:
        if looping:
            seg = min((loop.end - c) / k, total - t)
        else:
            seg = total - t
        out.append((t, seg, c, c + seg * k))
        t += seg
        c = loop.start
    return out


def natural_length(clip: AlsClip, tempo: float) -> float:
    """Beats one pass of the clip takes -- a Session clip's own length
    (loop off: start->end marker; loop on: one loop cycle)."""
    k = _content_units_per_beat(clip, tempo)
    loop = clip.loop
    start = loop.start if loop.on else _content_start(clip)
    return max(0.0, (loop.end - start) / k)


def _content_units_per_beat(clip: AlsClip, tempo: float) -> float:
    if clip.kind == "AUDIO" and not clip.warped:
        return 60.0 / tempo
    return 1.0


def _warp_seconds(clip: AlsClip, beat: float, tempo: float) -> float:
    """Content beat -> seconds into the file, piecewise-linear through the
    clip's warp markers, extrapolated along the first/last segment."""
    points: list[tuple[float, float]] = []
    for sec, b in sorted(clip.warp_markers, key=lambda m: m[1]):
        if not points or b - points[-1][0] > _EPS:
            points.append((b, sec))
    if len(points) < 2:
        b0, s0 = points[0] if points else (0.0, 0.0)
        return s0 + (beat - b0) * 60.0 / tempo
    i = bisect.bisect_right([b for b, _ in points], beat) - 1
    i = max(0, min(i, len(points) - 2))
    (b0, s0), (b1, s1) = points[i], points[i + 1]
    return s0 + (beat - b0) * (s1 - s0) / (b1 - b0)


def midi_clip_items(clip: AlsClip, position: float, total: float) -> list[RppItem]:
    notes: list[MidiNote] = []
    for t, seg, c0, c1 in _segments(clip, total, 1.0):
        for n in clip.notes:
            if c0 - _EPS <= n.position < c1 - _EPS:
                length = min(n.length, c1 - n.position)
                if length > _EPS:
                    notes.append(MidiNote(t + n.position - c0, n.pitch, n.velocity, length))
    return [RppItem(position=position, length=total, name=clip.name, muted=clip.disabled, notes=notes)]


def audio_clip_items(clip: AlsClip, position: float, total: float, tempo: float) -> list[RppItem]:
    k = _content_units_per_beat(clip, tempo)
    sec_per_beat = 60.0 / tempo

    def to_seconds(content: float) -> float:
        return _warp_seconds(clip, content, tempo) if clip.warped else content

    items: list[RppItem] = []
    for t, seg, c0, c1 in _segments(clip, total, k):
        s0, s1 = to_seconds(c0), to_seconds(c1)
        if s1 <= _EPS:
            continue  # entirely before the file starts -- silence
        if s0 < 0:
            # Ableton lets the start marker sit before the file's first
            # sample (leading silence); REAPER's SOFFS can't go negative,
            # so trim the item's front instead.
            frac = -s0 / (s1 - s0)
            t += seg * frac
            seg *= 1 - frac
            s0 = 0.0
        if seg <= _EPS:
            continue
        items.append(
            RppItem(
                position=position + t,
                length=seg,
                name=clip.name,
                muted=clip.disabled,
                source_path=clip.sample_path,
                source_offset=s0,
                playrate=(s1 - s0) / (seg * sec_per_beat),
                preserve_pitch=clip.preserve_pitch,
            )
        )
    return items


def clip_items(clip: AlsClip, position: float, total: float, tempo: float) -> list[RppItem]:
    if clip.kind == "MIDI":
        return midi_clip_items(clip, position, total)
    if not clip.sample_path:
        return []
    return audio_clip_items(clip, position, total, tempo)


@dataclass
class RppTrack:
    name: str
    volume: float = 1.0
    pan: float = 0.0
    muted: bool = False
    folder_flag: int = 0  # ISBUS first field: 1 opens a folder, 2 closes
    folder_delta: int = 0  # ISBUS second field: depth change after this track
    items: list[RppItem] = field(default_factory=list)
    pads: list[DrumPad] = field(default_factory=list)  # -> RS5k chain


@dataclass
class Conversion:
    text: str
    warnings: list[str]
    track_count: int
    item_count: int
    scene_count: int
    fx_report: list[FxEntry] = field(default_factory=list)
    # (source, destination) copies for `media_subdir` -- the caller does
    # the copying, once it has decided the export goes ahead.
    media_copies: list[tuple[str, str]] = field(default_factory=list)


def _bar_beats(time_signature: tuple[int, int]) -> float:
    num, den = time_signature
    return num * 4.0 / den


def _depths(tracks: list[AlsTrack]) -> list[int]:
    by_id = {t.track_id: t for t in tracks if t.kind == "GROUP"}
    depths = []
    for t in tracks:
        depth, gid, seen = 0, t.group_id, set()
        while gid in by_id and gid not in seen:
            seen.add(gid)
            depth += 1
            gid = by_id[gid].group_id
        depths.append(depth)
    return depths


def _device_report(track: AlsTrack, rt: RppTrack | None) -> list[FxEntry]:
    """Report every device on `track`; hands the first usable Drum Rack's
    pads to `rt` for the RS5k chain. `rt` None = a Return Track."""
    entries = []
    for dev in track.devices:
        if rt is None:
            entries.append(FxEntry(track.name, dev.name, False, "Return Track not converted"))
        elif dev.kind == "DrumGroupDevice" and dev.pads and not rt.pads:
            rt.pads = dev.pads
            notes = [f"-> {len(dev.pads)} RS5k instance(s)"] + dev.skipped_pads
            if any(p.choke_group for p in dev.pads):
                notes.append("choke groups not carried over (RS5k has none)")
            entries.append(FxEntry(track.name, dev.name, True, "; ".join(notes)))
        elif dev.kind == "DrumGroupDevice" and dev.pads:
            entries.append(FxEntry(track.name, dev.name, False, "only the first Drum Rack per track is converted"))
        else:
            entries.append(FxEntry(track.name, dev.name, False, "no REAPER equivalent mapped yet"))
    return entries


def _plan_media_copies(tracks: list[RppTrack], out_dir: str, media_subdir: str) -> list[tuple[str, str]]:
    """Point every audio item and RS5k pad at a copy inside
    `<out_dir>/<media_subdir>/`. Items get the RELATIVE path (REAPER
    resolves it against the project folder, like its own recorded
    media); RS5k keeps whatever path it's given verbatim, so pads get
    the absolute copy path. One copy per source file; two different
    files with the same name get numbered."""
    by_source: dict[str, str] = {}
    taken: set[str] = set()

    def dest_name(src: str) -> str:
        key = os.path.normcase(os.path.abspath(src))
        if key not in by_source:
            stem, ext = os.path.splitext(ntpath.basename(src))
            name, n = stem + ext, 1
            while name.lower() in taken:
                n += 1
                name = f"{stem} ({n}){ext}"
            taken.add(name.lower())
            by_source[key] = name
        return by_source[key]

    copies: dict[str, tuple[str, str]] = {}
    for t in tracks:
        for item in t.items:
            if item.source_path and os.path.exists(item.source_path):
                name = dest_name(item.source_path)
                copies[name] = (item.source_path, os.path.join(out_dir, media_subdir, name))
                item.source_path = ntpath.join(media_subdir, name)
        for pad in t.pads:
            if os.path.exists(pad.sample_path):
                name = dest_name(pad.sample_path)
                dst = os.path.abspath(os.path.join(out_dir, media_subdir, name))
                copies[name] = (pad.sample_path, dst)
                pad.sample_path = dst
    return list(copies.values())


def convert(
    project: AlsProject,
    session_scenes: bool = False,
    out_dir: str | None = None,
    media_subdir: str | None = None,
) -> Conversion:
    """`media_subdir` (+ `out_dir`, the .rpp's folder): gather all audio
    into that project subfolder -- see `_plan_media_copies`."""
    tempo = project.tempo
    warnings: list[str] = []
    if project.tempo_is_automated:
        warnings.append(f"tempo automation not converted -- using the starting tempo {tempo:g} bpm throughout")

    tracks = [t for t in project.tracks if t.kind != "RETURN"]
    returns = [t.name for t in project.tracks if t.kind == "RETURN"]
    if returns:
        warnings.append(f"Return Tracks not converted (sends not supported yet): {', '.join(returns)}")

    fx_report: list[FxEntry] = []
    for t in project.tracks:
        if t.kind == "RETURN":
            fx_report.extend(_device_report(t, None))
    rpp_tracks: list[RppTrack] = []
    depths = _depths(tracks)
    arrangement_end = 0.0
    for i, t in enumerate(tracks):
        delta = (depths[i + 1] if i + 1 < len(tracks) else 0) - depths[i]
        flag = 1 if delta > 0 else 2 if delta < 0 else 0
        rt = RppTrack(t.name, t.volume, t.pan, t.muted, flag, delta)
        fx_report.extend(_device_report(t, rt))
        for clip in t.arrangement_clips:
            rt.items.extend(clip_items(clip, clip.time or 0.0, clip.length or 0.0, tempo))
            arrangement_end = max(arrangement_end, (clip.time or 0.0) + (clip.length or 0.0))
            if clip.kind == "AUDIO" and not clip.sample_path:
                warnings.append(f"{t.name}: audio clip {clip.name!r} has no sample path, skipped")
        rpp_tracks.append(rt)

    missing = sorted(
        {i.source_path for t in rpp_tracks for i in t.items if i.source_path and not os.path.exists(i.source_path)}
        | {p.sample_path for t in rpp_tracks for p in t.pads if not os.path.exists(p.sample_path)}
    )
    if missing:
        warnings.append(f"{len(missing)} sample file(s) not found on disk, REAPER will show them offline: " + ", ".join(missing))

    markers = list(project.locators)
    scene_count = 0
    session_total = sum(len(t.session_clips) for t in tracks)
    if session_total and not session_scenes:
        warnings.append(f"{session_total} Session-view clip(s) ignored -- pass --scenes to lay them out as marker regions")
    if session_scenes:
        bar = _bar_beats(project.time_signature)
        cursor = math.ceil(arrangement_end / bar - _EPS) * bar + (bar if arrangement_end > _EPS else 0.0)
        scene_total = max([len(project.scene_names)] + [max(t.session_clips) + 1 for t in tracks if t.session_clips])
        for j in range(scene_total):
            row = [(k, t.session_clips[j]) for k, t in enumerate(tracks) if j in t.session_clips]
            if not row:
                continue
            region = max(natural_length(c, tempo) for _, c in row)
            region = max(bar, math.ceil(region / bar - _EPS) * bar)
            name = project.scene_names[j] if j < len(project.scene_names) and project.scene_names[j] else f"Scene {j + 1}"
            markers.append((cursor, name))
            for k, clip in row:
                length = region if clip.loop.on else min(region, natural_length(clip, tempo))
                rpp_tracks[k].items.extend(clip_items(clip, cursor, length, tempo))
            cursor += region
            scene_count += 1

    media_copies: list[tuple[str, str]] = []
    if media_subdir:
        media_copies = _plan_media_copies(rpp_tracks, out_dir or ".", media_subdir)

    text = build_rpp_text(
        tempo,
        project.time_signature,
        project.master_volume,
        project.master_pan,
        markers,
        rpp_tracks,
        record_path=media_subdir,
    )
    item_count = sum(len(t.items) for t in rpp_tracks)
    return Conversion(text, warnings, len(rpp_tracks), item_count, scene_count, fx_report, media_copies)


def _quote(s: str) -> str:
    """RPP string quoting: `"` normally, `'` or `` ` `` when the text
    itself contains double quotes (see rpp_parse._MARKER_RE)."""
    for q in ('"', "'", "`"):
        if q not in s:
            return f"{q}{s}{q}"
    return '"' + s.replace('"', "'") + '"'


_SOURCE_TYPES = {".wav": "WAVE", ".aif": "WAVE", ".aiff": "WAVE", ".mp3": "MP3", ".flac": "FLAC", ".ogg": "VORBIS"}


def _midi_source_lines(item: RppItem, indent: str) -> list[str]:
    events: list[tuple[int, int, str]] = []  # (tick, order, "status d1 d2") -- note-offs first at a tick
    for n in item.notes:
        on = round(n.position * _PPQ)
        off = max(on + 1, round((n.position + n.length) * _PPQ))
        events.append((on, 1, f"90 {n.pitch:02x} {n.velocity:02x}"))
        events.append((off, 0, f"80 {n.pitch:02x} 00"))
    events.sort()
    end = max([round(item.length * _PPQ)] + [e[0] for e in events])
    lines = [f"{indent}<SOURCE MIDI", f"{indent}  HASDATA 1 {_PPQ} QN"]
    last = 0
    for tick, _order, data in events:
        lines.append(f"{indent}  E {tick - last} {data}")
        last = tick
    # Trailing all-notes-off marks the source's end, same as REAPER writes.
    lines.append(f"{indent}  E {end - last} b0 7b 00")
    lines.append(f"{indent}>")
    return lines


def _item_lines(item: RppItem, tempo: float) -> list[str]:
    spb = 60.0 / tempo
    lines = [
        "    <ITEM",
        f"      POSITION {item.position * spb:.14g}",
        f"      LENGTH {item.length * spb:.14g}",
        "      LOOP 0",
        f"      MUTE {1 if item.muted else 0} 0",
        f"      IGUID {{{str(uuid.uuid4()).upper()}}}",
        f"      NAME {_quote(item.name)}",
        f"      SOFFS {item.source_offset:.14g}",
        f"      PLAYRATE {item.playrate:.14g} {1 if item.preserve_pitch else 0} 0 -1 0 0.0025",
        f"      GUID {{{str(uuid.uuid4()).upper()}}}",
    ]
    if item.source_path is None:
        lines += _midi_source_lines(item, "      ")
    else:
        kind = _SOURCE_TYPES.get(os.path.splitext(item.source_path)[1].lower(), "WAVE")
        lines += [f"      <SOURCE {kind}", f"        FILE {_quote(item.source_path)}", "      >"]
    lines.append("    >")
    return lines


# RS5k state template: one real REAPER-saved instance from
# tests/scenestest/scenestest.RPP. Body = `<sample path>\0` + params
# 0..15 as float64 (see rpp_parse.extract_rs5k_fields) + these 140 bytes
# of higher params, kept exactly as REAPER wrote them. Header bytes
# 32..35 hold the body's length.
_RS5K_HEADER = base64.b64decode("bW9zcu5e7f4AAAAAAgAAAAEAAAAAAAAAAgAAAAAAAACBAQAAAQAAAAAAEAA=")
_RS5K_BODY_TAIL = base64.b64decode(
    "AQAAAAAAAAAAAAAAAAAAAAAA8D9AAAAAVVVVVVVVxT//////CAQCgUAggD8AAAAAAADwPwAAAAAAAPA/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAADOpCEhGmWQPwAAAAAAAPA//Knx0k1iMD8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
_RS5K_TAIL = "AAAQAAAA"
_RS5K_DEFAULT_PARAMS = (1.0, 0.5, 0.0, 0.0, 0.0, 0.06875, 0.86875, 0.0, 0.4444444444444444, 0.0005, 0.0005, 0.0, 0.0, 0.0, 1.0, 0.5)
_RS5K_VST_LINE = (
    '<VST "VSTi: ReaSamplOmatic5000 (Cockos)" reasamplomatic.dll 0 {name} '
    '1920167789<56535472736F6D72656173616D706C6F> ""'
)
_B64_WIDTH = 128  # same wrap width REAPER uses, see rpp_parse._B64_WRAP_WIDTH


def _f32(x: float) -> float:
    # REAPER stores the note-range params at float32 precision.
    return struct.unpack("<f", struct.pack("<f", x))[0]


def rs5k_state(pad: DrumPad) -> tuple[bytes, bytes]:
    """(header, body) of an RS5k instance playing `pad` -- the inverse of
    rpp_parse.extract_rs5k_fields."""
    params = list(_RS5K_DEFAULT_PARAMS)
    params[0] = max(0.0, pad.volume)  # the on-disk value IS the linear gain
    params[1] = max(0.0, min(1.0, pad.pan / 2 + 0.5))
    params[3] = params[4] = _f32(pad.note / 127.0)
    params[11] = 1.0 if pad.classic else 0.0  # obey note-offs
    params[12] = 1.0 if pad.loop else 0.0
    params[15] = max(0.0, min(1.0, (pad.transpose + 24) / 48.0))
    body = pad.sample_path.encode("utf-8") + b"\0" + struct.pack("<16d", *params) + _RS5K_BODY_TAIL
    header = bytearray(_RS5K_HEADER)
    struct.pack_into("<I", header, 32, len(body))
    return bytes(header), body


def _b64_lines(data: bytes, indent: str) -> list[str]:
    enc = base64.b64encode(data).decode("ascii")
    return [indent + enc[i : i + _B64_WIDTH] for i in range(0, len(enc), _B64_WIDTH)]


def _fxchain_lines(pads: list[DrumPad]) -> list[str]:
    lines = ["    <FXCHAIN", "      SHOW 0", "      LASTSEL 0", "      DOCKED 0"]
    for pad in pads:
        header, body = rs5k_state(pad)
        # Each instance renamed after its sample, so the chain reads as a kit.
        vst_line = _RS5K_VST_LINE.format(name=_quote(ntpath.basename(pad.sample_path)))
        lines += ["      BYPASS 0 0 0", "      " + vst_line]
        lines += _b64_lines(header, "        ") + _b64_lines(body, "        ")
        lines += ["        " + _RS5K_TAIL, "      >"]
        lines += ["      FLOATPOS 0 0 0 0", f"      FXID {{{str(uuid.uuid4()).upper()}}}", "      WAK 0 0"]
    lines.append("    >")
    return lines


def build_rpp_text(
    tempo: float,
    time_signature: tuple[int, int],
    master_volume: float,
    master_pan: float,
    markers: list[tuple[float, str]],
    tracks: list[RppTrack],
    record_path: str | None = None,
) -> str:
    spb = 60.0 / tempo
    num, den = time_signature
    lines = ['<REAPER_PROJECT 0.1 "7.0" 0']
    if record_path:
        # New recordings land in the same folder the copies went to.
        lines.append(f'  RECORD_PATH {_quote(record_path)} ""')
    lines += [
        f"  TEMPO {tempo:.14g} {num} {den}",
        f"  MASTER_VOLUME {master_volume:.14g} {master_pan:.14g} -1 -1 1",
    ]
    for idx, (beats, name) in enumerate(sorted(markers, key=lambda m: m[0]), start=1):
        lines.append(f"  MARKER {idx} {beats * spb:.14g} {_quote(name)} 0")
    for t in tracks:
        lines += [
            f"  <TRACK {{{str(uuid.uuid4()).upper()}}}",
            f"    NAME {_quote(t.name)}",
            f"    VOLPAN {t.volume:.14g} {t.pan:.14g} -1 -1 1",
            f"    MUTESOLO {1 if t.muted else 0} 0 0",
            f"    ISBUS {t.folder_flag} {t.folder_delta}",
        ]
        if t.pads:
            lines += _fxchain_lines(t.pads)
        for item in sorted(t.items, key=lambda i: i.position):
            lines += _item_lines(item, tempo)
        lines.append("  >")
    lines.append(">")
    return "\n".join(lines) + "\n"
