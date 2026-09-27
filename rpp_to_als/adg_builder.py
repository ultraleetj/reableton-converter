"""Build Ableton Drum Rack (.adg) files from RS5k pad data.

Python port of `build_pad`/`export_adg_flow` in `triaz_browser.lua` (see
`reference/ADG_EXPORT_PLAN.md` for the debugging history behind every
non-obvious value below — PlaybackMode, IsWarped, ReceivingNote inversion,
etc). v1 scope only: single-note one-shot pads, no loop/choke/HH-open
handling (those were the deferred cases in the Lua tool too).
"""

from __future__ import annotations

import gzip
import math
import os
import re
import shutil
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache


def xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# TRIAZ library convention: <...>\TRIAZ - Factory Collection\<DrumType>\...
# The Lua tool gets drum_type from its own per-instance metadata (set when
# the sample was loaded via the browser); this tool only reads the raw
# .rpp file, which has no such metadata, so it's inferred from the WAV
# path instead. "Noise" as a tag/subfolder under another drum type (e.g.
# "Snare Electronic\Noise\...") does NOT count -- only the drum_type
# folder itself, immediately after "TRIAZ - Factory Collection".
_TRIAZ_COLLECTION_MARKER = "TRIAZ - Factory Collection"


def triaz_drum_type(wav_path: str) -> str | None:
    parts = wav_path.replace("/", "\\").split("\\")
    for i, part in enumerate(parts):
        if part == _TRIAZ_COLLECTION_MARKER and i + 1 < len(parts):
            return parts[i + 1]
    return None


def is_triaz_noise_sample(wav_path: str) -> bool:
    drum_type = triaz_drum_type(wav_path)
    return drum_type is not None and drum_type.lower() == "noise"


def _normalized_drum_type(wav_path: str) -> str:
    drum_type = triaz_drum_type(wav_path)
    return drum_type.lower().replace(" ", "") if drum_type else ""


def is_triaz_hihat_closed(wav_path: str) -> bool:
    return _normalized_drum_type(wav_path) == "hihatclosed"


def is_triaz_hihat_open(wav_path: str) -> bool:
    return _normalized_drum_type(wav_path) == "hihatopen"


def parse_choke_pairs(spec: str) -> dict[int, int]:
    """Parse "42-46 54-56" (space-separated note-note pairs, matching the
    Lua tool's `export_adg_flow` choke-group dialog UX) into
    {note: group_id}, group_id starting at 1 in pair order. Deliberately
    space- not comma-separated -- see ADG_EXPORT_PLAN.md ("Choke groups
    added") for why (REAPER's GetUserInputs comma-parsing collision, not
    relevant here, but kept for consistency with the Lua tool's format)."""
    groups: dict[int, int] = {}
    for i, pair in enumerate(spec.split(), start=1):
        a, b = pair.split("-")
        groups[int(a)] = i
        groups[int(b)] = i
    return groups


# Matches the Lua tool's ADG_NOISE_FADE_OUT_MS -- see ADG_EXPORT_PLAN.md
# "Noise pad sustain" investigation for how this value (and the
# SustainMode/LoopOn/unwarped combination) was derived against a real
# looping factory reference (Ambient Swells.adv).
NOISE_FADE_OUT_MS = 900.0


def fill_template(template: str, subs: dict[str, str]) -> str:
    out = template
    for key, value in subs.items():
        out = out.replace("{{" + key + "}}", xml_escape(str(value)))
    return out


_DRUM_BRANCH_OPEN_RE = re.compile(r"<DrumBranchPreset[^>]*>")


def split_adg_template(xml: str) -> tuple[str, str, str]:
    """Split a captured 1-pad .adg's XML into header / clonable pad block / footer."""
    m = _DRUM_BRANCH_OPEN_RE.search(xml)
    if not m:
        raise ValueError("template has no <DrumBranchPreset> block")
    close_tag = "</DrumBranchPreset>"
    close_idx = xml.find(close_tag, m.end())
    if close_idx == -1:
        raise ValueError("template's <DrumBranchPreset> is never closed")
    close_end = close_idx + len(close_tag)
    return xml[: m.start()], xml[m.start() : close_end], xml[close_end:]


_DRUM_GROUP_DEVICE_OPEN_RE = re.compile(r"<DrumGroupDevice[^>]*>")


