"""CLI: export REAPER RS5k tracks/projects to Ableton Drum Rack (.adg) / Live Set (.als)."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import sys
from collections.abc import Callable

from .adg_builder import (
    NOISE_FADE_OUT_MS,
    build_inline_drum_group_device,
    build_pad,
    build_rack_xml,
    copy_file,
    copy_samples_locally,
    get_audio_frames,
    is_triaz_hihat_closed,
    is_triaz_hihat_open,
    is_triaz_noise_sample,
    parse_choke_pairs,
    write_adg,
)
from .als_builder import (
    AudioItemSpec,
    TrackSpec,
    build_liveset_xml,
    build_reajs_device_xml,
    build_readelay_device_xml,
    build_simpler_sine_device_xml,
    build_surge_instrument_device_xml,
    build_waves_cla_guitars_device_xml,
    build_waves_generic_effect_device_xml,
    build_waves_rverb_device_xml,
    file_size_and_crc32,
    write_als,
)
from .als_parse import parse_als
from .fx_report import FxEntry, format_report, group
from .rpp_generate import convert as rpp_convert
from .rpp_parse import (
    Track,
    list_track_fx,
    audio_item_file_path,
    extract_rs5k_fields,
    find_items,
    find_js_blocks,
    find_rs5k_blocks,
    find_vst_blocks,
    is_midi_item,
    item_chanmode,
    item_length_seconds,
    item_loop_enabled,
    item_name,
    item_pitch_semitones,
    item_playrate,
    item_position_seconds,
    item_source_offset_beats,
    item_source_offset_seconds,
    item_volume,
    midi_item_raw_length_beats,
    parse_midi_item_cc_events,
    parse_midi_item_notes,
    parse_tracks,
    project_markers,
    project_tempo,
    project_time_signature,
    track_aux_recv_entries,
    track_has_audio_item,
    track_is_return_bus,
    track_needs_midi,
    reajs_processor_state_hex,
    track_pan,
    track_pan_envelope,
    track_volume,
    track_volume_envelope,
    vst2_processor_state_hex,
    vst3_processor_state_hex,
    waves_vst3_processor_state_hex,
)
from .rs5k import param_to_pitch

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "drum_pad.xml")
_INLINE_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "drum_pad_inline.xml")
_APROJECT_ICO_PATH = os.path.join(os.path.dirname(__file__), "templates", "AProject.ico")


def _write_project_info_marker(out_dir: str) -> None:
    """Real Ableton marks a folder as a genuine Live Project (so opening
    a Set already inside one and hitting Save doesn't prompt to create a
    NEW project folder elsewhere) purely by the PRESENCE of an
    `Ableton Project Info` subfolder next to the `.als` -- confirmed by
    comparing three of the user's own real-Ableton-saved project folders
    (carrizal, chirriado surge, voleo modular): identical `AProject.ico`
    (md5-matched byte-for-byte across all three -- a fixed stock icon,
    not project-derived) and identical 3-line `Desktop.ini`. Both are
    cosmetic/inert w.r.t. the `.als` itself, safe to reproduce verbatim
    for every export -- no guessing involved, unlike the still-unsolved
    cross-machine sample `RelativePath` question (PLAN.md/AUTOMATION.md
    don't cover that, see project_cc_automation_and_sample_fixes
    memory)."""
    info_dir = os.path.join(out_dir, "Ableton Project Info")
    os.makedirs(info_dir, exist_ok=True)
    shutil.copyfile(_APROJECT_ICO_PATH, os.path.join(info_dir, "AProject.ico"))
    desktop_ini_path = os.path.join(out_dir, "Desktop.ini")
    with open(desktop_ini_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("[.ShellClassInfo]\nConfirmFileOp=0\nNoSharing=0\n")

# Destination for quick test exports (--test / the GUI's Quick export).
# Handy to point at a folder added to Ableton's browser Places, which
# picks up new files live. Set RPP_TO_ALS_TEST_DIR to choose it.
_LIVETEST_DIR = os.environ.get("RPP_TO_ALS_TEST_DIR") or os.path.join(
    os.path.expanduser("~"), "Documents", "rpp-to-als test exports"
)
_LIVETEST_RPP_DIR = os.path.join(_LIVETEST_DIR, "rpp-reverse")

# export-rpp --copy-media's default subfolder -- what the user's own
# REAPER projects use as their media/record path.
DEFAULT_MEDIA_SUBDIR = "mediaFiles"

_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def _safe_filename(name: str) -> str:
    return _UNSAFE_FILENAME_RE.sub("_", name)


def _select_track(tracks: list[Track], selector: str) -> Track:
    if selector.isdigit():
        idx = int(selector)
        if 0 <= idx < len(tracks):
            return tracks[idx]
        raise SystemExit(f"Track index {idx} out of range (0..{len(tracks) - 1})")
    for t in tracks:
        if t.name == selector:
            return t
    names = ", ".join(f"{i}:{t.name!r}" for i, t in enumerate(tracks))
    raise SystemExit(f"No track named {selector!r}. Available: {names}")


def _build_pads_for_track(track: Track, choke: str | None = None):
    """Shared by `export` (standalone .adg) and `export-project` (embedded
    in a .als track). Returns (pads, warnings, choke_group_by_note)."""
    blocks = find_rs5k_blocks(track.chunk)
    pads = []
    pad_fields = []  # parallel to pads, for choke-group detection after the loop
    warnings = []
    for block in blocks:
        fields = extract_rs5k_fields(block)
        if fields is None:
            continue
        if fields.note_lo != fields.note_hi:
            warnings.append(f"{fields.wav_path}: multi-note/zone instance, v1 only supports single-note pads, skipped")
            continue
        pitch_st = param_to_pitch(fields.pitch_st_param)
        is_noise = is_triaz_noise_sample(fields.wav_path)
        pad = build_pad(
            note=fields.note_lo,
            wav_path=fields.wav_path,
            volume=fields.volume,
            pan_normalized=fields.pan,
            pitch_st=pitch_st,
            pad_name=fields.wav_path.rsplit("\\", 1)[-1],
            sustain_mode=1 if is_noise else 0,
            fade_out_ms=NOISE_FADE_OUT_MS if is_noise else 0.1,
            loop_on=is_noise,
        )
        if pad is None:
            warnings.append(f"{fields.wav_path}: could not read WAV header, skipped")
            continue
        pads.append(pad)
        pad_fields.append(fields)

    if choke:
        choke_group_by_note = parse_choke_pairs(choke)
    else:
        # Auto-default: choke every HiHat Closed pad against every HiHat
        # Open pad (all mutually in one group), matching the Lua tool's
        # "defaults to the kit's Hihat Closed/HiHat Open notes if present"
        # behavior, generalized to however many closed/open pads exist.
        hh_notes = [
            p.note
            for p, f in zip(pads, pad_fields)
            if is_triaz_hihat_closed(f.wav_path) or is_triaz_hihat_open(f.wav_path)
        ]
        choke_group_by_note = {n: 1 for n in hh_notes} if hh_notes else {}

    # HH-open pads without a choke group have no other way to be cut off
    # (matches factory content: PlaybackMode=1/SustainMode=0, choke is the
    # entire mechanism -- see ADG_EXPORT_PLAN.md "obey_note_off handling").
    for pad, fields in zip(pads, pad_fields):
        if (
            is_triaz_hihat_open(fields.wav_path)
            and fields.obey_note_off
            and pad.note not in choke_group_by_note
        ):
            warnings.append(f"{fields.wav_path}: obey-note-off HH-open pad has no choke group, will play to completion")

    return pads, warnings, choke_group_by_note


def export_track(
    rpp_path: str,
    track_selector: str,
    output_path: str | None,
    rack_name: str | None = None,
    *,
    test: bool = False,
    choke: str | None = None,
    copy_samples: bool = False,
) -> None:
    text = open(rpp_path, encoding="utf-8").read()
    time_signature = project_time_signature(text) or (4, 4)
    tracks = parse_tracks(text)
    track = _select_track(tracks, track_selector)
    final_rack_name = rack_name or track.name

    if test or output_path is None:
        output_path = os.path.join(_LIVETEST_DIR, _safe_filename(final_rack_name) + ".adg")

    pads, warnings, choke_group_by_note = _build_pads_for_track(track, choke)
    if not pads:
        raise SystemExit(f"No exportable RS5k instances found on track {track.name!r}.")

    if copy_samples:
        copy_samples_locally(pads, os.path.dirname(os.path.abspath(output_path)))

    template = open(_TEMPLATE_PATH, encoding="utf-8").read()
    xml = build_rack_xml(template, final_rack_name, pads, choke_group_by_note=choke_group_by_note, time_signature=time_signature)
    write_adg(xml, output_path)

    print(f"Exported {len(pads)} pad(s) to: {output_path}")
    for w in warnings:
        print(f"  warning: {w}")


def _arrangement_midi_clips_for_track(track_chunk: str, tempo: float) -> list[tuple[list, float, float, float, dict]]:
    """Every MIDI item on a track, as [(notes, start_beats, length_beats,
    loop_length_beats, cc_events), ...], each placed at its own real project-timeline
    position via Arrangement-view clips (see als_builder.build_track_block
    / TrackSpec.arrangement_midi_clips, PLAN.md "Arrangement-view clip
    placement") -- unlike the earlier Session-view scene-slot approach,
    there's no fixed capacity, so nothing gets dropped.

    A REAPER MIDI item's note ticks are stored unaffected by PLAYRATE --
    at rate < 1 the same notes take proportionally longer to actually
    play, so position/length must be scaled by `1 / playrate` to land
    correctly on the project's own beat timeline (see PLAN.md "Item
    PLAYRATE + LOOP"). When `LOOP 1` is set, the item's displayed LENGTH
    can be longer than the source content's own natural length --
    REAPER tiles/repeats it to fill the gap (not necessarily a whole
    number of repeats; loop-dragging to an arbitrary length is normal).
    `loop_length_beats` is the single-repeat length so the generated
    Ableton clip's own Loop region can reproduce the same tiling; equals
    `length_beats` (i.e. no visible loop) for a non-looping item. The
    cycle length comes from `midi_item_raw_length_beats` (the position of
    the take's LAST event, of any type), not from the last NOTE's end --
    a pattern's last note commonly ends shortly before the true loop
    point (a trailing rest), so using note data alone made the loop
    restart audibly early once scaled to project beats.

    `SOFFS`'s beats field (`item_source_offset_beats`) is subtracted from
    every note position -- and from the raw loop-cycle length -- before
    the playrate scaling above, since it's in the same raw tick/ppq
    coordinate space. A nonzero SOFFS means the item's actual playback
    starts partway into the raw MIDI take (common with pre-roll
    recording: a leading stretch of silence before the real performance
    that REAPER trims via SOFFS rather than removing from the take
    outright) -- exporting from raw tick 0 instead played that leading
    silence as real clip content, shifting everything later by roughly
    the offset. See PLAN.md "Item SOFFS (source offset / pre-roll)".

    Each tuple's 5th element is {cc_number: [MidiCCEvent, ...]} -- every
    Control Change lane recorded on the item, positions put through the
    exact same SOFFS-subtract/pre-roll-clip/playrate-scale pipeline as
    note positions above (same raw tick/ppq coordinate space, see
    AUTOMATION.md "MIDI CC automation")."""
    midi_items = [item for item in find_items(track_chunk) if is_midi_item(item)]
    clips = []
    for item in midi_items:
        notes = parse_midi_item_notes(item)
        cc_raw_events = parse_midi_item_cc_events(item)
        playrate = item_playrate(item)
        source_offset_beats = item_source_offset_beats(item)
        pitch_shift = round(item_pitch_semitones(item))
        if pitch_shift:
            for note in notes:
                note.pitch = min(127, max(0, note.pitch + pitch_shift))
        for note in notes:
            note.position -= source_offset_beats
        # A note that starts slightly before SOFFS but whose sustain
        # crosses into the visible item (position + length > 0) still
        # needs to sound -- REAPER clips it to the item boundary rather
        # than dropping it outright. Found via a real audible bug: a
        # kick note on a swing-grid position (raw 5.99167 beats, a
        # 1/12-beat subdivision) landed ~0.008 beats before this item's
        # SOFFS (5.99999999992835) -- a genuine swing-timing offset, not
        # float noise -- but its 0.0708-beat length carried it 0.0625
        # beats PAST the boundary, so REAPER still plays it at the
        # item's very start. The old unconditional `position >= 0`
        # filter dropped it (and a hi-hat hit in the same spot)
        # entirely, silencing REAPER's own first downbeat on reload.
        # Only genuinely pre-roll notes (fully decayed before the
        # boundary, position + length <= 0) get dropped.
        clipped = []
        for note in notes:
            if note.position + note.length <= 0:
                continue
            if note.position < 0:
                note.length += note.position
                note.position = 0.0
            clipped.append(note)
        notes = clipped
        for note in notes:
            note.position /= playrate
            note.length /= playrate
        # Same SOFFS-subtract/pre-roll-drop/playrate-scale pipeline as
        # notes above -- a CC event has no length, so "clip to boundary"
        # collapses to a plain drop for anything still before the item's
        # real start.
        for cc in cc_raw_events:
            cc.position -= source_offset_beats
        cc_raw_events = [cc for cc in cc_raw_events if cc.position >= 0]
        for cc in cc_raw_events:
            cc.position /= playrate
        cc_events: dict[int, list] = {}
        for cc in cc_raw_events:
            cc_events.setdefault(cc.cc_number, []).append(cc)
        position_seconds = item_position_seconds(item) or 0.0
        length_seconds = item_length_seconds(item)
        start_beats = position_seconds * tempo / 60.0
        length_beats = (length_seconds * tempo / 60.0) if length_seconds else 4.0
        loop_length_beats = length_beats
        if item_loop_enabled(item):
            raw_cycle_beats = midi_item_raw_length_beats(item)
            if raw_cycle_beats:
                cycle_beats = (raw_cycle_beats - source_offset_beats) / playrate
                if 0 < cycle_beats < length_beats:
                    loop_length_beats = cycle_beats
        clips.append((notes, start_beats, length_beats, loop_length_beats, cc_events))
    return clips


_CHANMODE_LABELS = {1: "swap", 2: "mono", 3: "L", 4: "R"}


def _chanmode_flag(track_chunk: str) -> str:
    """Ableton has no per-clip equivalent of REAPER's item `CHANMODE`
    (see PLAN.md "Item CHANMODE not carried over") -- rather than
    silently exporting a mono-downmix/L-only/R-only item as plain
    stereo, flag it in the track name (e.g. " [mono]") so it's obvious
    in Ableton that item(s) on this track need a Utility device (or a
    re-glue in REAPER) to reproduce the original channel mode. Empty
    string if every item on the track is CHANMODE 0 (normal)."""
    labels = {_CHANMODE_LABELS[m] for item in find_items(track_chunk) if (m := item_chanmode(item)) in _CHANMODE_LABELS}
    return f" [{'/'.join(sorted(labels))}]" if labels else ""


def _arrangement_audio_clips_for_track(track_chunk: str, tempo: float, project_dir: str | None = None) -> list[AudioItemSpec]:
    """Every audio item on a track, as an AudioItemSpec each -- see
    als_builder.AudioItemSpec / build_audio_clip_xml / TrackSpec.
    arrangement_audio_clips. Each item becomes its own AudioClip; the same
    source file placed
    multiple times as separate REAPER items (a normal REAPER pattern)
    just becomes multiple separate AudioClips too, not merged/detected --
    simplest correct approach, no behavioral difference in Ableton either
    way.

    REAPER's audio-item POSITION/LENGTH are already in seconds (unlike a
    MIDI item's tick-based note data), so Arrangement placement
    (start_beats/length_beats) only needs the straightforward
    `beats = seconds * (tempo / 60)` conversion, no playrate/tick-scaling
    step like the MIDI path -- `playrate` (REAPER item PLAYRATE, first
    field) is passed through as-is instead, since build_audio_clip_xml
    needs the raw ratio itself to build a correct warped clip when it's
    not 1.0, not a pre-scaled position.

    `SOFFS <seconds>` (audio's single-field variant, see
    rpp_parse.item_source_offset_seconds -- distinct from a MIDI item's
    two-field `SOFFS <seconds> <beats>`) is REAPER's `OFFSET`: how far into
    the source WAV file playback/looping actually starts. UNLIKE
    start_beats/length_beats, this is passed through as plain seconds, NOT
    converted to beats -- see build_audio_clip_xml's docstring for why an
    unwarped clip's own `Loop` region is a different (seconds, not
    tempo-scaled beats) coordinate space than its Arrangement placement; an
    earlier version of this function wrongly scaled it by `tempo / 60` and
    made looped/offset clips audibly play too fast on reload. When
    `LOOP 1` is set, the remaining file duration past that offset (source
    file's own natural duration minus the offset) is the single-cycle loop
    length -- `None` (no loop region) for a non-looping item, matching
    build_audio_clip_xml's `loop_length_seconds` convention. Items whose
    source file can't be read (missing/corrupt/unsupported) are skipped --
    see adg_builder.get_audio_frames for which formats are supported
    (WAV/MP3/FLAC/OGG/AIFF, matching rpp_parse.track_has_audio_item).

    `project_dir` resolves a RELATIVE `FILE` path (REAPER's own "save
    media with relative paths" project setting -- common for media
    recorded/rendered straight into the project's own media subfolders,
    e.g. `piano chunks\accomp 1.wav`) against the .RPP file's own
    directory. An RS5k instrument's own sample path is embedded straight
    in its VST param blob and REAPER always writes that one absolute
    regardless of this project setting, so only THIS path (audio items'
    `<SOURCE ... FILE>`) needs it -- found via a real project
    (voleoModular.RPP) where relative-path audio items were silently
    skipped (get_audio_frames failing to open a path relative to the
    CLI's own cwd, not the project's), while the same project's RS5k
    samples (absolute paths already) copied fine."""
    audio_items = [item for item in find_items(track_chunk) if not is_midi_item(item) and audio_item_file_path(item)]
    clips = []
    for item in audio_items:
        wav_path = audio_item_file_path(item)
        if project_dir and not os.path.isabs(wav_path):
            wav_path = os.path.normpath(os.path.join(project_dir, wav_path))
        wav_info = get_audio_frames(wav_path)
        if wav_info is None:
            continue
        frames, sample_rate = wav_info
        duration_seconds = frames / sample_rate if sample_rate else 0.0
        file_size, file_crc = file_size_and_crc32(wav_path)

        position_seconds = item_position_seconds(item) or 0.0
        length_seconds = item_length_seconds(item) or duration_seconds
        start_beats = position_seconds * tempo / 60.0
        length_beats = length_seconds * tempo / 60.0

        offset_seconds = item_source_offset_seconds(item)

        loop_length_seconds = None
        if item_loop_enabled(item):
            remaining_seconds = duration_seconds - offset_seconds
            if remaining_seconds > 0:
                loop_length_seconds = remaining_seconds

        name = item_name(item) or wav_path.rsplit("\\", 1)[-1]
        playrate = item_playrate(item)
        clips.append(
            AudioItemSpec(
                wav_path=wav_path,
                name=name,
                start_beats=start_beats,
                length_beats=length_beats,
                duration_frames=frames,
                sample_rate=sample_rate,
                loop_offset_seconds=offset_seconds,
                loop_length_seconds=loop_length_seconds,
                playrate=playrate,
                volume=item_volume(item),
                pitch_semitones=item_pitch_semitones(item),
                original_file_size=file_size,
                original_crc=file_crc,
            )
        )
    return clips


def _bundle_audio_clip_samples(
    clips: list[AudioItemSpec],
    dest_dir: str,
    copied: dict[str, str],
    copy: Callable[[str, str], None] = copy_file,
) -> None:
    """Copy each clip's source WAV into `<dest_dir>/Samples/Imported`
    (Ableton's own "collect and save" convention) and repoint
    `clip.wav_path` at the copy -- sibling to
    `adg_builder.copy_samples_locally` (same dedupe-by-source-path idea,
    `copied` shared across every track's call in one export so the same
    file used by multiple items/tracks is only copied once). Mutates
    `clips` in place; call before `build_liveset_xml`. Gated by
    `export_project`'s `copy_samples` flag, same switch as the existing
    (until now, RS5k-only) local-sample bundling.

    `dest_dir` is always the `.als` file's own directory (`export_project`'s
    `out_dir`), so `clip.relative_path` is always exactly
    `Samples/Imported/<name>` -- also set here so build_audio_clip_xml can
    write a real `RelativePathType="1"` FileRef, letting the sample
    resolve correctly even if the whole exported folder is moved to a
    different machine/location, not just the one it was exported on.

    `copy` does the physical file copy -- see copy_samples_locally."""
    dest = os.path.join(dest_dir, "Samples", "Imported")
    for clip in clips:
        if clip.wav_path not in copied:
            name = clip.wav_path.replace("/", "\\").rsplit("\\", 1)[-1]
            dst = os.path.join(dest, name)
            copy(clip.wav_path, dst)
            copied[clip.wav_path] = dst
        clip.wav_path = copied[clip.wav_path]
        clip.relative_path = "Samples/Imported/" + clip.wav_path.replace("/", "\\").rsplit("\\", 1)[-1]


# Recognized Waves plugins -> (builder, label). Add an entry here (plus
# a builder in als_builder.py + a templates/vst3_waves_*.xml extracted
# the same way as RVerb/CLA Guitars, see PLAN.md "FX/device matching
# research") to support another Waves plugin. Matched by substring
# against the RawVstBlock's own display name, checked in order --
# unrecognized/unsupported plugins (other vendors, or a Waves plugin not
# in this table yet, e.g. CLA Unplugged/CLA Bass as of writing) are
# silently skipped, same "no transplant shortcut available yet" scope
# discipline as everywhere else in this file.
_WAVES_DEVICE_BUILDERS = (
    ("RVerb", build_waves_rverb_device_xml, "RVerb"),
    ("CLA Guitars", build_waves_cla_guitars_device_xml, "CLA Guitars"),
)


def _waves_generic_plugin_name(block_name: str) -> str:
    """`"VST3: CLA Bass Stereo (Waves)"` -> `"CLA Bass Stereo"`, matching
    the plain-name convention Ableton's own `<Name>` field uses
    (confirmed against real RVerb/CLA Guitars reference files: `"RVerb
    Mono/Stereo"`, `"CLA Guitars Mono/Stereo"` -- no `"VST3: "` prefix,
    no `"(Waves)"` suffix)."""
    name = block_name.split(": ", 1)[-1]
    return name.removesuffix(" (Waves)").strip()


def _waves_effect_devices_for_track(
    track_chunk: str, track_name: str, warnings: list[str], converted: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Every recognized Waves effect on a track, as parallel
    (device_xml, label) lists in REAPER FX-chain order (`find_vst_blocks`
    iterates the chunk in document order, which matches REAPER's own
    FX-chain processing order) -- see als_builder._embed_device's
    multi-device chaining.

    Plugins in `_WAVES_DEVICE_BUILDERS` (with a real reference-extracted
    template) take priority; any OTHER Waves VST3 plugin with a known
    GUID falls back to `als_builder.build_waves_generic_effect_device_xml`
    -- reload-confirmed working in real Ableton (2026-07-25, CLA
    Unplugged + CLA Bass) -- see that function's docstring for the
    structural-identity reasoning it relies on.

    `converted` collects the raw FX name of each block turned into a
    device, for the FX report."""
    devices: list[str] = []
    labels: list[str] = []
    if converted is None:
        converted = []
    for block in find_vst_blocks(track_chunk):
        if "Waves" not in block.name or not block.is_vst3:
            continue
        for name_substr, builder, label in _WAVES_DEVICE_BUILDERS:
            if name_substr in block.name:
                state_hex = waves_vst3_processor_state_hex(block)
                if state_hex:
                    devices.append(builder(state_hex))
                    labels.append(label)
                    converted.append(block.name)
                else:
                    warnings.append(f"{track_name}: Waves {label} instance found but its state isn't the expected setA format, skipped")
                break
        else:
            if not block.guid_hex:
                continue
            state_hex = waves_vst3_processor_state_hex(block)
            if not state_hex:
                continue
            plugin_name = _waves_generic_plugin_name(block.name)
            uid_fields = tuple(struct.unpack(">i", bytes.fromhex(block.guid_hex)[i : i + 4])[0] for i in (0, 4, 8, 12))
            branch_device_id = "-".join(
                [block.guid_hex[0:8], block.guid_hex[8:12], block.guid_hex[12:16], block.guid_hex[16:20], block.guid_hex[20:32]]
            ).lower()
            devices.append(build_waves_generic_effect_device_xml(state_hex, plugin_name, uid_fields, branch_device_id))
            labels.append(plugin_name)
            converted.append(block.name)
    return devices, labels


# Recognized native ReaPlugs VST2 effects -> (builder, label). Each ReaPlugs
# effect is its own distinct VST2 binary with its own parameter layout (no
# shared wrapper the way Waves' WaveShell generalizes) -- unlike the Waves
# case, there's no generic fallback here; a plugin not in this table is
# skipped with a warning naming it, so it's visible which ones still need a
# reference-extracted template (add one the same way vst2_readelay_device.xml
# was built -- load the plugin in Ableton with any preset, save, extract).
# ReaSamplOmatic5000 (RS5k) is deliberately excluded -- handled separately by
# adg_builder's Drum Rack conversion, not this effects path.
_REAPLUGS_DEVICE_BUILDERS = (("ReaDelay", build_readelay_device_xml, "ReaDelay"),)


def _reaplugs_plugin_display_name(block_name: str) -> str:
    """`"VST: ReaDelay (Cockos)"` -> `"ReaDelay"`, matching the `"VST:
    "`/`"VSTi: "` prefix and `" (Cockos)"` vendor suffix convention
    REAPER uses for native ReaPlugs effects (same shape as
    `_waves_generic_plugin_name`, different vendor tag)."""
    name = block_name.split(": ", 1)[-1]
    return name.removesuffix(" (Cockos)").strip()


def _reaplugs_effect_devices_for_track(
    track_chunk: str, track_name: str, warnings: list[str], converted: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Every recognized Rea*/JS FX effect on a track, as parallel
    (device_xml, label) lists in REAPER FX-chain order -- sibling to
    `_waves_effect_devices_for_track`. JS FX (`<JS ...>` blocks, a REAPER
    JSFX instance) all route through the single generic `reajs` template
    (ReaJS exposes a fixed 64-slot interface regardless of which .jsfx
    script is loaded -- see `build_reajs_device_xml`); native Rea* VST2
    effects need a per-plugin template from `_REAPLUGS_DEVICE_BUILDERS`.
    No source-file bundling for JS FX -- ReaJS has to be installed/
    registered in Ableton on the receiving machine regardless, same as
    any other Rea* plugin here, so copying the `.jsfx` script alongside
    the project adds no real portability on its own (see PLAN.md "JSFX
    + ReaPlugs export" for the earlier bundling attempt and why it was
    dropped). `converted`: same as in `_waves_effect_devices_for_track`."""
    devices: list[str] = []
    labels: list[str] = []
    if converted is None:
        converted = []
    for block in find_vst_blocks(track_chunk):
        # Instruments ("VSTi: ...", e.g. RS5k, ReaSynth) have their own
        # paths in export_project -- this one is effects only.
        if block.is_vst3 or block.name.startswith("VSTi"):
            continue
        plugin_name = _reaplugs_plugin_display_name(block.name)
        if not plugin_name.startswith("Rea"):
            continue
        for name_substr, builder, label in _REAPLUGS_DEVICE_BUILDERS:
            if name_substr in block.name:
                state_hex = vst2_processor_state_hex(block)
                if state_hex:
                    devices.append(builder(state_hex))
                    labels.append(label)
                    converted.append(block.name)
                else:
                    warnings.append(f"{track_name}: {label} instance found but its state isn't in the expected 3-field format, skipped")
                break
        else:
            warnings.append(f"{track_name}: Rea* effect '{plugin_name}' found but has no reference-extracted template yet, skipped")
    for js in find_js_blocks(track_chunk):
        state_hex = reajs_processor_state_hex(js.name, js.params)
        devices.append(build_reajs_device_xml(state_hex))
        labels.append(f"ReaJS ({js.name})")
        converted.append(f"JS: {js.name}")
    return devices, labels


def _forward_fx_report(track_name: str, track_chunk: str, converted: list[str], notes: dict[str, str]) -> list[FxEntry]:
    """Every FX on the track, marked converted when a matcher consumed
    one instance with that name (`converted`, a multiset). `notes`:
    per-FX-name remark for converted entries, e.g. what it became."""
    remaining = list(converted)
    entries = []
    for fx in list_track_fx(track_chunk):
        if fx in remaining:
            remaining.remove(fx)
            entries.append(FxEntry(track_name, fx, True, notes.get(fx, "")))
        elif any(k in fx for k in _MATCHED_FX_MARKERS):
            entries.append(FxEntry(track_name, fx, False, "skipped -- see warnings"))
        else:
            entries.append(FxEntry(track_name, fx, False, "no Ableton equivalent mapped yet"))
    return group(entries)


# FX some matcher handles -- an unconverted instance of one was skipped
# for a reason (a warning says which), not simply unmapped.
RS5K_FX_NAME = "VSTi: ReaSamplOmatic5000 (Cockos)"
_MATCHED_FX_MARKERS = ("ReaSamplOmatic5000", "Surge XT", "ReaSynth", "(Waves)", "JS: ") + tuple(
    s for s, _b, _l in _REAPLUGS_DEVICE_BUILDERS
)


def export_project(
    rpp_path: str,
    output_path: str | None,
    *,
    test: bool = False,
    copy_samples: bool = False,
    export_standalone_adg: bool = False,
    scenes: bool = False,
) -> list[FxEntry]:
    """Track shells (name, MIDI-vs-Audio type, tempo), an Arrangement-view
    MIDI clip per MIDI item and an Arrangement-view AudioClip per audio item
    at their real project-timeline positions, and for any track with RS5k
    instances, an embedded Drum Rack device -- see als_builder.py module
    docstring for what's still not implemented (routing) -- track color
    intentionally not implemented, see PLAN.md.

    `scenes` -- every REAPER project marker becomes both a Locator (as
    normal) AND a Session-view Scene with the marker's own name, matching
    how a real Ableton project keeps Locators/Scenes in 1:1 name/order
    sync (confirmed against the Ableton factory demo song, see PLAN.md
    "Scene export"). Each marker-to-next-marker region is one Scene; a
    track's own item is placed in that Scene's ClipSlot if the item
    starts within the region. Requires at least one marker (hard error
    otherwise) and at most one item per track per Scene (hard error on a
    track with 2+ items in the same region) -- replaces the normal
    item-position-based Session-view mirroring entirely when given.

    `export_standalone_adg` additionally writes each Drum Rack track's own
    `<name>.adg` next to the `.als` (browser-importable/reusable elsewhere in
    Ableton) -- OFF by default, since the `.als` embeds the Drum Rack device
    inline already (confirmed working on reload) and doesn't need the loose
    `.adg` to load correctly; it's a pure bonus-export, not a dependency.
    User noticed the loose `.adg` files as clutter (2026-07-25) before this
    flag existed -- see PLAN.md "Item pitch adjust" follow-up."""
    text = open(rpp_path, encoding="utf-8").read()
    project_dir = os.path.dirname(os.path.abspath(rpp_path))
    tempo = project_tempo(text) or 120.0
    time_signature = project_time_signature(text) or (4, 4)
    tracks = parse_tracks(text)
    # Checked up front, before any sample copying writes into out_dir.
    if scenes and not project_markers(text):
        raise SystemExit("--scenes requires at least one project marker, none found.")

    if output_path is None:
        base = os.path.splitext(os.path.basename(rpp_path))[0]
        output_path = os.path.join(_LIVETEST_DIR, _safe_filename(base) + ".als") if test else base + ".als"
    out_dir = os.path.dirname(os.path.abspath(output_path))

    template = open(_TEMPLATE_PATH, encoding="utf-8").read()
    inline_template = open(_INLINE_TEMPLATE_PATH, encoding="utf-8").read()

    # Ableton has no equivalent of a REAPER track holding both MIDI and
    # audio items -- a track is strictly one or the other. Split any
    # mixed REAPER track into two Ableton tracks rather than silently
    # dropping whichever kind loses.
    # Sends: a REAPER track is "really" an aux-send bus/Return Track
    # equivalent if another track's AUXRECV points at it -- detected
    # structurally, not by naming convention (see rpp_parse.
    # track_is_return_bus, AUTOMATION.md "Sends"). Bus tracks are pulled
    # out of the normal per-track export loop entirely and instead become
    # Ableton Return Tracks (build_liveset_xml's return_tracks); every
    # OTHER track gets a TrackSpec.sends entry per bus, positional to
    # match return_tracks' order.
    bus_indices = [i for i, t in enumerate(tracks) if track_is_return_bus(t.chunk)]
    send_levels_by_source: dict[int, list[float | None]] = {}
    for bus_pos, bus_idx in enumerate(bus_indices):
        for src_idx, vol, _pan in track_aux_recv_entries(tracks[bus_idx].chunk):
            levels = send_levels_by_source.setdefault(src_idx, [None] * len(bus_indices))
            levels[bus_pos] = vol

    track_specs: list[TrackSpec] = []
    device_labels: list[str | None] = []
    all_warnings: list[str] = []
    fx_report: list[FxEntry] = []
    copied_audio_samples: dict[str, str] = {}
    # Every file write into out_dir (sample copies, standalone .adgs) is
    # queued and only run once the whole export has validated -- a
    # --scenes error otherwise left a half-populated export folder.
    pending_writes: list[Callable[[], None]] = []

    def queue_copy(src: str, dst: str) -> None:
        pending_writes.append(lambda: copy_file(src, dst))

    return_track_specs: list[tuple[str, str | list[str] | None]] = []
    for bus_idx in bus_indices:
        bus = tracks[bus_idx]
        bus_converted: list[str] = []
        bus_effects, _bus_effect_labels = _waves_effect_devices_for_track(bus.chunk, bus.name, all_warnings, bus_converted)
        bus_reaplugs, _bus_reaplugs_labels = _reaplugs_effect_devices_for_track(
            bus.chunk, bus.name, all_warnings, bus_converted
        )
        bus_effects += bus_reaplugs
        fx_report += _forward_fx_report(bus.name, bus.chunk, bus_converted, {})
        return_track_specs.append((bus.name, bus_effects or None))

    for i, t in enumerate(tracks):
        if i in bus_indices:
            continue
        has_midi = track_needs_midi(t.chunk)
        has_audio = track_has_audio_item(t.chunk)
        rs5k_blocks = find_rs5k_blocks(t.chunk)

        device_xml = None
        device_label = None
        converted: list[str] = []
        fx_notes: dict[str, str] = {}
        midi_name = f"{t.name} (MIDI)" if (has_midi and has_audio) else t.name
        if rs5k_blocks:
            pads, warnings, choke_group_by_note = _build_pads_for_track(t)
            all_warnings.extend(f"{t.name}: {w}" for w in warnings)
            if pads:
                if copy_samples:
                    copy_samples_locally(
                        pads,
                        os.path.join(out_dir, "Samples", "Imported"),
                        relative_prefix="Samples/Imported/",
                        copy=queue_copy,
                    )
                if export_standalone_adg:
                    # Standalone .adg still uses the preset-format builder
                    # (BranchPresets/DrumBranchPreset) -- confirmed working
                    # in real Ableton, unaffected by the inline-embed fix
                    # below.
                    rack_xml = build_rack_xml(
                        template, t.name, pads, choke_group_by_note=choke_group_by_note, time_signature=time_signature
                    )
                    adg_path = os.path.join(out_dir, _safe_filename(t.name) + ".adg")
                    pending_writes.append(lambda x=rack_xml, p=adg_path: write_adg(x, p))
                # The .als-embedded device needs the INLINE format
                # (DrumBranch, not DrumBranchPreset) -- a preset's empty
                # <Branches /> only gets hydrated by Ableton when loading a
                # .adg from the browser, not for a device already inline in
                # a track's DeviceChain. See PLAN.md.
                device_xml = build_inline_drum_group_device(
                    template,
                    inline_template,
                    t.name,
                    pads,
                    choke_group_by_note=choke_group_by_note,
                    time_signature=time_signature,
                )
                device_label = "Drum Rack"
                converted += [RS5K_FX_NAME] * len(pads)
                fx_notes[RS5K_FX_NAME] = "-> Drum Rack pad"
        else:
            surge_blocks = [b for b in find_vst_blocks(t.chunk) if "Surge XT" in b.name]
            if surge_blocks:
                state_hex = vst3_processor_state_hex(surge_blocks[0])
                if state_hex:
                    device_xml = build_surge_instrument_device_xml(state_hex)
                    device_label = "Surge XT"
                    converted.append(surge_blocks[0].name)
                else:
                    all_warnings.append(f"{t.name}: Surge XT instance found but its state isn't the expected JUCE/sub3 format, skipped")
            else:
                reasynth = [b for b in find_vst_blocks(t.chunk) if "ReaSynth" in b.name]
                if reasynth and has_midi:
                    device_xml = build_simpler_sine_device_xml()
                    device_label = "Simpler (sine)"
                    converted.append(reasynth[0].name)
                    fx_notes[reasynth[0].name] = "-> plain sine Simpler stand-in, settings not carried over"

        # Waves effects go wherever the track's real audio content lives:
        # a REAPER track's own single FX chain applies to everything
        # passing through it -- both its instrument's audio output and
        # any raw audio items on the same track get mixed and go through
        # this SAME chain. Since Ableton has no equivalent of one track
        # holding both MIDI and audio, a split track (has_midi AND
        # has_audio) becomes two separate Ableton tracks; carrying the
        # original single FX chain over means attaching a COPY of it to
        # BOTH resulting tracks (each gets its own independently
        # renumbered device xml via build_track_block's own id_offset,
        # so reusing the same device_xml strings on two different
        # TrackSpecs is safe -- no id collision, they never share a
        # <Devices> list). MIDI side chains after the instrument device
        # if any (Ableton natively supports Instrument -> Effect(s) on
        # one track's own DeviceChain, same multi-device mechanism
        # already used for tp's CLA Guitars+RVerb chain).
        effect_devices, effect_labels = _waves_effect_devices_for_track(t.chunk, t.name, all_warnings, converted)
        reaplugs_devices, reaplugs_labels = _reaplugs_effect_devices_for_track(t.chunk, t.name, all_warnings, converted)
        fx_report += _forward_fx_report(t.name, t.chunk, converted, fx_notes)
        effect_devices += reaplugs_devices
        effect_labels += reaplugs_labels
        audio_devices: list[str] | None = None
        audio_device_label = None
        if has_audio:
            audio_devices = effect_devices or None
            audio_device_label = "+".join(effect_labels) if effect_labels else None
        if has_midi and effect_devices:
            combined = ([device_xml] if device_xml else []) + effect_devices
            device_xml = combined
            device_label = "+".join(([device_label] if device_label else []) + effect_labels)

        arrangement_midi_clips = None
        if has_midi:
            arrangement_midi_clips = _arrangement_midi_clips_for_track(t.chunk, tempo) or None

        arrangement_audio_clips = None
        chanmode_flag = ""
        if has_audio:
            arrangement_audio_clips = _arrangement_audio_clips_for_track(t.chunk, tempo, project_dir) or None
            if arrangement_audio_clips and copy_samples:
                _bundle_audio_clip_samples(arrangement_audio_clips, out_dir, copied_audio_samples, copy=queue_copy)
            chanmode_flag = _chanmode_flag(t.chunk)

        # REAPER's single VOLPAN fader applies once, before any
        # MIDI+audio split -- both halves of a split track get the same
        # value, since either could plausibly carry it and there's no
        # more-correct way to divide one fader between two Ableton
        # tracks.
        vol = track_volume(t.chunk)
        pan = track_pan(t.chunk)
        vol_automation = track_volume_envelope(t.chunk)
        pan_automation = track_pan_envelope(t.chunk)
        sends = send_levels_by_source.get(i)

        if has_midi and has_audio:
            track_specs.append(
                TrackSpec(
                    "MIDI",
                    midi_name,
                    device_xml,
                    arrangement_midi_clips=arrangement_midi_clips,
                    volume=vol,
                    pan=pan,
                    volume_automation=vol_automation,
                    pan_automation=pan_automation,
                    sends=sends,
                )
            )
            device_labels.append(device_label)
            track_specs.append(
                TrackSpec(
                    "Audio",
                    f"{t.name} (Audio){chanmode_flag}",
                    audio_devices,
                    arrangement_audio_clips=arrangement_audio_clips,
                    volume=vol,
                    pan=pan,
                    volume_automation=vol_automation,
                    pan_automation=pan_automation,
                    sends=sends,
                )
            )
            device_labels.append(audio_device_label)
        elif has_midi:
            track_specs.append(
                TrackSpec(
                    "MIDI",
                    t.name,
                    device_xml,
                    arrangement_midi_clips=arrangement_midi_clips,
                    volume=vol,
                    pan=pan,
                    volume_automation=vol_automation,
                    pan_automation=pan_automation,
                    sends=sends,
                )
            )
            device_labels.append(device_label)
        else:
            track_specs.append(
                TrackSpec(
                    "Audio",
                    f"{t.name}{chanmode_flag}",
                    audio_devices,
                    arrangement_audio_clips=arrangement_audio_clips,
                    volume=vol,
                    pan=pan,
                    volume_automation=vol_automation,
                    pan_automation=pan_automation,
                    sends=sends,
                )
            )
            device_labels.append(audio_device_label)

    if not track_specs:
        raise SystemExit("No tracks found in project.")

    locators = [(pos_seconds * tempo / 60.0, name) for pos_seconds, name in project_markers(text)]

    scene_names: list[str] | None = None
    if scenes:
        if not locators:
            raise SystemExit("--scenes requires at least one project marker, none found.")
        # Locators aren't guaranteed sorted by position in the .rpp text --
        # sort so region N covers [locators[N].time, locators[N+1].time),
        # last region open-ended to the end of the project.
        locators = sorted(locators, key=lambda pair: pair[0])
        scene_bounds = [
            (start, locators[i + 1][0] if i + 1 < len(locators) else float("inf"))
            for i, (start, _name) in enumerate(locators)
        ]
        scene_names = [name for _, name in locators]

        # REAPER positions carry float noise (a marker at 8.00000000002s,
        # an item "on" it at exactly 8s) -- snap within this tolerance so
        # an item placed on a marker lands in that marker's Scene.
        _SCENE_EPS_BEATS = 1e-6

        def _scene_index_for(start_beats: float, track_name: str) -> int:
            for scene_idx, (lo, hi) in enumerate(scene_bounds):
                if lo - _SCENE_EPS_BEATS <= start_beats < hi - _SCENE_EPS_BEATS:
                    return scene_idx
            raise SystemExit(
                f"--scenes: track {track_name!r} has an item starting at beat {start_beats:g}, "
                "before the first marker -- every item must fall within a marker region."
            )

        for spec in track_specs:
            items = spec.arrangement_midi_clips or spec.arrangement_audio_clips or []
            if not items:
                spec.session_slot_count = len(scene_bounds)
                continue
            item_starts = (
                [entry[1] for entry in spec.arrangement_midi_clips]
                if spec.arrangement_midi_clips
                else [item.start_beats for item in spec.arrangement_audio_clips]
            )
            slot_indices = [_scene_index_for(start, spec.name) for start in item_starts]
            seen: dict[int, float] = {}
            for scene_idx, start in zip(slot_indices, item_starts):
                if scene_idx in seen:
                    raise SystemExit(
                        f"--scenes: track {spec.name!r} has 2+ items in Scene {scene_names[scene_idx]!r} "
                        f"(at beats {seen[scene_idx]:g} and {start:g}) -- only one item per track per "
                        "Scene is supported."
                    )
                seen[scene_idx] = start
            spec.session_slot_count = len(scene_bounds)
            spec.scene_slot_indices = slot_indices
    else:
        # Session view: mirror every track's own Arrangement items into that
        # SAME track's own Session-view ClipSlots too (never crossing into
        # another track's slots -- see als_builder.TrackSpec.
        # session_slot_count / _set_session_clip_slots, PLAN.md "Session view
        # clips"). Every track needs the SAME slot count for Session view to
        # stay a rectangular grid, so it's the project-wide max item count
        # across all tracks -- a track with fewer items than that just gets
        # empty slots past its own last one. 0 (no clips anywhere in the
        # project) leaves every track's stock template slot(s) untouched.
        max_session_slots = max(
            (len(spec.arrangement_midi_clips or spec.arrangement_audio_clips or []) for spec in track_specs),
            default=0,
        )
        if max_session_slots:
            for spec in track_specs:
                spec.session_slot_count = max_session_slots

    xml = build_liveset_xml(
        track_specs,
        tempo,
        time_signature,
        locators=locators or None,
        return_tracks=return_track_specs or None,
        scene_names=scene_names,
    )
    os.makedirs(out_dir, exist_ok=True)
    for write in pending_writes:
        write()
    write_als(xml, output_path)
    _write_project_info_marker(out_dir)

    print(f"Exported {len(track_specs)} track(s) to: {output_path} ({time_signature[0]}/{time_signature[1]})")
    if locators:
        suffix = f" + {len(scene_names)} Scene(s)" if scene_names else ""
        print(f"  {len(locators)} marker(s) -> Locator(s){suffix}")
    if return_track_specs:
        print(f"  {len(return_track_specs)} Return Track(s): {[name for name, _ in return_track_specs]}")
    for spec, label in zip(track_specs, device_labels):
        device_note = f" (+ {label})" if spec.device_xml and label else ""
        clip_note = ""
        if spec.arrangement_midi_clips:
            counts = [len(entry[0]) for entry in spec.arrangement_midi_clips]
            clip_note = f" (+ {len(counts)} MIDI clip(s), {counts} notes)"
        if spec.arrangement_audio_clips:
            clip_note += f" (+ {len(spec.arrangement_audio_clips)} audio clip(s))"
        send_note = ""
        if spec.sends and any(level is not None for level in spec.sends):
            send_note = f" (-> sends: {spec.sends})"
        print(f"  {spec.track_type}: {spec.name!r}{device_note}{clip_note}{send_note}")
    for w in all_warnings:
        print(f"  warning: {w}")
    for line in format_report(fx_report):
        print(line)
    return fx_report


def export_rpp(
    als_path: str,
    output_path: str,
    scenes: bool = False,
    force: bool = False,
    media_subdir: str | None = None,
) -> list[FxEntry]:
    """Reverse direction: Ableton `.als` -> REAPER `.rpp` (als_parse +
    rpp_generate, see PLAN.md ".als -> .rpp export"). Refuses to
    overwrite an existing file without `force` -- the target could be a
    real REAPER project. `media_subdir`: copy every audio file and drum
    sample into that subfolder next to the .rpp (--copy-media). Returns
    the FX report (also printed)."""
    if os.path.exists(output_path) and not force:
        raise SystemExit(f"{output_path} already exists -- pass --force to overwrite it.")
    out_dir = os.path.dirname(os.path.abspath(output_path))
    result = rpp_convert(parse_als(als_path), session_scenes=scenes, out_dir=out_dir, media_subdir=media_subdir)
    os.makedirs(out_dir, exist_ok=True)
    for src, dst in result.media_copies:
        if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dst)):
            copy_file(src, dst)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(result.text)
    print(f"Exported {result.track_count} track(s), {result.item_count} item(s) to: {output_path}")
    if scenes:
        print(f"  {result.scene_count} Scene(s) -> marker regions after the Arrangement")
    if media_subdir:
        print(f"  {len(result.media_copies)} media file(s) copied into {os.path.join(out_dir, media_subdir)}")
    for w in result.warnings:
        print(f"  warning: {w}")
    for line in format_report(result.fx_report):
        print(line)
    return result.fx_report


def main() -> int:
    parser = argparse.ArgumentParser(prog="rpp-to-als")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export an RS5k track to a .adg Drum Rack")
    export.add_argument("project", help="Path to .rpp project file")
    export.add_argument("track", help="Track name or 0-based index")
    export.add_argument("output", nargs="?", help="Output .adg path (omit when using --test)")
    export.add_argument("--rack-name", help="Drum Rack display name (default: track name)")
    export.add_argument(
        "--test",
        action="store_true",
        help=f"Write to {_LIVETEST_DIR} (set RPP_TO_ALS_TEST_DIR to change it) using the rack name as filename",
    )
    export.add_argument(
        "--choke",
        help=(
            "Choke groups as space-separated note-note pairs, e.g. '42-46 54-56' "
            "(default: auto-choke all HiHat Closed pads against all HiHat Open pads, if both present)"
        ),
    )
    export.add_argument(
        "--copy-samples",
        action="store_true",
        help="Copy WAVs next to the output .adg (portable export) instead of referencing the original library path",
    )

    list_tracks = sub.add_parser("list-tracks", help="List tracks and their RS5k instance counts")
    list_tracks.add_argument("project", help="Path to .rpp project file")

    export_project_cmd = sub.add_parser(
        "export-project",
        help="Export a whole project to a .als: track shells + embedded Drum Racks for RS5k tracks",
    )
    export_project_cmd.add_argument("project", help="Path to .rpp project file")
    export_project_cmd.add_argument("output", nargs="?", help="Output .als path (omit when using --test)")
    export_project_cmd.add_argument(
        "--test",
        action="store_true",
        help=f"Write to {_LIVETEST_DIR} (set RPP_TO_ALS_TEST_DIR to change it) using the project's filename",
    )
    export_project_cmd.add_argument(
        "--copy-samples",
        action="store_true",
        help="Copy RS5k/audio-item WAV files next to the .als (portable export) -- JSFX .jsfx scripts are NEVER bundled by this tool (see PLAN.md)",
    )
    export_project_cmd.add_argument(
        "--export-standalone-adg",
        action="store_true",
        help="Also write each Drum Rack track's own <name>.adg next to the .als (browser-importable elsewhere) -- OFF by default; the .als embeds the Drum Rack inline already and doesn't need it",
    )
    export_project_cmd.add_argument(
        "--scenes",
        action="store_true",
        help="Turn every project marker into a Session-view Scene (marker-to-next-marker region), replacing the normal item-position-based Session mirroring -- requires at least one marker, and at most one item per track per region",
    )

    export_rpp_cmd = sub.add_parser(
        "export-rpp",
        help="Reverse direction: convert an Ableton .als into a REAPER .rpp (tracks, clips, markers, Drum Racks -> RS5k)",
    )
    export_rpp_cmd.add_argument("als", help="Path to .als Live Set")
    export_rpp_cmd.add_argument("output", help="Output .rpp path")
    export_rpp_cmd.add_argument(
        "--scenes",
        action="store_true",
        help="Also lay out Session-view clips after the Arrangement, one marker region per non-empty Scene (reverse of export-project --scenes)",
    )
    export_rpp_cmd.add_argument("--force", action="store_true", help="Overwrite the output .rpp if it already exists")
    export_rpp_cmd.add_argument(
        "--copy-media",
        nargs="?",
        const=DEFAULT_MEDIA_SUBDIR,
        metavar="SUBFOLDER",
        help=f"Copy all audio files and drum samples into a subfolder next to the .rpp (default name: {DEFAULT_MEDIA_SUBDIR}) and reference them there",
    )

    args = parser.parse_args()

    if args.command == "export":
        if args.output is None and not args.test:
            parser.error("output is required unless --test is given")
        export_track(
            args.project,
            args.track,
            args.output,
            args.rack_name,
            test=args.test,
            choke=args.choke,
            copy_samples=args.copy_samples,
        )
        return 0

    if args.command == "export-project":
        if args.output is None and not args.test:
            parser.error("output is required unless --test is given")
        export_project(
            args.project,
            args.output,
            test=args.test,
            copy_samples=args.copy_samples,
            export_standalone_adg=args.export_standalone_adg,
            scenes=args.scenes,
        )
        return 0

    if args.command == "export-rpp":
        export_rpp(args.als, args.output, scenes=args.scenes, force=args.force, media_subdir=args.copy_media)
        return 0

    if args.command == "list-tracks":
        text = open(args.project, encoding="utf-8").read()
        for i, t in enumerate(parse_tracks(text)):
            count = len(find_rs5k_blocks(t.chunk))
            print(f"{i}: {t.name!r} ({count} RS5k instance(s))")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