def extract_drum_group_device(rack_xml: str) -> str:
    """Pull just the `<DrumGroupDevice>...</DrumGroupDevice>` block out of a
    full .adg (`build_rack_xml()` output) -- the same content a standalone
    .adg wraps in `<Ableton><GroupDevicePreset><Device>...</Device>
    <PresetRef>...`, but for embedding directly into an Ableton track's
    `DeviceChain > Devices` list (als_builder.embed_drum_rack), the
    preset-file wrapper isn't wanted, just the device itself."""
    m = _DRUM_GROUP_DEVICE_OPEN_RE.search(rack_xml)
    if not m:
        raise ValueError("no <DrumGroupDevice> found in rack XML")
    close_tag = "</DrumGroupDevice>"
    close_idx = rack_xml.find(close_tag, m.end())
    if close_idx == -1:
        raise ValueError("<DrumGroupDevice> is never closed")
    return rack_xml[m.start() : close_idx + len(close_tag)]


_BRANCHES_EMPTY_RE = re.compile(r"<Branches\s*/>")
_ORIGINAL_SIMPLER_RE = re.compile(r"<OriginalSimpler[^>]*>")
_AUDIO_BRANCH_MIXER_RE = re.compile(r"<AudioBranchMixerDevice[^>]*>")


def _extract_balanced(text: str, open_re: re.Pattern, tagname: str) -> str:
    m = open_re.search(text)
    if not m:
        raise ValueError(f"no <{tagname}> found")
    close_tag = f"</{tagname}>"
    close_idx = text.find(close_tag, m.end())
    if close_idx == -1:
        raise ValueError(f"<{tagname}> is never closed")
    return text[m.start() : close_idx + len(close_tag)]


def _extract_original_simpler(pad_block: str) -> str:
    """Pull the `<OriginalSimpler>...</OriginalSimpler>` instrument payload
    out of a filled `<DrumBranchPreset>` pad block. This inner payload is
    structurally identical between the preset format (DrumBranchPreset,
    standalone .adg) and the inline format (DrumBranch, embedded directly
    in a .als track's DeviceChain) -- confirmed by comparing this
    project's template against a real Ableton-exported inline DrumBranch
    (Live's own "Quick Start Beat.als"). Only the wrapper around it
    differs; see PLAN.md."""
    return _extract_balanced(pad_block, _ORIGINAL_SIMPLER_RE, "OriginalSimpler")


def _extract_audio_branch_mixer_as_inline(pad_block: str) -> str:
    """Pull `<AudioBranchMixerDevice>...</AudioBranchMixerDevice>` (the
    preset-format per-pad mixer: volume/pan/sends) and rewrap it as the
    inline format's `<MixerDevice>` -- same fields (Speaker/Volume/
    Panorama/SendInfos/RoutingHelper), just a different outer tag with no
    `Id` attribute and no `AbletonDevicePreset`/`PresetRef` preset
    wrapper, confirmed against a real inline DrumBranch's own MixerDevice."""
    mixer = _extract_balanced(pad_block, _AUDIO_BRANCH_MIXER_RE, "AudioBranchMixerDevice")
    return mixer.replace('<AudioBranchMixerDevice Id="0">', "<MixerDevice>").replace(
        "</AudioBranchMixerDevice>", "</MixerDevice>"
    )


def _uniquify_inline_pad_ids(original_simpler: str, mixer: str, pad_i: int) -> tuple[str, str]:
    """The shared payload (extracted from drum_pad.xml) has a few sub-ids
    hardcoded to the SAME nonzero value regardless of which pad clones it
    -- MultiSamplePart Id="0", FileRef Id="22", WarpMarker Id="1",
    AudioBranchSendInfo Id="0"/"1" -- harmless for the standalone-.adg
    case (each pad's copy lives in its own separately-scoped
    DrumBranchPreset, hydrated fresh by Ableton at preset-load time), but
    the same-shaped bug as the already-fixed KeyTrack/MidiClip
    "Non-unique list ids" issue (PLAN.md) once multiple pads become
    siblings in one inline-embedded document. Give each pad clone its own
    distinct nonzero values, derived from its index so they can never
    collide with another pad's."""
    base = 1000 + pad_i * 10
    original_simpler = re.sub(r'(<MultiSamplePart Id=")0(")', rf"\g<1>{base}\g<2>", original_simpler, count=1)
    original_simpler = re.sub(r'(<FileRef Id=")22(">)', rf"\g<1>{base + 1}\g<2>", original_simpler, count=1)
    original_simpler = re.sub(
        r'(<WarpMarker Id=")1(" SecTime=)', rf"\g<1>{base + 2}\g<2>", original_simpler, count=1
    )
    mixer = re.sub(r'(<AudioBranchSendInfo Id=")0(">)', rf"\g<1>{base + 3}\g<2>", mixer, count=1)
    mixer = re.sub(r'(<AudioBranchSendInfo Id=")1(">)', rf"\g<1>{base + 4}\g<2>", mixer, count=1)
    return original_simpler, mixer


_POINTEE_FAMILY_ZERO_RE = re.compile(r'<(AutomationTarget|ModulationTarget|Pointee) Id="0"')


def _renumber_pointee_family(xml: str) -> str:
    """Real Ableton-saved devices never leave `AutomationTarget`/
    `ModulationTarget`/`Pointee` at `Id="0"` -- confirmed by diffing a real
    inline Drum Rack ("Quick Start Beat.als"): every one of its ~2800 +
    2000 + 80 such elements has a distinct nonzero id, while a device
    built by cloning drum_pad.xml's per-pad template leaves literally all
    of them (thousands, once cloned across many pads) at the shared "0".

    This doesn't contradict the "Id=0 is a safe null sentinel" rule used
    elsewhere in this codebase (tracks, clips) -- a handful of scattered
    Id="0" automation targets on a single track loads fine in real
    Ableton (see PLAN.md). What breaks here is cloning the SAME zeroed
    automation-registry subtree as exact siblings at scale (one pad's
    OriginalSimpler has ~86 such targets, x 53 pads) -- the same *shape*
    of bug as the earlier KeyTrack/MidiClip "Non-unique list ids" fix,
    just many levels deeper than any per-tag or sibling-scoped check
    looked (see PLAN.md "Pointee/AutomationTarget/ModulationTarget must
    be nonzero in a populated device"). Assign each occurrence a fresh,
    sequential, unique nonzero id -- matches what a real save produces.
    Safe to run on the WHOLE assembled device (wrapper + all pads) since
    the counter is shared, guaranteeing no collisions across pads.

    Starts well above _uniquify_inline_pad_ids's own range (tops out
    around ~2000 even for a large rack) -- the device's total raw id
    budget must stay under _DEVICE_ID_SUB_OFFSET (50_000, see
    als_builder.py) or it overflows into the NEXT track's own 100_000-
    wide bucket once embedded, so this can't just be pushed arbitrarily
    high either; 10_000 leaves headroom on both sides for realistic pad
    counts (thousands of these targets per device, not tens of
    thousands)."""
    counter = [10_000]

    def next_id(m: re.Match) -> str:
        counter[0] += 1
        return f'<{m.group(1)} Id="{counter[0]}"'

    return _POINTEE_FAMILY_ZERO_RE.sub(next_id, xml)


def build_inline_drum_group_device(
    rack_template: str,
    inline_pad_template: str,
    rack_name: str,
    pads: list[Pad],
    *,
    choke_group_by_note: dict[int, int] | None = None,
    time_signature: tuple[int, int] = (4, 4),
) -> str:
    """Build a populated `<DrumGroupDevice><Branches>...real pads...
    </Branches></DrumGroupDevice>` block, ready for direct embedding into
    a .als track's `<Devices>` list (als_builder._embed_device).

    Unlike `extract_drum_group_device(build_rack_xml(...))`, whose
    `<Branches>` is always empty -- that's a PRESET-file structure
    (BranchPresets/DrumBranchPreset) that Ableton only hydrates into
    Branches when loading a .adg from the browser/drag-and-drop, NOT when
    a device is already inline in a track's DeviceChain. This produces
    real `<DrumBranch>` children instead (see PLAN.md's "pending
    investigation" note this resolves)."""
    choke_group_by_note = choke_group_by_note or {}

    # Reuse build_rack_xml just for its already-proven outer DrumGroupDevice
    # wrapper (RACK_NAME substitution, macros, etc). Passing no pads means
    # BranchPresets/Branches both come back empty -- fine, the wrapper
    # itself is identical either way (see PLAN.md); only Branches' content
    # differs between the preset and inline cases.
    empty_rack_xml = build_rack_xml(rack_template, rack_name, [], choke_group_by_note=None)
    device_xml = extract_drum_group_device(empty_rack_xml)

    _, pad_block_template, _ = split_adg_template(rack_template)

    blocks = []
    for pad_i, p in enumerate(pads):
        filled_pad_block = fill_template(
            pad_block_template, _pad_substitutions(pad_i, p, choke_group_by_note, time_signature)
        )
        original_simpler = _extract_original_simpler(filled_pad_block)
        mixer = _extract_audio_branch_mixer_as_inline(filled_pad_block)
        original_simpler, mixer = _uniquify_inline_pad_ids(original_simpler, mixer, pad_i)

        inline_subs = {
            "PAD_ID": str(pad_i),
            "PAD_NAME": p.pad_name,
            "RECEIVING_NOTE": str(128 - p.note),
            "CHOKE_GROUP": str(choke_group_by_note.get(p.note, 0)),
        }
        pad_xml = fill_template(inline_pad_template, inline_subs)
        pad_xml = pad_xml.replace("{{ORIGINAL_SIMPLER}}", original_simpler).replace("{{MIXER_DEVICE}}", mixer)
        blocks.append(pad_xml)

    combined = "".join(blocks)
    result, n = _BRANCHES_EMPTY_RE.subn(lambda m: f"<Branches>{combined}</Branches>", device_xml, count=1)
    if n != 1:
        raise ValueError(f"expected exactly one empty <Branches /> in DrumGroupDevice wrapper, found {n}")
    return _renumber_pointee_family(result)


@lru_cache(maxsize=None)
def get_wav_frames(path: str) -> tuple[int, int] | None:
    """Returns (frame_count, sample_rate) for a WAV file, or None if it
    can't be read. Cached by path -- the same source WAV commonly appears
    on multiple items (drum rack pads, repeated audio items), and
    re-reading its header for each occurrence is wasted I/O for an
    identical result.

    Tries the stdlib `wave` module first -- exact frame count straight
    from the file's own data-chunk size, but it only understands PCM
    format 1 (`unknown format: 3` on anything else). REAPER's own
    recording default is 32-bit FLOAT WAV (format 3), which is common
    real-world input here and would otherwise be silently skipped
    entirely -- falls back to mutagen's dedicated WAVE parser (handles
    float/extensible formats), deriving frame count from its
    float-seconds duration same as get_audio_frames' non-WAV path
    (slightly less exact than the stdlib path, still sample-rate-close
    enough for Arrangement placement/loop purposes)."""
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes(), w.getframerate()
    except (FileNotFoundError, EOFError):
        return None
    except wave.Error:
        pass

    import mutagen.wave

    try:
        info = mutagen.wave.WAVE(path).info
    except (OSError, mutagen.MutagenError):
        return None
    if not info.sample_rate or not info.length:
        return None
    return round(info.length * info.sample_rate), info.sample_rate


@lru_cache(maxsize=None)
def get_audio_frames(path: str) -> tuple[int, int] | None:
    """Returns (frame_count, sample_rate) for any REAPER-supported audio
    item source (WAV, MP3, FLAC, OGG, AIFF), or None if it can't be read.
    WAV goes through get_wav_frames (exact frame count from the file's own
    header). Everything else has no reliable exact frame count without a
    full decode, so it's derived from mutagen's own (float-seconds)
    duration estimate times the sample rate -- close enough for Arrangement
    placement/loop-length purposes, not sample-accurate the way the WAV
    path is."""
    if path.lower().endswith(".wav"):
        return get_wav_frames(path)
    import mutagen

    try:
        f = mutagen.File(path)
    except (OSError, mutagen.MutagenError):
        return None
    if f is None or f.info is None:
        return None
    sample_rate = getattr(f.info, "sample_rate", None)
    length_seconds = getattr(f.info, "length", None)
    if not sample_rate or not length_seconds:
        return None
    return round(length_seconds * sample_rate), sample_rate


def rs5k_pan_to_panorama(pan_normalized: float) -> float:
    """RS5k pan (0=L, 0.5=C, 1=R) -> Ableton Panorama (-1..1, 0=center)."""
    return (pan_normalized - 0.5) * 2


@dataclass
class Pad:
    note: int  # MIDI note this pad triggers on (pre-ReceivingNote-inversion)
    name: str  # WAV filename
    path: str  # absolute WAV path
    pad_name: str
    volume: float  # true linear gain, unity = 1.0
    panorama: float  # -1..1
    transpose_key: int
    transpose_fine: int  # cents, -100..100
    frames: int
    rate: int
    sustain_mode: int = 0
    fade_out_ms: float = 0.1
    loop_on: bool = False
    playback_mode: str = "1"  # "1"=One-Shot, "0"=Classic (required for loop_on)
    is_warped: str = "false"
    warp_mode: str = "0"
    warp_end_sec: float = 0.015625  # template's original placeholder values
    warp_end_beat: float = 0.03125
    copied_locally: bool = False  # set by copy_samples_locally(); affects SAMPLE_RELPATH
    # path RELATIVE TO whatever file (.adg or the embedding .als) this
    # pad's own template block ends up in -- set by copy_samples_locally
    # alongside copied_locally, replaces the old copied_locally-implies-
    # "just p.name" assumption (wrong for the export-project/.als case,
    # where the copy lands in a Samples/Imported subfolder, not next to
    # the file itself -- see copy_samples_locally's relative_prefix).
    relative_path: str = ""
    # Real byte size / CRC32 of `path` -- see _pad_substitutions' docstring
    # for why (matches every real FileRef's OriginalFileSize/OriginalCrc).
    # Computed by build_pad (which already opens the real file); left at
    # the old "0" default for Pads built directly (e.g. in tests) rather
    # than via build_pad.
    original_file_size: int = 0
    original_crc: int = 0


def build_pad(
    note: int,
    wav_path: str,
    volume: float,
    pan_normalized: float,
    pitch_st: float,
    pad_name: str,
    *,
    sustain_mode: int = 0,
    fade_out_ms: float = 0.1,
    loop_on: bool = False,
) -> Pad | None:
    """Returns None if the WAV header can't be read (mirrors the Lua tool's
    behavior: skip with a warning rather than fail the whole export)."""
    wav_info = get_wav_frames(wav_path)
    if wav_info is None:
        return None
    frames, rate = wav_info

    # matches lua: t_key = ceil(semis) if semis<0 else floor(semis)
    t_key = math.ceil(pitch_st) if pitch_st < 0 else math.floor(pitch_st)
    t_fine = round((pitch_st - t_key) * 100)

    name = wav_path.replace("/", "\\").rsplit("\\", 1)[-1]

    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    # OriginalCrc's XML int node is SIGNED 32-bit -- zlib.crc32 returns
    # UNSIGNED (0..4294967295); values past 2147483647 make Ableton
    # reject the whole document on load ("Unexpected value for int
    # node"), confirmed the hard way -- see als_builder.file_size_and_crc32
    # for the same fix on the audio-clip side.
    unsigned_crc = zlib.crc32(wav_bytes)
    signed_crc = unsigned_crc - 0x1_0000_0000 if unsigned_crc >= 0x8000_0000 else unsigned_crc

    return Pad(
        note=note,
        name=name,
        path=wav_path,
        pad_name=pad_name,
        volume=volume,
        panorama=rs5k_pan_to_panorama(pan_normalized),
        transpose_key=t_key,
        transpose_fine=t_fine,
        frames=frames,
        rate=rate,
        sustain_mode=sustain_mode,
        fade_out_ms=fade_out_ms,
        loop_on=loop_on,
        playback_mode="0" if loop_on else "1",
        original_file_size=len(wav_bytes),
        original_crc=signed_crc,
    )


def copy_file(src: str, dst: str) -> None:
    """`shutil.copy2`, creating `dst`'s folder first -- the default
    `copy` hook for copy_samples_locally/cli._bundle_audio_clip_samples."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def copy_samples_locally(
    pads: list[Pad], dest_dir: str, relative_prefix: str = "", copy: Callable[[str, str], None] = copy_file
) -> None:
    """Copy each pad's WAV into `dest_dir` (portable export) and repoint
    `p.path` at the copy. Dedupes by source path -- the same sample used
    by multiple pads (e.g. layered instances) is copied only once, same
    as the Lua tool's `seen_paths` table in `export_adg_flow`. Mutates
    `pads` in place; call before `build_rack_xml`.

    `relative_prefix` is prepended to `p.relative_path` (which
    `_pad_substitutions` writes into the FileRef's `RelativePath`, letting
    Ableton resolve the sample if the whole export is moved to a
    different machine/location -- see build_audio_clip_xml's own
    `relative_path` param for the sibling fix on the Arrangement-audio-
    clip side). Default "" is correct when `dest_dir` IS the file the pad
    ends up next to (a standalone `.adg`'s own directory -- `p.name`
    alone is already relative to that). When `dest_dir` is a SUBFOLDER of
    the file the pad ends up in (export-project's embedded-device case,
    `dest_dir` = `<als_dir>/Samples/Imported`), pass that subfolder back,
    e.g. `"Samples/Imported/"`, so the relative path is correct from the
    `.als`'s own directory instead.

    `copy` does the physical file copy; export_project passes a queueing
    hook so nothing is written until the whole export has validated."""
    copied: dict[str, str] = {}
    for p in pads:
        if p.path not in copied:
            dst = os.path.join(dest_dir, p.name)
            copy(p.path, dst)
            copied[p.path] = dst
        p.path = copied[p.path]
        p.copied_locally = True
        p.relative_path = relative_prefix + p.name


def _pad_substitutions(
    pad_i: int, p: Pad, choke_group_by_note: dict[int, int], time_signature: tuple[int, int] = (4, 4)
) -> dict[str, str]:
    """Shared by the preset-file pad block (DrumBranchPreset, build_rack_xml)
    and the inline pad block (DrumBranch, build_inline_drum_group_device) --
    both wrap the exact same OriginalSimpler/AudioBranchMixerDevice payload
    (confirmed by comparing this project's template against a real
    Ableton-exported inline DrumBranch, see PLAN.md), so the substitution
    values are identical; only the surrounding wrapper differs.

    RelativePathType/RelativePath/Type/OriginalFileSize/OriginalCrc are
    ALL forced to the plain absolute-Path-only state regardless of
    `p.copied_locally`/`p.relative_path`/`p.original_file_size`/
    `p.original_crc` -- see als_builder.build_audio_clip_xml's docstring
    for the full story: two separate portability attempts (RelativePathType
    "1" then "3", the second matched field-for-field against a real
    locally-collected Ableton project) both crashed real Ableton on load.
    Cross-machine sample portability for --copy-samples is UNSOLVED;
    absolute-Path-only is the only confirmed-safe state. The unused
    fields/params are kept so a future attempt doesn't have to re-thread
    every call site."""
    return {
        "PAD_ID": str(pad_i),
        # Ableton's ReceivingNote is stored inverted: actual trigger note
        # = 128 - value (verified empirically, see ADG_EXPORT_PLAN.md).
        "RECEIVING_NOTE": str(128 - p.note),
        "SAMPLE_NAME": p.name,
        "SAMPLE_PATH": p.path,
        "SAMPLE_RELPATH": "",
        "SAMPLE_RELPATH_TYPE": "0",
        "SAMPLE_FILE_TYPE": "2",
        "SAMPLE_FILE_SIZE": "0",
        "SAMPLE_CRC": "0",
        "VOLUME": f"{p.volume:.6f}",
        "PANORAMA": f"{p.panorama:.6f}",
        "SUSTAIN_MODE": str(p.sustain_mode),
        "FADE_OUT_MS": f"{p.fade_out_ms:.6f}",
        "LOOP_ON": "true" if p.loop_on else "false",
        "PLAYBACK_MODE": p.playback_mode,
        "IS_WARPED": p.is_warped,
        "WARP_MODE": p.warp_mode,
        "WARP_END_SEC": f"{p.warp_end_sec:.9f}",
        "WARP_END_BEAT": f"{p.warp_end_beat:.9f}",
        "CHOKE_GROUP": str(choke_group_by_note.get(p.note, 0)),
        "TRANSPOSE_KEY": str(p.transpose_key),
        "TRANSPOSE_FINE": str(p.transpose_fine),
        "SAMPLE_END": str(p.frames),
        "SAMPLE_DURATION": str(p.frames),
        "SAMPLE_RATE": str(p.rate),
        "TIME_SIG_NUM": str(time_signature[0]),
        "TIME_SIG_DEN": str(time_signature[1]),
    }


def build_rack_xml(
    template: str,
    rack_name: str,
    pads: list[Pad],
    *,
    choke_group_by_note: dict[int, int] | None = None,
    time_signature: tuple[int, int] = (4, 4),
) -> str:
    header, pad_block, footer = split_adg_template(template)
    header = header.replace("{{RACK_NAME}}", xml_escape(rack_name))
    choke_group_by_note = choke_group_by_note or {}

    blocks = [
        fill_template(pad_block, _pad_substitutions(pad_i, p, choke_group_by_note, time_signature))
        for pad_i, p in enumerate(pads)
    ]

    return header + "".join(blocks) + footer


def write_adg(xml_text: str, adg_path: str) -> None:
    """Gzip-write the assembled XML as a .adg. Replaces the Lua tool's
    PowerShell GZipStream shellout — Python's stdlib gzip does the same job
    directly, no subprocess needed."""
    with gzip.open(adg_path, "wb") as f:
        f.write(xml_text.encode("utf-8"))
