"""Build Ableton Live Set (.als) files: track shells + embedded devices.

Generates track shells (correct name, correct MIDI-vs-Audio type, real
tempo) and can embed a device (e.g. an RS5k Drum Rack) directly into a
track's DeviceChain. Uses the same technique proven in adg_builder.py --
clone real content from Ableton's own default templates and substitute a
few fields / splice in another block, rather than hand-writing XML from
scratch (which the .adg investigation found does not work: "Ableton
requires the exact structure of a real preset file, including hundreds of
default parameters", see reference/ADG_EXPORT_PLAN.md). Cross-file block
transplant (a MidiTrack block from one file spliced into another file's
envelope; a DrumGroupDevice from a standalone-.adg build spliced into a
track's empty `<Devices />`) is the same technique already confirmed
working for .adg pads ("cross_test.adg").

Ableton's own default templates are NOT copied into this repo (they're
Ableton's copyrighted files) -- read directly from the local install at
runtime. If Ableton isn't installed at the expected path, track building
fails with a clear error; there is no bundled fallback.

MIDI clips: a Session-view clip (first scene slot) can now be embedded
with real note data (build_midi_clip_xml / TrackSpec.midi_notes), built
from a real Ableton-exported MidiClip block (see templates/midi_clip.xml,
extracted from Ableton's own "Drums Lesson.als" factory content -- PLAN.md
"MIDI clip embedding"). Clip position is always beat 0 (item's own
POSITION-in-project / PLAYRATE time-stretch not yet applied -- notes stay
relative to the item's own start, see rpp_parse.parse_midi_item_notes);
Arrangement-view placement, not just Session view, is also not yet done.

Time signature: project-root meter (build_liveset_xml's time_signature
param) is wired -- Ableton packs numerator+denominator into one integer
(LiveSet-root `<TimeSignature><Manual Value="N" />`, see
_encode_time_signature / PLAN.md "Time signature"), confirmed against a
real Ableton template file plus an independent open-source encoder/
decoder pair. Restricted to Ableton's own supported range: numerator
1-99, denominator in {1, 2, 4, 8, 16}.

Audio clips: an Arrangement-view AudioClip per REAPER audio item (straight
playback, looped playback with REAPER's `OFFSET`/`SOFFS`, and PLAYRATE
time-stretch) is embedded now -- see build_audio_clip_xml /
TrackSpec.arrangement_audio_clips, templates/audio_clip.xml (sourced from
a real Ableton-exported example, the Chuck Sutton "Patience" factory demo
song's own Arrangement AudioClip). Two modes: `IsWarped="false"` (native
speed, `PLAYRATE == 1.0`, the common case -- Loop values are plain
SECONDS, a different coordinate space than Arrangement placement, see
build_audio_clip_xml's docstring) or `IsWarped="true"` with a 2-marker
linear stretch reproducing REAPER's own PLAYRATE ratio (Loop values then
switch to real tempo-scaled beats, matching a real warped Ableton clip's
own convention). An earlier version of this always exported unwarped,
tempo-scaled-Loop clips regardless of PLAYRATE -- confirmed by the user
(2026-07-24) to audibly play too fast on any item with a non-1.0
PLAYRATE (a genuinely time-stretched item, e.g. `carrizal.RPP`'s
"torbellino..." item at `PLAYRATE 0.69047619047619`). Not yet
reload-confirmed for the newer 2-mode version (MIDI clips/devices/tempo
were; audio clips are new, real-Ableton-reload-tested only for the
straightforward unwarped case so far as of writing).

NOT YET DONE: routing, return tracks beyond the one carried over from
the template, groups. Track color intentionally out of scope (see
PLAN.md) -- Ableton's ColorIndex is a fixed 70-color palette, REAPER's
PEAKCOL is arbitrary RGB, no clean 1:1 mapping.
"""

from __future__ import annotations

import glob
import gzip
import os
import re
import zlib
from collections import defaultdict
from dataclasses import dataclass

from .model import MidiCCEvent, MidiNote

_MIDI_CLIP_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "midi_clip.xml")
_AUDIO_CLIP_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "audio_clip.xml")
_VST3_SURGE_DEVICE_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "templates", "vst3_surge_instrument_device.xml"
)
_VST3_WAVES_RVERB_DEVICE_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "templates", "vst3_waves_rverb_device.xml"
)
_VST3_WAVES_CLA_GUITARS_DEVICE_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "templates", "vst3_waves_cla_guitars_device.xml"
)
_VST2_REAJS_DEVICE_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "vst2_reajs_device.xml")
_VST2_READELAY_DEVICE_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "templates", "vst2_readelay_device.xml"
)
_RETURN_TRACK_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "return_track.xml")
_SIMPLER_SINE_DEVICE_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "simpler_sine_device.xml")

def find_core_library() -> str | None:
    """The installed Ableton Live 12 Core Library folder, whose default
    track templates every export is built from (they're read from the
    user's own install, not bundled). `ABLETON_CORE_LIBRARY` overrides;
    otherwise any Live 12 edition in the standard Windows/macOS spots,
    Suite first."""
    env = os.environ.get("ABLETON_CORE_LIBRARY")
    if env:
        return env
    candidates = glob.glob(r"C:\ProgramData\Ableton\Live 12*\Resources\Core Library")
    candidates += glob.glob("/Applications/Ableton Live 12*.app/Contents/App-Resources/Core Library")
    candidates.sort(key=lambda p: ("Suite" not in p, p))
    return candidates[0] if candidates else None


_CORE_LIBRARY = find_core_library() or ""
_AUDIO_TRACK_ALS = os.path.join(_CORE_LIBRARY, "Defaults", "Creating Tracks", "Audio Track", "Default Audio Track.als")
_MIDI_TRACK_ALS = os.path.join(_CORE_LIBRARY, "Defaults", "Creating Tracks", "MIDI Track", "Default MIDI Track.als")

_TRACK_NAME_BLOCK_RE = re.compile(r'<EffectiveName Value="[^"]*" />\s*<UserName Value="[^"]*" />')
_ID_ATTR_RE = re.compile(r'Id="(-?\d+)"')
_TRACKS_OPEN_RE = re.compile(r"<Tracks>")

# The track's own root element (<MidiTrack Id="0" .../<AudioTrack Id="0" ...)
# in Ableton's default templates is "0" -- same as every OTHER Id="0" in the
# template, which _renumber_ids deliberately leaves alone as the null-
# pointee sentinel. But unlike those, a track's root Id is a real, load-
# bearing sibling-list id: every track is a direct sibling under
# <LiveSet><Tracks>, so cloning N tracks from the same template leaves all
# N sharing Id="0" -- Ableton's real "Non-unique list ids" corruption error
# (confirmed: forum reports of this exact error describe "2 tracks with the
# same ID", and our own multi-track export had every single track's root Id
# still "0"). Same *shape* of bug as KeyTrack/MidiClip/DrumBranch before it
# -- a literal "0" that's the null sentinel almost everywhere, except at
# this one list-membership spot. Force it to id_offset (unique per track,
# never collides with 0 or with anything else -- id_offset starts at
# 100_000, well above the template's own small raw ids).
_TRACK_ROOT_ID_RE = re.compile(r'^<(MidiTrack|AudioTrack) Id="0"')

# Every MIDI track template ships a fixed 131-entry <MidiControllers>
# block (Ableton writes this on every MIDI track, even fully unautomated)
# under MainSequencer: <ControllerTargets.N Id="X"><LockEnvelope .../>
# </ControllerTargets.N>, N = 0..130. Confirmed against a real Ableton-
# imported clip with genuine CC1 automation (see PLAN.md "MIDI CC
# automation") that a clip's CC-lane ClipEnvelope target is
# ControllerTargets.(cc_number + 2) -- 2 extra slots (Pitch Bend, Channel
# Pressure) precede CC0 at index 2, one more trailing extra sits at index
# 130 after CC127. Ids are already present in the template and get
# shifted by the normal per-track _renumber_ids/id_offset pass like every
# other Id in the block -- no fresh id allocation needed, just read back
# what's already there after renumbering.
_CONTROLLER_TARGET_RE = re.compile(r'<ControllerTargets\.(\d+) Id="(\d+)">')

# A track's own Mixer Volume/Pan <Manual> value -- anchored on the
# enclosing tag name since "<Manual Value=..." alone appears many times
# in a track block (Tempo, On/Speaker/CrossFadeState, devices...). Both
# <Volume> and <Pan> appear exactly once per fresh track template
# (confirmed against both midi_track_block() and audio_track_block()), no
# ambiguity risk like the shared-tag issues elsewhere in this file. A
# fresh track template's <Envelopes /> list is empty (no shadow
# AutomationEnvelope for Volume/Pan the way the LiveSet-root Tempo/
# TimeSignature always carries one, see Bug 7 in PLAN.md) -- so patching
# <Manual> directly is sufficient here, no _patch_shadow_envelope needed.
_TRACK_VOLUME_MANUAL_RE = re.compile(r'(<Volume>\s*<LomId Value="0" />\s*<Manual Value=")[^"]*(")')
_TRACK_PAN_MANUAL_RE = re.compile(r'(<Pan>\s*<LomId Value="0" />\s*<Manual Value=")[^"]*(")')

# A track's own Volume/Pan <AutomationTarget Id>, needed as the
# EnvelopeTarget/PointeeId for a real (non-shadow) automation envelope --
# see AUTOMATION.md. Anchored the same way as _TRACK_VOLUME_MANUAL_RE/
# _TRACK_PAN_MANUAL_RE (enclosing tag name), scanning past the
# MidiControllerRange block to reach the field's own AutomationTarget
# rather than a sibling field's.
_TRACK_VOLUME_TARGET_RE = re.compile(
    r'<Volume>\s*<LomId Value="0" />\s*<Manual Value="[^"]*" />\s*'
    r"<MidiControllerRange>.*?</MidiControllerRange>\s*"
    r'<AutomationTarget Id="(\d+)"',
    re.DOTALL,
)
_TRACK_PAN_TARGET_RE = re.compile(
    r'<Pan>\s*<LomId Value="0" />\s*<Manual Value="[^"]*" />\s*'
    r"<MidiControllerRange>.*?</MidiControllerRange>\s*"
    r'<AutomationTarget Id="(\d+)"',
    re.DOTALL,
)
_TRACK_AUTOMATION_ENVELOPES_EMPTY_RE = re.compile(r"<AutomationEnvelopes>\s*<Envelopes\s*/>\s*</AutomationEnvelopes>")

# A track's own empty <Sends /> (populated by _add_track_sends when the
# REAPER track has one or more AUXRECV entries pointing at a bus -- see
# AUTOMATION.md "Sends"). Exactly one per fresh track template (both
# regular AudioTrack/MidiTrack blocks and templates/return_track.xml),
# confirmed the same way as _TRACK_VOLUME_MANUAL_RE.
_SENDS_EMPTY_RE = re.compile(r"<Sends\s*/>")

_RETURN_TRACK_ROOT_ID_RE = re.compile(r'^\s*<ReturnTrack Id="0"')


def _decompress(als_path: str) -> str:
    with gzip.open(als_path, "rb") as f:
        return f.read().decode("utf-8")


def _extract_block(xml: str, tag: str) -> str:
    m = re.search(r"<" + tag + r"[^>]*>", xml)
    if not m:
        raise ValueError(f"no <{tag}> found")
    close_tag = f"</{tag}>"
    close_idx = xml.find(close_tag, m.end())
    if close_idx == -1:
        raise ValueError(f"<{tag}> is never closed")
    return xml[m.start() : close_idx + len(close_tag)]


class _Templates:
    """Loaded lazily and cached -- decompressing + regex-scanning a ~50KB
    XML file per export is cheap, but no need to repeat it per track."""

    _audio_track_block: str | None = None
    _midi_track_block: str | None = None
    _header: str | None = None
    _tail: str | None = None
    _midi_clip_template: str | None = None
    _audio_clip_template: str | None = None
    _vst3_surge_device_template: str | None = None
    _vst3_waves_rverb_device_template: str | None = None
    _vst3_waves_cla_guitars_device_template: str | None = None
    _vst2_reajs_device_template: str | None = None
    _vst2_readelay_device_template: str | None = None
    _return_track_template: str | None = None

    @classmethod
    def load(cls) -> None:
        if cls._header is not None:
            return
        if not (os.path.exists(_AUDIO_TRACK_ALS) and os.path.exists(_MIDI_TRACK_ALS)):
            raise SystemExit(
                "Ableton Live 12's default track templates weren't found (looked for a Live 12 Core Library). "
                "Set ABLETON_CORE_LIBRARY to your Core Library folder, e.g. "
                r"C:\ProgramData\Ableton\Live 12 Standard\Resources\Core Library"
            )
        audio_xml = _decompress(_AUDIO_TRACK_ALS)
        midi_xml = _decompress(_MIDI_TRACK_ALS)

        cls._audio_track_block = _extract_block(audio_xml, "AudioTrack")
        cls._midi_track_block = _extract_block(midi_xml, "MidiTrack")

        # Envelope (header + tail) comes from the audio-track file: header
        # is everything up to and including the opening <Tracks>, tail is
        # everything from the end of its own AudioTrack block onward (the
        # file's one ReturnTrack, </Tracks>, MainTrack/master, Transport
        # with tempo, etc). We replace only the single default AudioTrack
        # in the middle with our own generated track blocks.
        m = _TRACKS_OPEN_RE.search(audio_xml)
        cls._header = audio_xml[: m.end()]
        audio_track_end = audio_xml.find("</AudioTrack>") + len("</AudioTrack>")
        cls._tail = audio_xml[audio_track_end:]

    @classmethod
    def header(cls) -> str:
        cls.load()
        return cls._header

    @classmethod
    def tail(cls) -> str:
        cls.load()
        return cls._tail

    @classmethod
    def audio_track_block(cls) -> str:
        cls.load()
        return cls._audio_track_block

    @classmethod
    def midi_track_block(cls) -> str:
        cls.load()
        return cls._midi_track_block

    @classmethod
    def midi_clip_template(cls) -> str:
        if cls._midi_clip_template is None:
            with open(_MIDI_CLIP_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._midi_clip_template = f.read()
        return cls._midi_clip_template

    @classmethod
    def audio_clip_template(cls) -> str:
        if cls._audio_clip_template is None:
            with open(_AUDIO_CLIP_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._audio_clip_template = f.read()
        return cls._audio_clip_template

    @classmethod
    def vst3_surge_device_template(cls) -> str:
        if cls._vst3_surge_device_template is None:
            with open(_VST3_SURGE_DEVICE_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._vst3_surge_device_template = f.read()
        return cls._vst3_surge_device_template

    @classmethod
    def vst3_waves_rverb_device_template(cls) -> str:
        if cls._vst3_waves_rverb_device_template is None:
            with open(_VST3_WAVES_RVERB_DEVICE_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._vst3_waves_rverb_device_template = f.read()
        return cls._vst3_waves_rverb_device_template

    @classmethod
    def vst3_waves_cla_guitars_device_template(cls) -> str:
        if cls._vst3_waves_cla_guitars_device_template is None:
            with open(_VST3_WAVES_CLA_GUITARS_DEVICE_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._vst3_waves_cla_guitars_device_template = f.read()
        return cls._vst3_waves_cla_guitars_device_template

    @classmethod
    def vst2_reajs_device_template(cls) -> str:
        if cls._vst2_reajs_device_template is None:
            with open(_VST2_REAJS_DEVICE_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._vst2_reajs_device_template = f.read()
        return cls._vst2_reajs_device_template

    @classmethod
    def vst2_readelay_device_template(cls) -> str:
        if cls._vst2_readelay_device_template is None:
            with open(_VST2_READELAY_DEVICE_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._vst2_readelay_device_template = f.read()
        return cls._vst2_readelay_device_template

    @classmethod
    def return_track_template(cls) -> str:
        if cls._return_track_template is None:
            with open(_RETURN_TRACK_TEMPLATE_PATH, encoding="utf-8") as f:
                cls._return_track_template = f.read()
        return cls._return_track_template


def _renumber_ids(xml_block: str, offset: int) -> str:
    """Every top-level and nested `Id="N"` in a cloned track block must be
    unique across tracks in the same file, or Ableton's LOM (automation
    targets, envelopes, etc) may alias between tracks. Shift every Id by a
    large per-track offset -- doesn't touch `Value="N"` attributes.

    `Id="0"` is Ableton's null-pointee sentinel (unassigned automation/
    modulation target) and appears hundreds of times per untouched track
    template -- shifting it would collapse them all onto one bogus shared
    non-zero id per track, which is what actually triggers Live's "pointer
    IDs not unique" load error. Leave zeros alone."""
    return _ID_ATTR_RE.sub(
        lambda m: m.group(0) if m.group(1) == "0" else f'Id="{int(m.group(1)) + offset}"',
        xml_block,
    )


# Total width of each track's own Id bucket (id_offset is a multiple of
# this, see build_liveset_xml) -- widened from an earlier 100_000 to
# 400_000 to fit _SESSION_MIRROR_CLIP_ID_SUB_OFFSET's zone alongside
# everything else once a track's items can appear in BOTH Arrangement
# and (project-wide-max-sized) Session-view slots simultaneously -- see
# PLAN.md "Session view clips". A track template's own raw Ids top out
# around ~4400 and a full 53-pad DrumGroupDevice's raw Ids top out at 3
# (pad Ids are locally-scoped within DrumBranchPreset, not global --
# verified empirically, see PLAN.md), so every sub-zone below has
# enormous headroom regardless.
_TRACK_ID_BUCKET_WIDTH = 400_000

# Sub-offset for an embedded device's own Ids. Moved from an earlier
# 50_000 to make room for _SESSION_MIRROR_CLIP_ID_SUB_OFFSET's zone
# ahead of it.
_DEVICE_ID_SUB_OFFSET = 280_000

_DEVICES_EMPTY_RE = re.compile(r"<Devices\s*/>")


# Sub-offset for an embedded Session-view clip's NoteIds, via the OLD
# TrackSpec.midi_notes/extra_midi_clips mechanism (MAX_MIDI_CLIPS_PER_
# TRACK-capped, not used by any real CLI export path as of 2026-07-25 --
# see session_slot_count for the current one). Kept for test coverage of
# that mechanism; do not reuse this zone for anything new.
_CLIP_ID_SUB_OFFSET = 10_000

# Sub-offset for embedded Arrangement-view clips' NoteIds -- own zone
# (20_000-149_999, 130_000 wide -- 65 clips' worth at _CLIP_ID_STRIDE)
# distinct from _CLIP_ID_SUB_OFFSET's old Session-clip zone (10_000-
# 13_999), _SESSION_MIRROR_CLIP_ID_SUB_OFFSET's zone (150_000+), and
# _DEVICE_ID_SUB_OFFSET's zone (280_000+). build_track_block bounds-
# checks this against _SESSION_MIRROR_CLIP_ID_SUB_OFFSET since, unlike
# Session's old fixed scene-slot cap, Arrangement clip count is
# unbounded.
_ARRANGEMENT_CLIP_ID_SUB_OFFSET = 20_000

# Sub-offset for session_slot_count's Session-view MIRROR clips' NoteIds
# (the CURRENT Session-view mechanism -- see TrackSpec.session_slot_count
# and _set_session_clip_slots) -- own 130_000-wide zone (150_000-279_999,
# same width/reasoning as _ARRANGEMENT_CLIP_ID_SUB_OFFSET, since it
# mirrors the exact same clip list 1:1), distinct from every zone above
# and from _DEVICE_ID_SUB_OFFSET's zone (280_000+) below it.
_SESSION_MIRROR_CLIP_ID_SUB_OFFSET = 150_000

# Sub-offset for a track's own real (non-shadow) Volume/Pan
# AutomationEnvelopes, see AUTOMATION.md. Own 2_000-wide zone per
# envelope (envelope's own Id + up to 1_999 FloatEvent breakpoint Ids),
# volume at 396_000, pan at 398_000 -- placed at the very top of the
# 400_000-wide per-track bucket, well above _DEVICE_ID_SUB_OFFSET's
# (280_000+) realistic usage (would need >23 chained devices at
# _DEVICE_ID_STRIDE to reach this far, not seen in any real project so
# far).
_ENVELOPE_ID_SUB_OFFSET = 396_000
_ENVELOPE_ID_STRIDE = 2_000

# Sub-offset for a track's own Send AutomationTarget/ModulationTarget ids
# (see AUTOMATION.md "Sends", _add_track_sends) -- one `TrackSendHolder`
# per Return Track in the project, each needing 2 fresh globally-unique
# ids (AutomationTarget + ModulationTarget; the TrackSendHolder's own
# `Id="N"` is NOT one of these -- it's a local 0-based list-position
# index, not a global Pointee id, matching exactly what a real Ableton-
# saved file does -- see PLAN.md "Sends"). Placed just below the
# Volume/Pan envelope zone (396_000+), own 3_000-wide zone (393_000-
# 395_999) at _SEND_ID_STRIDE=10 per send comfortably fits far more
# sends than any real project has Return Tracks.
_SEND_ID_SUB_OFFSET = 393_000
_SEND_ID_STRIDE = 10

# Matches an empty Session-view clip slot's `<Value />` in a fresh track
# block (`<ClipSlot><Value /></ClipSlot>`, nested inside the outer
# `<ClipSlot Id="0">...</ClipSlot>` that also holds LomId/HasStop -- see
# PLAN.md "MIDI clip embedding").
_CLIP_SLOT_EMPTY_RE = re.compile(r"<ClipSlot>\s*<Value\s*/>\s*</ClipSlot>")

# A default track template has this many empty Session-view scene slots
# within the MidiTrack block itself (verified against Ableton's own
# "Default MIDI Track.als" -- see PLAN.md "Multiple MIDI items per
# track"; an earlier count of 3 was wrong, it accidentally included a
# clip slot belonging to a sibling element past </MidiTrack>). A REAPER
# track with more MIDI items than this has the extras dropped
# (cli._midi_clips_for_track warns).
MAX_MIDI_CLIPS_PER_TRACK = 2

# Sub-range each clip's NoteIds get within a zone (_CLIP_ID_SUB_OFFSET,
# _ARRANGEMENT_CLIP_ID_SUB_OFFSET, or _SESSION_MIRROR_CLIP_ID_SUB_OFFSET)
# -- generous headroom for realistic per-item note counts (the densest
# real clip seen so far, carrizal.RPP's "chords" items, tops out at 80
# notes).
_CLIP_ID_STRIDE = 2_000


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def file_size_and_crc32(path: str) -> tuple[int, int]:
    """Real byte size + CRC32 of `path` -- see build_audio_clip_xml's
    `original_file_size`/`original_crc` params for why (matches every
    real FileRef's OriginalFileSize/OriginalCrc rather than the old
    hardcoded "0"s). Called by the CLI layer, not by build_audio_clip_xml
    itself -- keeps that function a pure/testable XML builder with no
    filesystem access of its own.

    `OriginalCrc`'s XML int node is SIGNED 32-bit -- `zlib.crc32` returns
    UNSIGNED (0..4294967295), and any value past 2147483647 (roughly a
    50/50 chance per file) makes Ableton reject the whole document on
    load ("Unexpected value for int node") -- confirmed the hard way,
    real crash-free reload on a first file, then this exact error on
    the very next real test file. Wrapped to signed two's-complement
    range here, same underlying bit pattern either way."""
    with open(path, "rb") as f:
        data = f.read()
    unsigned_crc = zlib.crc32(data)
    signed_crc = unsigned_crc - 0x1_0000_0000 if unsigned_crc >= 0x8000_0000 else unsigned_crc
    return len(data), signed_crc


def _controller_target_ids(track_block: str) -> dict[int, str]:
    """MIDI CC number -> this (already _renumber_ids'd) track's own
    ControllerTargets Id -- see _CONTROLLER_TARGET_RE's comment for the
    `+2` index offset."""
    return {int(idx) - 2: target_id for idx, target_id in _CONTROLLER_TARGET_RE.findall(track_block)}


def _stepped_cc_points(events: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """Convert raw (position_beats, value) MIDI CC events (sorted by
    position) into Ableton's step/hold breakpoint pairs -- confirmed
    against a real Ableton-imported clip's own ClipEnvelope (see PLAN.md
    "MIDI CC automation"): a MIDI CC value is a literal instantaneous
    jump, not a ramp, so each value change after the first repeats the
    same Time twice -- once holding the previous value, once landing on
    the new one. A leading Time="-63072000" point (the same sentinel used
    by the Tempo/TimeSignature shadow envelope, see _patch_shadow_envelope)
    establishes the value before the clip starts, matching the reference
    file's own leading point."""
    if not events:
        return []
    points: list[tuple[float, float]] = [(-63072000.0, float(events[0][1]))]
    prev_value = events[0][1]
    for i, (beat, value) in enumerate(events):
        if i > 0:
            points.append((beat, float(prev_value)))
        points.append((beat, float(value)))
        prev_value = value
    return points


def _build_clip_cc_envelope_xml(envelope_id: int, target_id: str, points_beats: list[tuple[float, float]]) -> str:
    """A clip-level `<ClipEnvelope>` for one MIDI CC lane -- same
    Pointee/AutomationTarget linking mechanism as a track's own
    `<AutomationEnvelope>` (_build_track_automation_envelope_xml) but
    scoped to this clip's own `<Envelopes><Envelopes>` sibling list, so
    `envelope_id`/FloatEvent `Id`s only need to be unique within this one
    clip, not globally (same as a clip's own KeyTrack Ids)."""

    def _fmt_time(beat: float) -> str:
        # Plain `:g` renders the -63072000 shadow sentinel as scientific
        # notation ("-6.3072e+07") -- every real Ableton reference file
        # (tempo/timesig shadow envelopes, this clip-envelope example)
        # writes it as a plain integer, so special-case it.
        return "-63072000" if beat == -63072000.0 else f"{beat:g}"

    events = "".join(
        f'<FloatEvent Id="{i}" Time="{_fmt_time(beat)}" Value="{value:g}" />'
        for i, (beat, value) in enumerate(points_beats)
    )
    return (
        f'<ClipEnvelope Id="{envelope_id}">'
        f'<EnvelopeTarget><PointeeId Value="{target_id}" /></EnvelopeTarget>'
        f"<Automation><Events>{events}</Events>"
        '<AutomationTransformViewState><IsTransformPending Value="false" />'
        "<TimeAndValueTransforms /></AutomationTransformViewState>"
        "</Automation>"
        "<LoopSlot><Value /></LoopSlot>"
        '<ScrollerTimePreserver><LeftTime Value="0" /><RightTime Value="0" /></ScrollerTimePreserver>'
        "</ClipEnvelope>"
    )


def build_midi_clip_xml(
    notes: list[MidiNote],
    name: str,
    length_beats: float,
    note_id_offset: int,
    color: int = 0,
    start_beats: float = 0.0,
    clip_id: int = 0,
    loop_length_beats: float | None = None,
    cc_events: dict[int, list[MidiCCEvent]] | None = None,
    controller_target_ids: dict[int, str] | None = None,
    time_signature: tuple[int, int] = (4, 4),
) -> str:
    """Build a `<MidiClip>` block (see templates/midi_clip.xml, a real
    Ableton-exported example with the variable bits templated out) from
    parsed REAPER note data. `note_id_offset` must land in a range
    disjoint from every other Id/NoteId in the document -- NoteId is its
    own globally-unique-across-the-document id family, same rule as the
    Pointee/AutomationTarget system (see PLAN.md "Pointee/LOM Id
    uniqueness"). Notes are grouped into one `<KeyTrack>` per pitch, the
    format Ableton itself uses.

    `start_beats` is the clip's `Time`/`CurrentStart` -- 0.0 for a
    Session-view clip (no timeline position concept there, confirmed
    against a real Session MidiClip: Time="0"/CurrentStart Value="0"),
    or the item's real absolute project position for an Arrangement-view
    clip (confirmed against a real Arrangement MidiClip, e.g. Time=
    "140.771..." -- see PLAN.md "Arrangement-view clip placement").

    `loop_length_beats` sets the clip's own `Loop` region (LoopEnd/
    OutMarker/HiddenLoopEnd) independent of `length_beats` (CurrentEnd) --
    when shorter than `length_beats`, Ableton tiles/repeats the note
    content to fill the gap, matching REAPER's own `LOOP 1` item-source
    behavior (see PLAN.md "Item PLAYRATE + LOOP"). Defaults to
    `length_beats` (loop region == whole clip, i.e. no visible tiling).
    `LoopOn` is derived from this, not hardcoded: only `true` when
    `loop_length_beats` is genuinely shorter than `length_beats` --
    a non-looping item (REAPER `LOOP` flag unset) must NOT get
    `LoopOn="true"`, confirmed by a real Ableton reload where a
    non-looping clip kept restarting instead of just ending (see
    PLAN.md "Item PLAYRATE + LOOP").

    `clip_id` must be unique among sibling clips in whatever list this
    clip ends up in (e.g. multiple MidiClips in one Arrangement TakeLane's
    <Events> list are direct siblings) -- caller's responsibility, same
    "Non-unique list ids" rule as KeyTrack below. Default 0 is fine for a
    clip that's the only member of its list (e.g. a lone Session clip).

    `cc_events` (CC number -> that CC's raw events, unsorted) plus
    `controller_target_ids` (CC number -> this track's own
    ControllerTargets Id, from _controller_target_ids) together embed one
    `<ClipEnvelope>` per CC number present -- see AUTOMATION.md "MIDI CC
    automation". Omit both for a clip with no CC automation.

    `time_signature` sets the clip's own `RemoteableTimeSignature` --
    was hardcoded 4/4 in the template regardless of the actual project
    meter (a real bug, not a deliberate choice: the sibling
    build_audio_clip_xml already had this wired since it needed the
    same field for a different reason -- warp-marker math -- but
    nobody had noticed MIDI clips were left on the template's literal
    default). Purely a clip-editor grid/snap display value, doesn't
    affect playback timing, but is visibly wrong in Ableton's own UI
    -- confirmed via a real project's clip properties panel showing
    "4/4" for a MIDI clip while the same project's audio clips (and
    the transport) correctly showed "3/4"."""
    template = _Templates.midi_clip_template()

    by_pitch: dict[int, list[MidiNote]] = defaultdict(list)
    for note in notes:
        by_pitch[note.pitch].append(note)

    key_tracks = []
    next_note_id = note_id_offset
    for key_track_id, pitch in enumerate(sorted(by_pitch)):
        events = []
        for note in sorted(by_pitch[pitch], key=lambda n: n.position):
            next_note_id += 1
            events.append(
                f'<MidiNoteEvent Time="{note.position:g}" Duration="{note.length:g}" '
                f'Velocity="{note.velocity}" OffVelocity="64" NoteId="{next_note_id}" />'
            )
        # KeyTrack Id must be unique among sibling KeyTracks in this
        # clip's own KeyTracks list ("Non-unique list ids" if not --
        # a real single-pitch clip's sole KeyTrack has Id="0" and that's
        # fine, but multiple pitches means multiple sibling KeyTracks, and
        # they can't all share "0". See PLAN.md "Non-unique list ids".
        key_tracks.append(
            f'<KeyTrack Id="{key_track_id}"><Notes>{"".join(events)}</Notes><MidiKey Value="{pitch}" /></KeyTrack>'
        )

    resolved_loop_length = loop_length_beats if loop_length_beats is not None else length_beats
    xml = template
    xml = xml.replace("{{CLIP_ID}}", str(clip_id))
    xml = xml.replace("{{CLIP_NAME}}", _xml_escape(name))
    xml = xml.replace("{{CLIP_LENGTH}}", f"{length_beats:g}")
    xml = xml.replace("{{LOOP_LENGTH}}", f"{resolved_loop_length:g}")
    xml = xml.replace("{{LOOP_ON}}", "true" if resolved_loop_length < length_beats else "false")
    xml = xml.replace("{{CLIP_START}}", f"{start_beats:g}")
    xml = xml.replace("{{CLIP_END}}", f"{start_beats + length_beats:g}")
    xml = xml.replace("{{CLIP_COLOR}}", str(color))
    xml = xml.replace("{{KEY_TRACKS}}", "".join(key_tracks))
    xml = xml.replace("{{NEXT_NOTE_ID}}", str(next_note_id + 1))
    numerator, denominator = time_signature
    xml = xml.replace("{{TIME_SIG_NUM}}", str(numerator))
    xml = xml.replace("{{TIME_SIG_DEN}}", str(denominator))

    cc_envelopes_xml = ""
    if cc_events:
        for envelope_id, cc_number in enumerate(sorted(cc_events)):
            target_id = (controller_target_ids or {}).get(cc_number)
            if target_id is None:
                continue  # every CC 0-127 has a template slot; should not happen
            points = _stepped_cc_points(
                sorted(((e.position, e.value) for e in cc_events[cc_number]), key=lambda p: p[0])
            )
            cc_envelopes_xml += _build_clip_cc_envelope_xml(envelope_id, target_id, points)
    xml = xml.replace("{{CC_ENVELOPES}}", cc_envelopes_xml)
    return xml


@dataclass
class AudioItemSpec:
    """One REAPER audio item, ready to place as an Arrangement AudioClip --
    see build_audio_clip_xml's docstring for what each field means and the
    coordinate-space subtleties (loop_offset_seconds/loop_length_seconds
    are plain seconds, NOT tempo-scaled like start_beats/length_beats).
    Built by cli._arrangement_audio_clips_for_track."""

    wav_path: str
    name: str
    start_beats: float
    length_beats: float
    duration_frames: int
    sample_rate: int
    loop_offset_seconds: float = 0.0
    loop_length_seconds: float | None = None
    playrate: float = 1.0
    volume: float = 1.0
    pitch_semitones: float = 0.0
    # Path to wav_path RELATIVE TO the .als file's own directory
    # (forward slashes, Ableton's own convention regardless of OS -- see
    # every real FileRef in a genuine Ableton-saved file). Set by
    # cli._bundle_audio_clip_samples once a sample is copied into the
    # export's own Samples/Imported/ folder -- lets Ableton resolve the
    # file on ANY machine the whole exported folder is moved/copied to,
    # not just one where wav_path's absolute path happens to still
    # exist. None (the default) means "only an absolute path is known"
    # (e.g. --copy-samples was not used) -- RelativePathType stays "0"
    # (absolute-only) in that case, same as before this field existed.
    relative_path: str | None = None
    # Real byte size / CRC32 of wav_path, from als_builder.file_size_and_crc32
    # -- see build_audio_clip_xml's original_file_size/original_crc params.
    original_file_size: int = 0
    original_crc: int = 0


def build_audio_clip_xml(
    wav_path: str,
    name: str,
    length_beats: float,
    duration_frames: int,
    sample_rate: int,
    tempo: float,
    start_beats: float = 0.0,
    clip_id: int = 0,
    color: int = 0,
    loop_offset_seconds: float = 0.0,
    loop_length_seconds: float | None = None,
    playrate: float = 1.0,
    time_signature: tuple[int, int] = (4, 4),
    volume: float = 1.0,
    pitch_semitones: float = 0.0,
    relative_path: str | None = None,
    original_file_size: int = 0,
    original_crc: int = 0,
) -> str:
    """Build an `<AudioClip>` block (see templates/audio_clip.xml, a real
    Ableton-exported example -- Chuck Sutton "Patience" factory demo song's
    own Arrangement AudioClip -- with the variable bits templated out) for a
    REAPER audio item.

    Two DIFFERENT export modes, chosen by whether `playrate` (REAPER item
    `PLAYRATE`, first field -- rpp_parse.item_playrate) is 1.0 (untouched
    native speed) AND no tiling is needed, or either of those doesn't hold:

    **`playrate == 1.0` and no loop-tiling needed -- `IsWarped="false"`.**
    Two default `WarpMarker`s spanning the whole file (structurally
    present, functionally inert while unwarped). `Time`/`CurrentStart`/
    `CurrentEnd` are real tempo-scaled Arrangement beats (`start_beats`/
    `length_beats`, same convention as build_midi_clip_xml). The `Loop`
    region (`LoopStart`/`LoopEnd`/etc) is a DIFFERENT, unrelated coordinate
    space here: plain SECONDS, not tempo-scaled beats -- confirmed by an
    exact 17-significant-digit float match between a real Ableton-exported
    unwarped clip's own `LoopEnd` value and its source WAV's raw
    `frames / sample_rate` duration (no tempo term anywhere in that
    match). An earlier version of this function wrongly scaled
    `loop_offset`/`loop_length` by `tempo / 60` like the beat fields,
    which inflated the Loop region and made Ableton visibly speed up
    looped/offset UNWARPED clips on reload (confirmed: 145bpm project,
    ~2.4x too fast, matching `145 / 60`).

    **`playrate != 1.0` OR the item needs loop-tiling (source content
    shorter than the played length) -- `IsWarped="true"`.** REAPER's
    PLAYRATE-driven time-stretch has no unwarped equivalent -- reproducing
    the same audible speed change needs Ableton to actually resample,
    which only happens when warped. Separately, real Ableton apparently
    never actually tiles an UNWARPED clip's Loop bracket across
    `CurrentEnd` in Arrangement even at playrate 1.0 -- confirmed by
    diffing a real reference file (every `LoopOn="true"` AudioClip in the
    Chuck Sutton "Patience" factory demo song is also `IsWarped="true"`,
    zero exceptions) and by a real bug (`voleoModular.RPP`'s
    "base bambuco-glued.wav", playrate 1.0, `LOOP 1`: exported
    unwarped+LoopOn=true, every Loop value read back correct in Ableton's
    own Sample panel, but the clip only played one cycle in the
    Arrangement instead of tiling). So this branch now also triggers
    whenever `loop_length_seconds` is given and shorter than the item's
    played length, using a plain 1:1 warp ratio when `playrate == 1.0` --
    still just two `WarpMarker`s defining a single linear stretch ratio,
    matching REAPER's own: marker0 at
    `(SecTime=loop_offset_seconds, BeatTime=0)`, marker1 at
    `(SecTime=loop_offset_seconds + length_seconds * playrate,
    BeatTime=length_beats)` -- `length_seconds * playrate` is how many
    RAW (native-speed) seconds of the source file get consumed to fill
    the item's own displayed `length_beats`-long span (REAPER: a
    playrate < 1 slows playback down, so filling a given timeline
    duration consumes LESS raw source content than that same duration
    would at native speed -- confirmed against real `carrizal.RPP` data,
    e.g. one item at `PLAYRATE 0.69047619047619`). Once warped, `Loop`
    values switch to the SAME coordinate space as `CurrentStart/End` --
    real tempo-scaled beats, not seconds (confirmed against a real
    Ableton-exported WARPED clip, unlike the unwarped case above) --
    `LoopStart=0` (the offset is already baked into marker0's `SecTime`)
    and `LoopEnd` is the single-cycle raw-seconds loop length
    (`loop_length_seconds`, same meaning as the unwarped case) converted
    via the SAME marker0/marker1 ratio (`length_beats / (length_seconds *
    playrate)` beats per raw second) rather than `tempo / 60` directly --
    the two happen to be equal only when `playrate == 1.0`.

    `time_signature` sets the clip's own `RemoteableTimeSignature` (was
    hardcoded 4/4 in an earlier version regardless of the actual project
    meter -- doesn't affect playback speed, purely the clip's own editor
    grid/snap display, but should still match the project like every
    other clip does).

    `loop_offset_seconds` is REAPER's item `SOFFS <seconds>` (single-field,
    audio-only variant -- see rpp_parse.item_source_offset_seconds) as-is,
    no conversion -- how far into the source file the loop region/
    playback actually starts. `loop_length_seconds` is the single-cycle
    loop length in RAW seconds (`None` for a non-looping item); `LoopOn`
    is derived, not hardcoded, same convention as build_midi_clip_xml --
    only `true` when the cycle is genuinely shorter than the item's own
    played length.

    `clip_id` must be unique among sibling AudioClips in whatever
    Arrangement `<Events>` list this ends up in -- same "Non-unique list
    ids" rule as build_midi_clip_xml's `clip_id`.

    `volume` is REAPER's item-level `VOLPAN` linear gain multiplier
    (rpp_parse.item_volume, unity = 1.0) -- maps directly onto Ableton's
    own `SampleVolume` clip field, same linear-gain convention, no
    remapping needed. REAPER's per-item PAN has no per-clip Ableton
    equivalent (Live only has per-track pan) so it's not represented
    here at all. NOT yet reload-confirmed in real Ableton.

    `relative_path`, when given, is `wav_path`'s path RELATIVE TO the
    `.als` file's own directory (forward slashes) -- written into
    `RelativePathType="3"`/`RelativePath` alongside the always-present
    absolute `Path`. `RelativePathType` codes confirmed against a REAL
    locally-collected Ableton project (MERIYAI.als, user-supplied, 79
    genuine `Samples/Imported/*` FileRefs): "3" is a plain local-project
    relative reference -- NOT "1" (an earlier guess, sourced from
    community docs rather than a real reference, crashed Ableton outright
    on load: EXCEPTION_ACCESS_VIOLATION while loading the document,
    before any plugin/device even loaded -- see Preferences/Log.txt).
    `Type` is likewise "1" for a plain local file (confirmed from the
    same reference) vs "2" for a genuine Ableton-Library-sourced sample
    (confirmed from the Chuck Sutton "Patience" demo song's own Core
    Library FileRefs, RelativePathType="5") -- so `Type` is hardcoded
    "1" here unconditionally (every clip this function builds is from a
    plain user file, never an Ableton Library one). Lets Ableton resolve
    the file if the whole exported project folder is moved to a
    different location or a different machine, not just one where the
    absolute `Path` still happens to exist. `None` (default) keeps the
    old absolute-only behavior (`RelativePathType="0"`, empty
    `RelativePath`) -- appropriate when the sample wasn't copied locally
    (`--copy-samples` not used).

    `original_file_size`/`original_crc` populate `OriginalFileSize`/
    `OriginalCrc` (every real reference FileRef has real, nonzero values
    here, never "0") -- computed by the caller (cli.py, via
    `file_size_and_crc32`) rather than read from disk inside this
    function, so this stays a pure/testable XML builder like every other
    field here (same reasoning as `duration_frames`/`sample_rate` being
    passed in rather than derived internally). Default 0/0 when the
    caller doesn't have/need real values.

    `pitch_semitones` is REAPER's item "pitch adjust" (rpp_parse.
    item_pitch_semitones -- the PLAYRATE line's third field, independent
    of the rate/warp fields above) -- split into whole-semitone
    `PitchCoarse` and the leftover in `PitchFine` (100 cents per
    semitone, Ableton's own convention), matching Ableton's Sample-box
    Transpose knob. 0.0 (no shift) by default."""
    template = _Templates.audio_clip_template()

    length_seconds = length_beats * 60.0 / tempo if tempo else 0.0
    duration_seconds = duration_frames / sample_rate if sample_rate else 0.0
    # A genuinely tiled loop (source content shorter than the item's played
    # length) needs IsWarped="true" even at native playrate==1.0 -- real
    # Ableton apparently never actually repeats an UNWARPED clip's Loop
    # bracket across CurrentEnd in Arrangement. Confirmed by diffing every
    # AudioClip in a real Ableton-saved reference (Chuck Sutton "Patience"
    # factory demo song): every LoopOn="true" clip in that file (5 of them)
    # is also IsWarped="true"; the file's only unwarped clips (3) are all
    # LoopOn="false" -- zero counterexamples either way. Matches a real bug
    # (voleoModular.RPP "base bambuco-glued.wav", playrate==1.0, LOOP 1):
    # exported unwarped+LoopOn=true, all Loop/LoopStart/LoopEnd values read
    # back correct in Ableton's own Sample panel, but the clip played only
    # ONE cycle in the Arrangement instead of tiling.
    needs_tiling = loop_length_seconds is not None and loop_length_seconds < length_seconds
    is_warped = abs(playrate - 1.0) > 1e-9 or needs_tiling

    if is_warped:
        source_content_seconds = length_seconds * playrate
        warp_start_sec = loop_offset_seconds
        warp_end_sec = loop_offset_seconds + source_content_seconds
        warp_end_beat = length_beats
        beats_per_raw_second = length_beats / source_content_seconds if source_content_seconds else 0.0

        # loop_length_seconds (when given) is the RAW remaining file
        # duration past the offset, i.e. how much content is available to
        # tile -- not necessarily shorter than one play-through. Clamp to
        # length_beats whenever it isn't (no tiling needed): leaving the
        # unclamped, much-larger raw value in Loop/OutMarker/HiddenLoopEnd
        # here made non-tiling warped clips keep playing/scrubbable past
        # their own CurrentEnd on reload, even with LoopOn correctly
        # false -- same class of bug as the unwarped branch below, fixed
        # the same way, matching build_midi_clip_xml's convention
        # (`resolved_loop_length = loop_length_beats if given else
        # length_beats`).
        raw_loop_length_beats = (
            loop_length_seconds * beats_per_raw_second if loop_length_seconds is not None else length_beats
        )
        loop_on = raw_loop_length_beats < length_beats
        resolved_loop_length_beats = raw_loop_length_beats if loop_on else length_beats
        loop_start = 0.0
        loop_end = resolved_loop_length_beats
        start_relative = 0.0
    else:
        # Same clamp as the warped branch above, plus a dimensional fix:
        # the no-loop-info default used to be `length_seconds -
        # loop_offset_seconds`, which made LoopEnd = `loop_offset_seconds
        # + (length_seconds - loop_offset_seconds)` = `length_seconds` --
        # silently dropping the offset term entirely instead of the
        # intended `loop_offset_seconds + length_seconds` (the item's own
        # played region in the source file). Invisible whenever
        # loop_offset_seconds was 0 in earlier testing; wrong on any real
        # SOFFS-offset item. Default is now plain `length_seconds`,
        # matching build_midi_clip_xml's `loop_length_beats` convention.
        raw_loop_length_seconds = loop_length_seconds if loop_length_seconds is not None else length_seconds
        loop_on = raw_loop_length_seconds < length_seconds
        resolved_loop_length_seconds = raw_loop_length_seconds if loop_on else length_seconds
        loop_start = loop_offset_seconds
        loop_end = loop_offset_seconds + resolved_loop_length_seconds
        start_relative = -loop_offset_seconds
        warp_start_sec = 0.0
        warp_end_sec = duration_seconds
        warp_end_beat = duration_seconds

    numerator, denominator = time_signature

    xml = template
    xml = xml.replace("{{CLIP_ID}}", str(clip_id))
    xml = xml.replace("{{CLIP_NAME}}", _xml_escape(name))
    xml = xml.replace("{{CLIP_START}}", f"{start_beats:g}")
    xml = xml.replace("{{CLIP_END}}", f"{start_beats + length_beats:g}")
    xml = xml.replace("{{LOOP_START}}", f"{loop_start:g}")
    xml = xml.replace("{{LOOP_END}}", f"{loop_end:g}")
    xml = xml.replace("{{START_RELATIVE}}", f"{start_relative:g}")
    xml = xml.replace("{{LOOP_ON}}", "true" if loop_on else "false")
    xml = xml.replace("{{CLIP_COLOR}}", str(color))
    xml = xml.replace("{{IS_WARPED}}", "true" if is_warped else "false")
    xml = xml.replace("{{TIME_SIG_NUM}}", str(numerator))
    xml = xml.replace("{{TIME_SIG_DEN}}", str(denominator))
    # RelativePathType/RelativePath/Type/OriginalFileSize/OriginalCrc are
    # ALL forced to the plain absolute-Path-only state
    # (RelativePathType="0", RelativePath="", Type="2",
    # OriginalFileSize/OriginalCrc="0") regardless of `relative_path`/
    # `original_file_size`/`original_crc` -- TWO separate attempts at
    # real portability both crashed real Ableton on load, tried in this
    # order:
    #   1. RelativePathType="1" (sourced from community docs, not a real
    #      reference) -- EXCEPTION_ACCESS_VIOLATION during document
    #      parsing, before any plugin/device even loaded.
    #   2. RelativePathType="3" (matched field-for-field against a real
    #      locally-collected Ableton project, MERIYAI.als, including
    #      Type="1"/real OriginalFileSize+signed-int32 OriginalCrc) --
    #      loaded further (got to actually opening samples) but then
    #      failed to open EVERY sample despite each one existing exactly
    #      where RelativePath said, then crashed the same way. Turned out
    #      MERIYAI.als's own RelativePath/Samples-Imported layout doesn't
    #      even match ITS current on-disk layout either (the WAVs sit
    #      loose next to it, no Samples/Imported folder at all) -- so
    #      that reference file was never actually proof the mechanism
    #      works when the path resolves; whatever keeps IT loadable is
    #      some other Ableton-internal relink fallback, not this.
    #      RelativePathType stays "0"/absolute-Path-only here, the only
    #      confirmed-safe state -- BUT this turned out not to matter for
    #      actual cross-machine portability: user real-world-tested
    #      (2026-07-28) sending an export via OneDrive to a genuinely
    #      different machine (different user account, different drive
    #      layout, so the absolute Path could not possibly have
    #      resolved) and it played correctly, no missing-samples dialog.
    #      Confirms real Ableton has its OWN filename-based relink
    #      fallback that searches the project's own `Samples/Imported/`
    #      folder (exactly where --copy-samples already puts everything,
    #      see cli._bundle_audio_clip_samples) when the absolute Path
    #      fails to resolve -- this FileRef mechanism was never actually
    #      the blocker. See PLAN.md "Cross-machine sample portability"
    #      for the full writeup. `relative_path`/`original_file_size`/
    #      `original_crc` params are kept (unused) in case a future need
    #      arises (e.g. Ableton's own "Collect All and Save" wanting
    #      real RelativePath data specifically), not because portability
    #      itself is still an open problem.
    xml = xml.replace("{{SAMPLE_PATH}}", _xml_escape(wav_path))
    xml = xml.replace("{{RELATIVE_PATH_TYPE}}", "0")
    xml = xml.replace("{{RELATIVE_PATH}}", "")
    xml = xml.replace("{{FILE_TYPE}}", "2")
    xml = xml.replace("{{ORIGINAL_FILE_SIZE}}", "0")
    xml = xml.replace("{{ORIGINAL_CRC}}", "0")
    xml = xml.replace("{{SAMPLE_DURATION}}", str(duration_frames))
    xml = xml.replace("{{SAMPLE_RATE}}", str(sample_rate))
    xml = xml.replace("{{SAMPLE_VOLUME}}", f"{volume:.10g}")
    pitch_coarse = int(pitch_semitones)
    pitch_fine = round((pitch_semitones - pitch_coarse) * 100)
    xml = xml.replace("{{PITCH_COARSE}}", str(pitch_coarse))
    xml = xml.replace("{{PITCH_FINE}}", str(pitch_fine))
    xml = xml.replace("{{WARP_START_SEC}}", f"{warp_start_sec:g}")
    xml = xml.replace("{{WARP_END_SEC}}", f"{warp_end_sec:g}")
    xml = xml.replace("{{WARP_END_BEAT}}", f"{warp_end_beat:g}")
    # Ableton warp algorithm -- 0=Beats (granular, transient/grid-snapped,
    # was the hardcoded template default inherited from the real
    # reference file it's templated from -- never a deliberate choice).
    # Beats mode audibly chops/re-quantizes continuous melodic material
    # to the beat grid instead of a smooth linear stretch -- confirmed by
    # the user in real Ableton on a warped+looped tiple (melodic string)
    # line at PLAYRATE 1.4 (chirriado.RPP "tp" track): sounded slowed
    # down then jumped to a later position in the source to finish the
    # bar, the exact granular-resynthesis artifact Beats mode produces.
    # 4=Complex is Ableton's general-purpose algorithm, correct default
    # for anything that isn't a pure drum/percussion loop.
    xml = xml.replace("{{WARP_MODE}}", "4")
    return xml


def _embed_clips(track_block: str, clip_xmls: list[str]) -> str:
    """Insert MidiClips into a freshly-cloned track block's empty
    Session-view clip slots, one per slot in order (topmost scene first).
    Raises if there aren't enough empty slots -- callers should cap the
    clip count at MAX_MIDI_CLIPS_PER_TRACK first."""
    block = track_block
    for clip_xml in clip_xmls:
        block, count = _CLIP_SLOT_EMPTY_RE.subn(
            lambda m: f"<ClipSlot><Value>{clip_xml}</Value></ClipSlot>", block, count=1
        )
        if count != 1:
            raise ValueError("not enough empty Session-view clip slots in track block")
    return block


# Matches a track block's own `<ClipSlotList>...</ClipSlotList>` for
# wholesale replacement by _set_session_clip_slots -- unlike _embed_clips
# (which fills individual pre-existing empty slots one at a time, capped
# at whatever the template shipped with), this regenerates the ENTIRE
# list with however many slots session_slot_count calls for. TWO
# <ClipSlotList> elements exist per fresh track block, not one --
# MainSequencer's (the real Session-view one) AND an identically-shaped
# FreezeSequencer's (used only if the track is ever frozen, otherwise
# inert) -- confirmed against Ableton's own factory track templates
# (Default Audio/MIDI Track.als). A real reload ("documento dañado") on
# the first version of this feature confirmed BOTH must be resized to
# the SAME slot COUNT -- replacing only the first (MainSequencer's) left
# FreezeSequencer's stale/mismatched-length ClipSlotList behind, which
# alone was enough to corrupt the file. This regex/subn now targets ALL
# matches (`count=0`) -- but content must NOT also be duplicated into
# both: a MidiClip's NoteId is documented as globally unique across the
# WHOLE document (see build_midi_clip_xml), and an early version of this
# fix that wrote the SAME populated clip_xmls into FreezeSequencer too
# silently duplicated every NoteId (caught structurally before reload --
# 6441 total NoteIds, only 4294 unique -- not by another real-Ableton
# failure, but would very likely have been one). FreezeSequencer's slots
# are always written EMPTY regardless of clip_xmls -- correct AND
# simpler, since freeze content is never used unless the track is
# actually frozen.
_CLIP_SLOT_LIST_RE = re.compile(r"<ClipSlotList>.*?</ClipSlotList>", re.DOTALL)


def _build_empty_session_clip_slot_xml(slot_id: int) -> str:
    return (
        f'<ClipSlot Id="{slot_id}"><LomId Value="0" />'
        f"<ClipSlot><Value /></ClipSlot><HasStop Value=\"true\" /></ClipSlot>"
    )


def _build_populated_session_clip_slot_xml(slot_id: int, clip_xml: str) -> str:
    return (
        f'<ClipSlot Id="{slot_id}"><LomId Value="0" />'
        f"<ClipSlot><Value>{clip_xml}</Value></ClipSlot><HasStop Value=\"true\" /></ClipSlot>"
    )


def _set_session_clip_slots(track_block: str, clip_xmls: list[str | None], total_slots: int) -> str:
    """Regenerate a track's ENTIRE Session-view `<ClipSlotList>` from
    scratch with exactly `total_slots` `<ClipSlot>` entries -- the first
    `len(clip_xmls)` populated with this track's own items (in order),
    the rest left empty. Unlike `_embed_clips` (which fills pre-existing
    template slots one at a time, capped at whatever the template
    shipped with -- MAX_MIDI_CLIPS_PER_TRACK), this can produce however
    many slots the whole project's tallest track needs. Every track in
    one Live Set MUST be called with the SAME `total_slots` (the
    project-wide max item count across all tracks, computed by the
    caller), AND the LiveSet-level `<Scenes>` list must ALSO be grown to
    the same length (see `_set_scenes_count`) -- confirmed by a real
    Ableton reload failure ("documento dañado y no se puede cargar") that
    all three (MainSequencer's ClipSlotList, FreezeSequencer's, and the
    top-level Scenes list) must match exactly; an isolated single-track
    factory template file that seemed to show otherwise
    ("Default MIDI Track.als": 2 ClipSlots but only 1 Scene) turned out
    to be a red herring -- that file is only ever used as a NEW-TRACK
    template merged into an existing project, its own `<Scenes>` value
    is discarded, never load-tested as a standalone LiveSet. Each
    `ClipSlot`'s own `Id` only needs to be unique among these slots (a
    real sibling list, `<ClipSlotList>`'s direct children) --
    0..total_slots-1 here. Replaces the template's existing
    `<ClipSlotList>` wholesale (both occurrences, see _CLIP_SLOT_LIST_RE),
    not additive -- safe to call on a block whose slot count doesn't
    match what's being written."""
    if total_slots < len(clip_xmls):
        raise ValueError(f"{len(clip_xmls)} Session clips requested but only {total_slots} slots available")
    populated_slots = [
        _build_populated_session_clip_slot_xml(i, xml) if xml is not None else _build_empty_session_clip_slot_xml(i)
        for i, xml in enumerate(clip_xmls)
    ]
    populated_slots += [_build_empty_session_clip_slot_xml(i) for i in range(len(clip_xmls), total_slots)]
    empty_only_slots = [_build_empty_session_clip_slot_xml(i) for i in range(total_slots)]
    main_replacement = "<ClipSlotList>" + "".join(populated_slots) + "</ClipSlotList>"
    freeze_replacement = "<ClipSlotList>" + "".join(empty_only_slots) + "</ClipSlotList>"

    match_count = 0

    def _sub(_m: re.Match) -> str:
        nonlocal match_count
        match_count += 1
        return main_replacement if match_count == 1 else freeze_replacement

    new_block, n = _CLIP_SLOT_LIST_RE.subn(_sub, track_block, count=0)
    if n != 2:
        raise ValueError(
            f"expected exactly 2 ClipSlotList elements (MainSequencer + FreezeSequencer) in track block, found {n}"
        )
    return new_block


# Matches a fresh track block's PRIMARY Arrangement clip list --
# `<ClipTimeable><ArrangerAutomation><Events /></ArrangerAutomation>
# </ClipTimeable>`, a sibling of MainSequencer's Session-view
# `<ClipSlotList>`, NOT the `<TakeLanes>` element (a previous version of
# this code wrongly targeted `<TakeLanes>`, which is the take-COMPING
# overlay for alternate recorded takes -- structurally valid and even
# reload-clean, but invisible in the Arrangement by default since it's a
# secondary view layered on top of the real clip list, not the real list
# itself; found by diffing a real Ableton-saved Arrangement clip's full
# ancestor chain, which showed `<ClipTimeable><ArrangerAutomation>
# <Events><MidiClip>`, not `<TakeLanes>`). The `<ClipTimeable>` anchor is
# required because the same empty `<ArrangerAutomation><Events />` shape
# also appears (structurally unrelated) under `<Sample>` elsewhere in the
# same track template. See PLAN.md "Arrangement-view clip placement".
_ARRANGER_AUTOMATION_EMPTY_RE = re.compile(r"(<ClipTimeable>\s*<ArrangerAutomation>\s*)<Events\s*/>")


def _embed_arrangement_clips(track_block: str, clip_xmls: list[str]) -> str:
    """Insert MidiClips into a freshly-cloned track block's Arrangement
    view, in the track's real Arrangement clip list (`ClipTimeable`'s
    `ArrangerAutomation` `Events`), each clip carrying its own absolute
    `Time`/`CurrentStart` (see build_midi_clip_xml's `start_beats`).
    Unlike Session-view clip slots, there's no fixed capacity here -- any
    number of clips can go in this `Events` list, so this isn't subject
    to MAX_MIDI_CLIPS_PER_TRACK."""
    result, count = _ARRANGER_AUTOMATION_EMPTY_RE.subn(
        rf"\g<1><Events>{''.join(clip_xmls)}</Events>", track_block, count=1
    )
    if count != 1:
        raise ValueError("no empty ClipTimeable/ArrangerAutomation/Events list found in track block")
    return result


# An AudioTrack's real Arrangement-view clip list lives at a DIFFERENT spot
# than a MidiTrack's `<ClipTimeable><ArrangerAutomation><Events>` (see
# _ARRANGER_AUTOMATION_EMPTY_RE above) -- an AudioTrack's own default
# template has no `<ClipTimeable>` element at all. Found by comparing a
# real Ableton-saved Arrangement AudioClip's full ancestor chain (Chuck
# Sutton "Patience" factory demo song) against `Default Audio Track.als`:
# the real clip's nearest ancestor is `<MainSequencer><Sample>
# <ArrangerAutomation><Events><AudioClip>`, not `<ClipTimeable>`. The raw
# `Default Audio Track.als` template has multiple structurally-identical
# empty `<Sample><ArrangerAutomation><Events />` spots -- the real
# MainSequencer's own plus 0+ FreezeSequencer copies, this codebase's own
# extracted audio_track_block template currently has 2 total (this count
# has drifted before, e.g. down from 3 in an earlier version -- don't
# assume a fixed total). What's stable and load-bearing is that the real
# MainSequencer one is always immediately preceded by
# `<MonitoringEnum Value="2" />` (FreezeSequencer copies are preceded by
# `Value="1"` instead) -- match on that directly instead of trusting
# document order ("first match") or a specific total count.
_AUDIO_ARRANGER_AUTOMATION_EMPTY_RE = re.compile(
    r'(<MonitoringEnum Value="2"\s*/>.*?<Sample>\s*<ArrangerAutomation>\s*)<Events\s*/>', re.DOTALL
)


def _embed_arrangement_audio_clips(track_block: str, clip_xmls: list[str]) -> str:
    """Insert AudioClips into a freshly-cloned AudioTrack block's real
    Arrangement clip list (MainSequencer's own `Sample/ArrangerAutomation/
    Events`, not one of the FreezeSequencer copies -- see
    _AUDIO_ARRANGER_AUTOMATION_EMPTY_RE). No fixed capacity, like
    _embed_arrangement_clips for MIDI."""
    result, count = _AUDIO_ARRANGER_AUTOMATION_EMPTY_RE.subn(
        lambda m: f"{m.group(1)}<Events>{''.join(clip_xmls)}</Events>", track_block, count=1
    )
    if count != 1:
        raise ValueError(
            f"expected exactly one MainSequencer Sample/ArrangerAutomation/Events list "
            f"(preceded by MonitoringEnum Value=\"2\") in AudioTrack block, found {count}"
        )
    return result


@dataclass
class TrackSpec:
    track_type: str  # "MIDI" or "Audio"
    name: str
    device_xml: str | list[str] | None = None  # single device, or several chained in FX-chain order
    # Track-level mixer fader (rpp_parse.track_volume, linear gain, unity
    # = 1.0) and pan (rpp_parse.track_pan, -1..1) -- see build_track_block
    # for Ableton's own Volume/Pan field ranges/conventions.
    volume: float = 1.0
    pan: float = 0.0
    # Track-level Volume/Pan automation breakpoints -- [(time_seconds,
    # value), ...] from rpp_parse.track_volume_envelope/track_pan_envelope,
    # or None for a static (unautomated) fader/pan. See AUTOMATION.md and
    # build_track_block for the Ableton-side AutomationEnvelope wiring.
    volume_automation: list[tuple[float, float]] | None = None
    pan_automation: list[tuple[float, float]] | None = None
    midi_notes: list[MidiNote] | None = None  # embeds a Session-view MidiClip if given
    midi_clip_length_beats: float = 4.0
    # Additional MIDI items beyond the first (each its own scene slot, up
    # to MAX_MIDI_CLIPS_PER_TRACK total including midi_notes above) -- a
    # REAPER track can have more than one MIDI item; Session view has no
    # "position on timeline" concept, so each extra item just goes in the
    # next scene slot in item-start order, not a true absolute-position
    # placement. Superseded by arrangement_midi_clips below when true
    # absolute positioning is wanted -- kept for callers that still want
    # Session-view clips specifically.
    extra_midi_clips: list[tuple[list[MidiNote], float]] | None = None
    # Arrangement-view clips, each at its own real project-timeline
    # position: [(notes, start_beats, length_beats), ...] or
    # [(notes, start_beats, length_beats, loop_length_beats), ...] --
    # the 4th element is optional (defaults to length_beats, i.e. no
    # visible loop tiling) for callers that don't care about REAPER's
    # LOOP-source behavior. Unlike Session clip slots there's no fixed
    # capacity -- any number of items can go here. See PLAN.md
    # "Arrangement-view clip placement", "Item PLAYRATE + LOOP". A 5th
    # optional element, {cc_number: [MidiCCEvent, ...]}, embeds one
    # ClipEnvelope per CC number present -- see AUTOMATION.md "MIDI CC
    # automation".
    arrangement_midi_clips: (
        list[
            tuple[list[MidiNote], float, float]
            | tuple[list[MidiNote], float, float, float]
            | tuple[list[MidiNote], float, float, float | None, dict[int, list[MidiCCEvent]]]
        ]
        | None
    ) = None
    # Arrangement-view audio clips (Audio tracks only), one AudioItemSpec
    # per REAPER audio item. See AudioItemSpec and build_audio_clip_xml's
    # docstring for the coordinate-space subtleties, and
    # cli._arrangement_audio_clips_for_track for how these are built.
    arrangement_audio_clips: list[AudioItemSpec] | None = None
    # When set, MIRRORS arrangement_midi_clips/arrangement_audio_clips
    # (whichever is set -- a track is one or the other, see cli.py's
    # mixed-track splitting) into this many Session-view ClipSlots: the
    # track's own items, in the same order, at slot 0..len(items)-1,
    # remaining slots up to session_slot_count left empty. Every track in
    # one Live Set gets the SAME session_slot_count (the project-wide max
    # item count across all tracks) so Session view stays a rectangular
    # grid -- see build_track_block / _set_session_clip_slots and PLAN.md
    # "Session view clips". 0 (default) skips Session embedding entirely,
    # leaving the track template's own stock empty slot(s) untouched.
    session_slot_count: int = 0
    # When set (scene-export mode, see cli.py --scenes), overrides the
    # dense item-order-0..N Session mapping above: scene_slot_indices[i]
    # is the Scene index (into session_slot_count slots) that the i-th
    # entry of arrangement_midi_clips/arrangement_audio_clips belongs in,
    # one REAPER marker-to-marker region per Scene. Must be the same
    # length as whichever arrangement_*_clips list is set. Slots with no
    # item mapped to them are left empty.
    scene_slot_indices: list[int] | None = None
    # Aux-send levels to this project's Return Tracks, positional (index i
    # = the i-th Return Track passed to build_liveset_xml's return_tracks
    # list) -- linear gain per rpp_parse.track_aux_recv_entries, or None
    # at a given position for "no send to that return". None (default)
    # means this track has no <Sends> at all, i.e. it isn't itself a
    # sender (only bus/return tracks and untouched tracks skip this).
    # See AUTOMATION.md "Sends".
    sends: list[float | None] | None = None


def build_surge_instrument_device_xml(processor_state_hex: str) -> str:
    """Build a `<PluginDevice>` block embedding Surge XT with a REAPER
    project's own patch state transplanted in -- see PLAN.md "FX/device
    matching research" for how this was derived and confirmed.

    `templates/vst3_surge_instrument_device.xml` is a real Ableton-
    exported `<PluginDevice>` (a Surge XT instance, extracted from a
    reference `.als` the user saved loading the SAME presets as
    `chirriado.RPP`) with its `<ProcessorState>` hex blob replaced by the
    `{{PROCESSOR_STATE_HEX}}` token -- everything else (I/O counts,
    `<Uid>` GUID fields, `<BranchDeviceId>`, the full per-parameter
    `AutomationTarget`/`Pointee` scaffolding Ableton always writes for
    every one of Surge's ~390 automatable params) is IDENTICAL for any
    Surge XT instance regardless of preset, since none of it encodes the
    patch itself -- only `<ProcessorState>` does. The template's own raw
    `Id="N"` values were pre-compacted to a small sequential range
    (1..390) before saving (the real reference file's own ids were
    already large, in the hundreds of thousands, having been assigned
    globally across a whole other project -- adding the usual small
    per-track sub-offset to those directly would blow through the
    per-track id bucket every other embedded block in this
    file relies on; confirmed none of the 390 ids are referenced from
    elsewhere as a pointer/PointeeId pair, so compacting them was safe).
    Caller still renumbers via the normal `_embed_device`/`_renumber_ids`
    path like any other device.

    `processor_state_hex` -- see `rpp_parse.vst3_processor_state_hex`,
    the uppercase hex string ready to drop in verbatim."""
    return _Templates.vst3_surge_device_template().replace("{{PROCESSOR_STATE_HEX}}", processor_state_hex)


def build_waves_rverb_device_xml(processor_state_hex: str) -> str:
    """Build a `<PluginDevice>` block embedding Waves RVerb with a REAPER
    project's own patch state transplanted in -- sibling to
    `build_surge_instrument_device_xml`, see PLAN.md "FX/device matching
    research". Same idea, different reference file/wrapper format
    (`rpp_parse.waves_vst3_processor_state_hex` instead of
    `vst3_processor_state_hex`), same id-compaction treatment
    (`templates/vst3_waves_rverb_device.xml`'s own ~390 ids pre-compacted
    to 1..390 the same way, same "no internal pointer references"
    confirmation).

    Unlike Surge (an instrument, `DeviceType Value="1"`, on a MIDI
    track), RVerb is an audio effect (`DeviceType Value="2"`) with 1
    audio input/1 audio output (mono) -- both already baked into the
    template since Waves plugins are identified by a fixed real VST3
    GUID same as Surge (confirmed: REAPER's own stored `{56535452...}`
    id decodes to the same big-endian int32 `<Uid><Fields.0-3>` split
    Ableton uses, and to the same ASCII-derived `BranchDeviceId` -- this
    is Waves' own genuine per-sub-plugin VST3 class id, not something
    either host synthesizes)."""
    return _Templates.vst3_waves_rverb_device_template().replace("{{PROCESSOR_STATE_HEX}}", processor_state_hex)


def build_waves_cla_guitars_device_xml(processor_state_hex: str) -> str:
    """Build a `<PluginDevice>` block embedding Waves CLA Guitars with a
    REAPER project's own patch state transplanted in -- sibling to
    `build_waves_rverb_device_xml`, same wrapper format
    (`rpp_parse.waves_vst3_processor_state_hex`, generalized to any
    Waves plugin's own `<4-char-code>setA` tag -- CLA Guitars uses
    `"CGTX"`, confirmed distinct from RVerb's `"RRFX"`), same
    `DeviceType Value="2"` (audio effect) and mono 1in/1out. Confirmed
    via `templates/vst3_waves_cla_guitars_device.xml`, extracted from a
    real reference `.als` where the user chained BOTH CLA Guitars and
    RVerb on the same track -- also incidentally confirmed the two
    devices' own `<PluginDevice Id="0">`/`Id="1"` really do need to
    differ as a real sibling list, matching CLAUDE.md's documented
    id-uniqueness rule."""
    return _Templates.vst3_waves_cla_guitars_device_template().replace("{{PROCESSOR_STATE_HEX}}", processor_state_hex)


def build_reajs_device_xml(processor_state_hex: str) -> str:
    """Build a `<PluginDevice>` block embedding ReaJS (part of ReaPlugs,
    hosting the same JSFX effect a REAPER `<JS ...>` FX instance names)
    with the FX's own name/params transplanted in.

    `templates/vst2_reajs_device.xml` is a real Ableton-exported
    `<PluginDevice>` (a `reajs` VST2 instance loading `Stillwell/
    delay_pong`, extracted from a reference `.als`) with its
    `<Buffer>` hex blob replaced by `{{PROCESSOR_STATE_HEX}}`. ReaJS
    always exposes the same fixed 64 generic `Parm1..Parm64` parameter
    slots regardless of which `.jsfx` script is actually loaded (same
    generic-fixed-interface shape as Waves' WaveShell wrapper), so this
    ONE template covers any JS FX instance -- no per-effect template
    needed, unlike native ReaPlugs effects (see
    `build_readelay_device_xml`). `processor_state_hex` --
    `rpp_parse.reajs_processor_state_hex`."""
    return _Templates.vst2_reajs_device_template().replace("{{PROCESSOR_STATE_HEX}}", processor_state_hex)


def build_simpler_sine_device_xml() -> str:
    """A plain sine Simpler standing in for REAPER's ReaSynth, which has
    no Ableton equivalent -- just enough to make a MIDI track sound. No
    ReaSynth settings are carried over (user's call, 2026-09-27).
    Template built from Ableton's own Supersine preset by
    `tools/build_simpler_sine_template.py`, see its docstring; its
    sample is a Core-Library-relative reference, nothing to bundle."""
    with open(_SIMPLER_SINE_DEVICE_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def build_readelay_device_xml(processor_state_hex: str) -> str:
    """Build a `<PluginDevice>` block embedding ReaDelay (a native
    ReaPlugs VST2 effect) with a REAPER project's own patch state
    transplanted in.

    `templates/vst2_readelay_device.xml` is a real Ableton-exported
    `<PluginDevice>` (a `readelay-standalone` VST2 instance) with its
    `<Buffer>` replaced by `{{PROCESSOR_STATE_HEX}}`. Unlike Waves'
    shared WaveShell wrapper, each ReaPlugs effect is its own distinct
    VST2 binary with its own parameter layout -- this template is
    ReaDelay-specific, NOT a generic stand-in for other Rea* effects;
    each new Rea* plugin needs its own reference-extracted template the
    same way Surge/RVerb/CLA Guitars each needed one before Waves'
    generic fallback was confirmed safe. `processor_state_hex` --
    `rpp_parse.vst2_processor_state_hex`."""
    return _Templates.vst2_readelay_device_template().replace("{{PROCESSOR_STATE_HEX}}", processor_state_hex)


def build_waves_generic_effect_device_xml(
    processor_state_hex: str, plugin_name: str, uid_fields: tuple[int, int, int, int], branch_device_id: str
) -> str:
    """Build a `<PluginDevice>` for a Waves effect that has NO dedicated
    reference-extracted template of its own, by reusing RVerb's template
    as a generic skeleton and substituting the parts that differ per
    plugin. Reload-confirmed working in real Ableton (2026-07-25, CLA
    Unplugged + CLA Bass) -- the underlying hypothesis (below) holds.

    Rationale: byte-diffing the RVerb and CLA Guitars templates against
    each other (both real, independently reference-extracted) showed
    them structurally IDENTICAL -- same 7487 lines, same 128 generic
    `<PluginFloatParameter>` slots, same 1in/1out mono I/O -- differing
    ONLY in cosmetic/plugin-identity fields (GUID, display name,
    per-parameter labels/initial values) that are never consulted for
    actual playback (confirmed by every prior Waves transplant: only
    `<ProcessorState>` was ever touched, and sound was correct). This
    strongly suggests Waves' WaveShell wrapper always exposes the same
    fixed generic interface regardless of which specific plugin is
    loaded -- if true, ANY WaveShell-hosted Waves effect can be built
    this way with no new reference file, only REAPER's own already-
    available data (GUID from the `<VST ...>` header, state via
    `rpp_parse.waves_vst3_processor_state_hex`).

    `plugin_name` -- the plugin's own display name (e.g. `"CLA Bass
    Stereo"`), used for both `<Name>` and (URL-encoded) the cosmetic
    `<BrowserContentPath>`. `uid_fields` -- the 4 big-endian int32s from
    splitting the plugin's raw 16-byte VST3 GUID (same GUID REAPER
    stores in its own `<VST ...>` header's `{HEXID}`) --
    `struct.unpack(">i", guid[i:i+4])[0]` for `i` in `0,4,8,12`.
    `branch_device_id` -- the same GUID as a dashed lowercase hex string
    (`device:vst3:audiofx:` prefix added here).

    KNOWN LIMITATION: doesn't verify the target plugin actually has
    1in/1out mono I/O or exactly 128 parameter slots like RVerb/CLA
    Guitars did -- if a future Waves plugin turns out to need a
    different shape, this will need a real reference-extracted template
    instead, same as the original two."""
    xml = _Templates.vst3_waves_rverb_device_template()
    xml = xml.replace('<BrowserContentPath Value="query:Everything#RVerb%20Mono/Stereo" />', f'<BrowserContentPath Value="query:Everything#{plugin_name.replace(" ", "%20")}" />')
    xml = xml.replace(
        '<BranchDeviceId Value="device:vst3:audiofx:56535452-5246-5872-7665-7262206d6f6e" />',
        f'<BranchDeviceId Value="device:vst3:audiofx:{branch_device_id}" />',
    )
    xml = xml.replace('<Name Value="RVerb Mono/Stereo" />', f'<Name Value="{plugin_name}" />')
    old_fields = (1448301650, 1380341874, 1986359906, 544042862)
    for i, (old, new) in enumerate(zip(old_fields, uid_fields)):
        xml = xml.replace(f'<Fields.{i} Value="{old}" />', f'<Fields.{i} Value="{new}" />')
    return xml.replace("{{PROCESSOR_STATE_HEX}}", processor_state_hex)


# Spacing between successive devices chained on the same track (e.g.
# CLA Guitars -> RVerb, matching a REAPER FX chain's own order) within
# _DEVICE_ID_SUB_OFFSET's 50_000-wide zone. Each device template's own
# compacted ids top out at ~390 (see build_surge_instrument_device_xml,
# build_waves_rverb_device_xml) -- 5_000 leaves enormous headroom per
# device, and the whole zone (50_000-99_999) comfortably fits ~10
# chained devices before touching the next track's own base offset.
_DEVICE_ID_STRIDE = 5_000


_DEVICE_ROOT_ID_RE = re.compile(r'^<(\w+) Id="\d+"')


def _embed_device(track_block: str, device_xml: str | list[str], id_offset: int) -> str:
    """Insert one or more devices (e.g. a DrumGroupDevice, or several
    chained VST3 effect PluginDevices in REAPER FX-chain order) into a
    freshly-cloned track block's empty `<Devices />` list. Each device is
    renumbered by its own `id_offset + i * _DEVICE_ID_STRIDE` (`i` = its
    position in the chain) so multiple devices' own internal ids -- each
    already pre-compacted to a small range in their own template -- can't
    collide with each other, still within the single `_DEVICE_ID_SUB_OFFSET`
    zone the caller already reserved.

    Each device template's own root tag (e.g. `<PluginDevice Id="0">`,
    `<DrumGroupDevice Id="0">`) carries whatever raw id it happened to
    have in its own source reference file -- usually `"0"`, but not
    always (a device extracted while it was the SECOND device already
    chained in its own reference, like CLA Guitars, keeps a small
    nonzero raw id like `"1"` after compaction). Either way this needs
    forcing to a value that's actually unique among this track's OTHER
    embedded devices: literal `"0"` is always left alone by
    `_renumber_ids` (Ableton's null-pointee sentinel everywhere --
    EXCEPT when the element is a member of a real sibling list, see
    CLAUDE.md), and even a small nonzero raw id could coincidentally
    collide with another chained device's own compacted range. A lone
    embedded device was fine either way (a 1-member list has no sibling
    to collide with, confirmed by 3+ real-Ableton reload tests), but
    chaining 2+ devices onto one track makes `<Devices>` a genuine
    multi-member sibling list -- found via exactly this bug when
    chaining CLA Guitars + RVerb, RVerb's root stayed literally `Id="0"`
    even after offsetting. Force each device's own root id to its own
    nonzero, unique-per-device `offset` after the generic renumbering
    pass, same fix pattern `build_track_block` already uses for the
    track's own root id."""
    devices = [device_xml] if isinstance(device_xml, str) else device_xml
    n = len(_DEVICES_EMPTY_RE.findall(track_block))
    if n != 1:
        raise ValueError(f"expected exactly one empty <Devices /> in track block, found {n}")
    parts = []
    for i, xml in enumerate(devices):
        offset = id_offset + i * _DEVICE_ID_STRIDE
        renumbered = _renumber_ids(xml, offset)
        renumbered, subbed = _DEVICE_ROOT_ID_RE.subn(rf'<\g<1> Id="{offset}"', renumbered, count=1)
        if subbed != 1:
            raise ValueError("could not find device's own root Id=\"0\" to force nonzero (template format may have changed)")
        parts.append(renumbered)
    return _DEVICES_EMPTY_RE.sub(lambda m: f"<Devices>{''.join(parts)}</Devices>", track_block, count=1)


def _add_track_sends(track_block: str, send_levels: list[float | None], id_offset: int) -> str:
    """Populate a track's own empty `<Sends />` (see _SENDS_EMPTY_RE) with
    one `<TrackSendHolder>` per Return Track in the project (`i` = the
    Return Track's position, matching build_liveset_xml's return_tracks
    order) -- confirmed against a real Ableton-saved file with genuine
    sends (Chuck Sutton "Patience" demo, see PLAN.md "Sends") that EVERY
    regular track carries one holder per Return Track, even ones with no
    audible send: `send_levels[i] is None` (or the list is shorter than
    the project's Return Track count) writes the same floor value/
    disabled state a real untouched send uses, `send_levels[i]` a real
    linear gain writes an enabled, real-valued one. `TrackSendHolder`'s
    own `Id="i"` is a local list-position index (0, 1, 2...), NOT a
    global Pointee id -- matches the real reference file exactly, no
    renumbering needed for it. The `AutomationTarget`/`ModulationTarget`
    ids inside each `<Send>` ARE global Pointee ids and get fresh ones
    from _SEND_ID_SUB_OFFSET's zone."""
    n = len(_SENDS_EMPTY_RE.findall(track_block))
    if n != 1:
        raise ValueError(f"expected exactly one empty <Sends /> in track block, found {n}")
    floor = 0.0003162277571
    holders = []
    for i, level in enumerate(send_levels):
        target_id = id_offset + _SEND_ID_SUB_OFFSET + i * _SEND_ID_STRIDE
        mod_id = target_id + 1
        enabled = level is not None
        manual = max(floor, min(1.0, level)) if enabled else floor
        holders.append(
            f'<TrackSendHolder Id="{i}">'
            f"<Send>"
            f'<LomId Value="0" />'
            f'<Manual Value="{manual:.10g}" />'
            f"<MidiControllerRange>"
            f'<Min Value="{floor}" />'
            f'<Max Value="1" />'
            f"</MidiControllerRange>"
            f'<AutomationTarget Id="{target_id}"><LockEnvelope Value="0" /></AutomationTarget>'
            f'<ModulationTarget Id="{mod_id}"><LockEnvelope Value="0" /></ModulationTarget>'
            f"</Send>"
            f'<EnabledByUser Value="{"true" if enabled else "false"}" />'
            f"</TrackSendHolder>"
        )
    return _SENDS_EMPTY_RE.sub(lambda m: f"<Sends>{''.join(holders)}</Sends>", track_block, count=1)


def build_return_track_block(
    name: str, id_offset: int, device_xml: str | list[str] | None = None, num_return_tracks: int = 1
) -> str:
    """Build one `<ReturnTrack>` block from templates/return_track.xml
    (see AUTOMATION.md "Sends" / PLAN.md for how that template was
    extracted -- a real Ableton-saved Return Track from the Patience demo
    with its own device/automation state stripped back to empty). Same
    renumbering scheme as a regular track (id_offset is this track's own
    _TRACK_ID_BUCKET_WIDTH-wide bucket), same `_embed_device` mechanism
    for the bus's own REAPER FX chain (matched the same way as a regular
    track's FX -- see cli.py).

    `num_return_tracks` -- total Return Track count in the project.
    Confirmed against the real Patience demo: a genuine `<ReturnTrack>`
    carries its OWN `<Sends>` too, one `TrackSendHolder` per Return Track
    in the project (including itself, disabled) -- same rectangular
    send-matrix shape every regular track's `<Sends>` already has via
    `_add_track_sends`. Leaving a Return Track's `<Sends>` empty while
    regular tracks carry `num_return_tracks` holders each is a row-length
    mismatch in that matrix -- prime suspect for a real-Ableton
    EXCEPTION_ACCESS_VIOLATION on load (see PLAN.md "Sends")."""
    block = _renumber_ids(_Templates.return_track_template(), id_offset)
    block, n = _RETURN_TRACK_ROOT_ID_RE.subn(rf'<ReturnTrack Id="{id_offset}"', block, count=1)
    if n != 1:
        raise ValueError("could not find Return Track root element's Id=\"0\" to renumber (template format may have changed)")
    escaped_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    if block.count("__RETURN_TRACK_NAME__") != 1:
        raise ValueError("could not find Return Track's own name placeholder (template format may have changed)")
    block = block.replace("__RETURN_TRACK_NAME__", escaped_name)
    block = _add_track_sends(block, [None] * num_return_tracks, id_offset)
    if device_xml is not None:
        block = _embed_device(block, device_xml, id_offset + _DEVICE_ID_SUB_OFFSET)
    return block


def build_track_block(
    spec: TrackSpec, id_offset: int, tempo: float = 120.0, time_signature: tuple[int, int] = (4, 4)
) -> str:
    template = _Templates.midi_track_block() if spec.track_type == "MIDI" else _Templates.audio_track_block()
    block = _renumber_ids(template, id_offset)
    block, n = _TRACK_ROOT_ID_RE.subn(rf'<\g<1> Id="{id_offset}"', block, count=1)
    if n != 1:
        raise ValueError("could not find track root element's Id=\"0\" to renumber (template format may have changed)")
    escaped_name = spec.name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    # UserName is Ableton's "this name was actually chosen by the user"
    # flag -- left blank (the template's default), Ableton treats the
    # track as never-renamed and displays/regenerates its own auto name
    # ("1-MIDI", "2-Audio", ...) instead of EffectiveName. Matched together
    # with EffectiveName (not as a separate regex) since a bare
    # `<UserName Value="" />` pattern also appears elsewhere in the
    # template (per-scene/per-preset naming, unrelated to the track's own
    # name) -- anchoring on the adjacent EffectiveName pair is what
    # actually identifies the track's own Name block.
    block, n = _TRACK_NAME_BLOCK_RE.subn(
        f'<EffectiveName Value="{escaped_name}" />\n\t\t\t\t\t<UserName Value="{escaped_name}" />', block, count=1
    )
    if n != 1:
        raise ValueError("could not find track's own EffectiveName/UserName pair (template format may have changed)")

    # Ableton's own Volume fader range (confirmed from the template's own
    # MidiControllerRange bounds) -- REAPER allows louder boosts than
    # Ableton's mixer max (~+6dB here vs REAPER's much higher ceiling),
    # clamp rather than write an out-of-range value Live might reject or
    # silently reinterpret.
    clamped_volume = max(0.0003162277571, min(1.99526238, spec.volume))
    clamped_pan = max(-1.0, min(1.0, spec.pan))
    block, n = _TRACK_VOLUME_MANUAL_RE.subn(rf"\g<1>{clamped_volume:.10g}\g<2>", block, count=1)
    if n != 1:
        raise ValueError("could not find track's own Volume Manual value (template format may have changed)")
    block, n = _TRACK_PAN_MANUAL_RE.subn(rf"\g<1>{clamped_pan:g}\g<2>", block, count=1)
    if n != 1:
        raise ValueError("could not find track's own Pan Manual value (template format may have changed)")

    envelopes_xml = ""
    if spec.volume_automation:
        target_m = _TRACK_VOLUME_TARGET_RE.search(block)
        if not target_m:
            raise ValueError("could not find track's own Volume AutomationTarget Id (template format may have changed)")
        points = [
            (sec * tempo / 60.0, max(0.0003162277571, min(1.99526238, val))) for sec, val in spec.volume_automation
        ]
        if len(points) >= _ENVELOPE_ID_STRIDE:
            raise ValueError(
                f"{len(points)} volume automation points for track {spec.name!r} exceed the per-envelope id budget"
            )
        envelopes_xml += _build_track_automation_envelope_xml(
            id_offset + _ENVELOPE_ID_SUB_OFFSET, target_m.group(1), id_offset + _ENVELOPE_ID_SUB_OFFSET + 1, points
        )
    if spec.pan_automation:
        target_m = _TRACK_PAN_TARGET_RE.search(block)
        if not target_m:
            raise ValueError("could not find track's own Pan AutomationTarget Id (template format may have changed)")
        points = [(sec * tempo / 60.0, max(-1.0, min(1.0, val))) for sec, val in spec.pan_automation]
        if len(points) >= _ENVELOPE_ID_STRIDE:
            raise ValueError(
                f"{len(points)} pan automation points for track {spec.name!r} exceed the per-envelope id budget"
            )
        pan_envelope_id = id_offset + _ENVELOPE_ID_SUB_OFFSET + _ENVELOPE_ID_STRIDE
        envelopes_xml += _build_track_automation_envelope_xml(
            pan_envelope_id, target_m.group(1), pan_envelope_id + 1, points
        )
    if envelopes_xml:
        block, n = _TRACK_AUTOMATION_ENVELOPES_EMPTY_RE.subn(
            f"<AutomationEnvelopes><Envelopes>{envelopes_xml}</Envelopes></AutomationEnvelopes>", block, count=1
        )
        if n != 1:
            raise ValueError(
                "could not find track's own empty <AutomationEnvelopes><Envelopes /> list "
                "(template format may have changed)"
            )

    if spec.device_xml is not None:
        block = _embed_device(block, spec.device_xml, id_offset + _DEVICE_ID_SUB_OFFSET)
    if spec.sends is not None:
        block = _add_track_sends(block, spec.sends, id_offset)
    # Ids in the MidiControllers block are just Id="N" like everything
    # else in the template, unaffected by device/send embedding above --
    # safe to read back any time after _renumber_ids, kept here since it's
    # only needed once clips are actually built below.
    controller_target_ids = _controller_target_ids(block) if spec.track_type == "MIDI" else {}
    clips = ([(spec.midi_notes, spec.midi_clip_length_beats)] if spec.midi_notes is not None else []) + list(
        spec.extra_midi_clips or []
    )
    if len(clips) > MAX_MIDI_CLIPS_PER_TRACK:
        raise ValueError(
            f"{len(clips)} MIDI clips requested for track {spec.name!r}, only {MAX_MIDI_CLIPS_PER_TRACK} scene slots available"
        )
    if clips:
        clip_xmls = [
            build_midi_clip_xml(
                notes,
                name=spec.name,
                length_beats=length_beats,
                note_id_offset=id_offset + _CLIP_ID_SUB_OFFSET + i * _CLIP_ID_STRIDE,
                clip_id=i,
                time_signature=time_signature,
            )
            for i, (notes, length_beats) in enumerate(clips)
        ]
        block = _embed_clips(block, clip_xmls)

    if spec.arrangement_midi_clips:
        max_offset = id_offset + _ARRANGEMENT_CLIP_ID_SUB_OFFSET + len(spec.arrangement_midi_clips) * _CLIP_ID_STRIDE
        if max_offset >= id_offset + _SESSION_MIRROR_CLIP_ID_SUB_OFFSET:
            raise ValueError(
                f"{len(spec.arrangement_midi_clips)} arrangement MIDI clips requested for track {spec.name!r} "
                "would overflow this track's Id bucket -- too many clips/notes for the current offset scheme"
            )
        arrangement_xmls = [
            build_midi_clip_xml(
                entry[0],
                name=spec.name,
                length_beats=entry[2],
                start_beats=entry[1],
                note_id_offset=id_offset + _ARRANGEMENT_CLIP_ID_SUB_OFFSET + i * _CLIP_ID_STRIDE,
                clip_id=i,
                loop_length_beats=entry[3] if len(entry) > 3 else None,
                cc_events=entry[4] if len(entry) > 4 else None,
                controller_target_ids=controller_target_ids,
                time_signature=time_signature,
            )
            for i, entry in enumerate(spec.arrangement_midi_clips)
        ]
        block = _embed_arrangement_clips(block, arrangement_xmls)

    if spec.arrangement_audio_clips:
        audio_xmls = [
            build_audio_clip_xml(
                wav_path=item.wav_path,
                name=item.name,
                length_beats=item.length_beats,
                duration_frames=item.duration_frames,
                sample_rate=item.sample_rate,
                tempo=tempo,
                start_beats=item.start_beats,
                clip_id=i,
                loop_offset_seconds=item.loop_offset_seconds,
                loop_length_seconds=item.loop_length_seconds,
                playrate=item.playrate,
                time_signature=time_signature,
                volume=item.volume,
                pitch_semitones=item.pitch_semitones,
                relative_path=item.relative_path,
                original_file_size=item.original_file_size,
                original_crc=item.original_crc,
            )
            for i, item in enumerate(spec.arrangement_audio_clips)
        ]
        block = _embed_arrangement_audio_clips(block, audio_xmls)

    if spec.session_slot_count:
        session_clip_xmls: list[str] = []
        if spec.arrangement_midi_clips:
            base = id_offset + _SESSION_MIRROR_CLIP_ID_SUB_OFFSET
            max_offset = base + len(spec.arrangement_midi_clips) * _CLIP_ID_STRIDE
            if max_offset >= id_offset + _DEVICE_ID_SUB_OFFSET:
                raise ValueError(
                    f"{len(spec.arrangement_midi_clips)} Session-mirror MIDI clips requested for track "
                    f"{spec.name!r} would overflow this track's Id bucket -- too many clips/notes for the "
                    "current offset scheme"
                )
            session_clip_xmls = [
                build_midi_clip_xml(
                    entry[0],
                    name=spec.name,
                    length_beats=entry[2],
                    note_id_offset=base + i * _CLIP_ID_STRIDE,
                    clip_id=i,
                    loop_length_beats=entry[3] if len(entry) > 3 else None,
                    cc_events=entry[4] if len(entry) > 4 else None,
                    controller_target_ids=controller_target_ids,
                    time_signature=time_signature,
                )
                for i, entry in enumerate(spec.arrangement_midi_clips)
            ]
        elif spec.arrangement_audio_clips:
            session_clip_xmls = [
                build_audio_clip_xml(
                    wav_path=item.wav_path,
                    name=item.name,
                    length_beats=item.length_beats,
                    duration_frames=item.duration_frames,
                    sample_rate=item.sample_rate,
                    tempo=tempo,
                    start_beats=0.0,
                    clip_id=i,
                    loop_offset_seconds=item.loop_offset_seconds,
                    loop_length_seconds=item.loop_length_seconds,
                    playrate=item.playrate,
                    time_signature=time_signature,
                    volume=item.volume,
                    pitch_semitones=item.pitch_semitones,
                    relative_path=item.relative_path,
                    original_file_size=item.original_file_size,
                    original_crc=item.original_crc,
                )
                for i, item in enumerate(spec.arrangement_audio_clips)
            ]
        if spec.scene_slot_indices is not None:
            if len(spec.scene_slot_indices) != len(session_clip_xmls):
                raise ValueError(
                    f"track {spec.name!r}: scene_slot_indices has {len(spec.scene_slot_indices)} entries "
                    f"but {len(session_clip_xmls)} Session clip(s) were built"
                )
            sparse: list[str | None] = [None] * spec.session_slot_count
            for scene_idx, clip_xml in zip(spec.scene_slot_indices, session_clip_xmls):
                sparse[scene_idx] = clip_xml
            session_clip_xmls = sparse
        block = _set_session_clip_slots(block, session_clip_xmls, spec.session_slot_count)
    return block


_TEMPO_RE = re.compile(r"(<Tempo>\s*<LomId Value=\"0\" />\s*<Manual Value=\")[\d.]+(\" />)")
_TIME_SIG_RE = re.compile(r'(<TimeSignature>\s*<LomId Value="0" />\s*<Manual Value=")\d+(" />)')
_NEXT_POINTEE_ID_RE = re.compile(r'(<NextPointeeId Value=")\d+(" />)')

# Real Ableton files (confirmed against a project genuinely saved by the
# user's own Ableton install, not just our factory-shipped templates)
# carry a SECOND, shadow copy of Tempo/TimeSignature: an
# <AutomationEnvelope> whose <EnvelopeTarget><PointeeId> matches the
# field's own <AutomationTarget Id>, holding a single breakpoint at
# Time="-63072000" (Ableton's "value before time began" convention for an
# unautomated-but-envelope-present parameter). Ableton's transport reads
# THIS breakpoint, not the <Manual> value, whenever such an envelope
# exists -- <Manual> is only the fallback when no envelope is present.
# Every one of our .als templates already carries this envelope (it's a
# stock, unautomated single-breakpoint envelope, not something added by
# real automation), so patching only <Manual> silently has no visible
# effect: the file loads fine, values look right in a text/structural
# check, but the transport keeps showing the template's original 120bpm/
# 4:4 -- found by direct comparison against a project genuinely saved by
# the user's own Ableton (not just a factory template), after patching
# <Manual> alone repeatedly failed to change the displayed value even in
# that genuine file. See PLAN.md "Tempo/time signature shadow envelope".
_TEMPO_TARGET_ID_RE = re.compile(r"<Tempo>\s*<LomId Value=\"0\" />\s*<Manual Value=\"[\d.]+\" />.*?<AutomationTarget Id=\"(\d+)\"", re.DOTALL)
_TIME_SIG_TARGET_ID_RE = re.compile(r'<TimeSignature>\s*<LomId Value="0" />\s*<Manual Value="\d+" />.*?<AutomationTarget Id="(\d+)"', re.DOTALL)


def _patch_shadow_envelope(xml: str, target_id: str, event_tag: str, new_value: str) -> str:
    pattern = re.compile(
        rf'(<AutomationEnvelope Id="\d+">\s*<EnvelopeTarget>\s*<PointeeId Value="{target_id}" />\s*'
        rf'</EnvelopeTarget>\s*<Automation>\s*<Events>\s*<{event_tag} Id="0" Time="-63072000" Value=")[^"]+("\s*/>)'
    )
    result, n = pattern.subn(rf"\g<1>{new_value}\g<2>", xml, count=1)
    if n == 0:
        raise ValueError(f"could not find shadow AutomationEnvelope for PointeeId {target_id} ({event_tag})")
    return result


def _build_track_automation_envelope_xml(
    envelope_id: int, target_id: str, point_id_offset: int, points_beats: list[tuple[float, float]]
) -> str:
    """A real (non-shadow) `<AutomationEnvelope>` for a track's own
    Volume/Pan, same element shape as the LiveSet-root Tempo/TimeSignature
    shadow envelope (`_patch_shadow_envelope`'s own docstring) but with
    real breakpoints instead of a single -63072000 placeholder, and
    `FloatEvent`s -- each a member of the envelope's own `<Events>`
    sibling list, so each needs its own distinct nonzero Id per this
    repo's one hard rule (see CLAUDE.md)."""
    events = "".join(
        f'<FloatEvent Id="{point_id_offset + i}" Time="{beat:g}" Value="{value:g}" />'
        for i, (beat, value) in enumerate(points_beats)
    )
    return (
        f'<AutomationEnvelope Id="{envelope_id}">'
        f"<EnvelopeTarget><PointeeId Value=\"{target_id}\" /></EnvelopeTarget>"
        f"<Automation><Events>{events}</Events>"
        "<AutomationTransformViewState><IsTransformPending Value=\"false\" />"
        "<TimeAndValueTransforms /></AutomationTransformViewState>"
        "</Automation></AutomationEnvelope>"
    )

# Ableton packs time signature numerator+denominator into ONE integer
# (LiveSet-root <TimeSignature><Manual Value="N" />, next to <Tempo> --
# distinct from a MidiClip's own separate-fields RemoteableTimeSignature,
# see templates/midi_clip.xml). Confirmed via a real, working open-source
# encoder/decoder pair (DawVert's plugins/output/ableton.py +
# plugins/input/r_ableton.py) that's self-consistent with the real
# template's own numeric bounds (4/4 -> Manual Value="201";
# MidiControllerRange Max="494" == 99/16, the packing scheme's own upper
# bound) -- see PLAN.md "Time signature".
_TIME_SIG_DENOMINATORS = [1, 2, 4, 8, 16]


def _encode_time_signature(numerator: int, denominator: int) -> int:
    if denominator not in _TIME_SIG_DENOMINATORS:
        raise ValueError(f"unsupported time signature denominator {denominator} (Ableton allows 1/2/4/8/16)")
    if not (1 <= numerator <= 99):
        raise ValueError(f"unsupported time signature numerator {numerator} (Ableton allows 1-99)")
    return (numerator - 1) + 99 * _TIME_SIG_DENOMINATORS.index(denominator)


_LOCATORS_EMPTY_RE = re.compile(r"<Locators>\s*<Locators\s*/>\s*</Locators>")

# Top-level `<SendsPre />` (sibling of `<PreHearTrack>`/`<Scenes>`, PreHear/
# cue-monitoring bus's own send matrix row) -- confirmed against the real
# Patience demo (2 Return Tracks there) that this is a genuine sibling
# list needing one `<SendPreBool Id="i" Value="false" />` per Return
# Track, id = local list-position index like TrackSendHolder's own Id
# (not a global Pointee id). Left empty regardless of Return Track count
# was the real cause of a real-Ableton EXCEPTION_ACCESS_VIOLATION on load
# with even a single bare Return Track and zero device/automation content
# otherwise -- see PLAN.md "Sends".
_SENDS_PRE_EMPTY_RE = re.compile(r"<SendsPre\s*/>")


def build_locators_xml(locators: list[tuple[float, str]]) -> str:
    """Build the `<Locators><Locators>...</Locators></Locators>` LiveSet-
    root block from `(time_beats, name)` pairs -- see PLAN.md "Markers/
    locators" for the real-Ableton-reference schema this is based on
    (Chuck Sutton "Patience" factory demo, the only reference file
    checked so far with actual locators set instead of the empty
    self-closing `<Locators />` every one of our own templates/other
    reference files has).

    `Id` here is plain 0-based sequential numbering, NOT the
    null-pointee-sentinel-vs-globally-unique convention used for
    Devices/DrumBranch/clip Events elsewhere in this codebase -- Ableton
    itself uses `Id="0"` for a real, ordinary first locator in the
    reference file, confirmed not a special case here. Locators live at
    the LiveSet root (not per-track), so they don't go through the
    per-track `_renumber_ids`/id_offset pipeline at all -- built and
    inserted directly."""
    parts = []
    for i, (time_beats, name) in enumerate(locators):
        parts.append(
            f'<Locator Id="{i}"><LomId Value="0" /><Time Value="{time_beats:g}" />'
            f'<Name Value="{_xml_escape(name)}" /><Annotation Value="" /><IsSongStart Value="false" /></Locator>'
        )
    return f"<Locators><Locators>{''.join(parts)}</Locators></Locators>"


# Matches the LiveSet-root `<Scenes>...</Scenes>` list in full, for
# wholesale replacement by _set_scenes_count -- see TrackSpec.
# session_slot_count / _set_session_clip_slots: every track's own
# ClipSlotList (both MainSequencer's and FreezeSequencer's) must have
# exactly as many slots as this list has Scene entries, confirmed by a
# real Ableton reload failure when only the ClipSlotLists were resized
# and this was left at its template default (1 entry).
_SCENES_LIST_RE = re.compile(r"<Scenes>.*?</Scenes>", re.DOTALL)
_SCENE_STOCK_RE = re.compile(r'<Scene Id="0">.*?</Scene>', re.DOTALL)
_SCENE_ID_ATTR_RE = re.compile(r'^<Scene Id="\d+">')
_SCENE_NAME_RE = re.compile(r'<Name Value="[^"]*" />')


def _set_scenes_count(xml: str, total_scenes: int, names: list[str] | None = None) -> str:
    """Regenerate the LiveSet-root `<Scenes>` list with exactly
    `total_scenes` entries, cloned from the template's own stock
    `<Scene Id="0">` (every field besides `Id` -- FollowAction/Name/
    Tempo/TimeSignatureId/etc -- is the template's untouched default,
    fine to duplicate across every clone since nothing here is
    per-track). Each clone's own `Id` just needs to be unique among
    Scenes (a real sibling list) -- 0..total_scenes-1.

    `names` -- optional, one name per scene (see cli.py --scenes, where
    each REAPER marker becomes one Scene with the marker's own name --
    matches how a real Ableton project keeps its Locators and Scenes in
    1:1 name/order sync, confirmed against the Ableton factory demo
    song). Must be `None` or exactly `total_scenes` long. `None` leaves
    every clone's Name at the template's stock default."""
    if names is not None and len(names) != total_scenes:
        raise ValueError(f"{len(names)} scene name(s) given but total_scenes is {total_scenes}")
    scenes_m = _SCENES_LIST_RE.search(xml)
    if scenes_m is None:
        raise ValueError("could not find <Scenes> list (Ableton template format may have changed)")
    stock_m = _SCENE_STOCK_RE.search(scenes_m.group(0))
    if stock_m is None:
        raise ValueError('could not find stock <Scene Id="0"> to clone (Ableton template format may have changed)')
    stock_scene = stock_m.group(0)
    scenes = []
    for i in range(total_scenes):
        scene_xml, n = _SCENE_ID_ATTR_RE.subn(f'<Scene Id="{i}">', stock_scene, count=1)
        if n != 1:
            raise ValueError("could not renumber cloned Scene's own Id (Ableton template format may have changed)")
        if names is not None:
            scene_xml, n = _SCENE_NAME_RE.subn(
                f'<Name Value="{_xml_escape(names[i])}" />', scene_xml, count=1
            )
            if n != 1:
                raise ValueError("could not set cloned Scene's own Name (Ableton template format may have changed)")
        scenes.append(scene_xml)
    replacement = "<Scenes>" + "".join(scenes) + "</Scenes>"
    new_xml, n = _SCENES_LIST_RE.subn(lambda _m: replacement, xml, count=1)
    if n != 1:
        raise ValueError("could not find <Scenes> list to replace (Ableton template format may have changed)")
    return new_xml


def build_liveset_xml(
    tracks: list[TrackSpec],
    tempo: float,
    time_signature: tuple[int, int] = (4, 4),
    locators: list[tuple[float, str]] | None = None,
    return_tracks: list[tuple[str, str | list[str] | None]] | None = None,
    scene_names: list[str] | None = None,
) -> str:
    """id_offset is assigned per track automatically (large enough to
    never collide within one track's ~4400 internal Ids, or with an
    embedded device's own Ids -- see _DEVICE_ID_SUB_OFFSET).

    `locators` -- optional `(time_beats, name)` pairs (see
    `rpp_parse.project_markers` for the REAPER-side source, in seconds --
    caller converts to beats the same way as everywhere else, `seconds *
    tempo / 60`), embedded as Arrangement-view Locators. `None`/empty
    leaves the template's own empty `<Locators />` as-is.

    `scene_names` -- optional, one name per Scene, in Scene-index order
    (see cli.py --scenes). Must line up with whatever caused
    `max_session_slots` below to be nonzero -- i.e. every TrackSpec's
    `session_slot_count` must equal `len(scene_names)` when this is
    given. `None` leaves cloned Scenes at their template default name.

    `return_tracks` -- optional `(name, device_xml)` pairs, one per
    REAPER aux-send bus (see rpp_parse.track_is_return_bus, AUTOMATION.md
    "Sends"), appended to `<Tracks>` AFTER every regular track -- matches
    real Ableton file ordering (Return Tracks always come after regular
    tracks). Position in this list is what any TrackSpec.sends entries
    are positional against."""
    blocks = [
        build_track_block(spec, id_offset=_TRACK_ID_BUCKET_WIDTH * (i + 1), tempo=tempo, time_signature=time_signature)
        for i, spec in enumerate(tracks)
    ]
    return_blocks = [
        build_return_track_block(
            name,
            id_offset=_TRACK_ID_BUCKET_WIDTH * (len(tracks) + j + 1),
            device_xml=device_xml,
            num_return_tracks=len(return_tracks or []),
        )
        for j, (name, device_xml) in enumerate(return_tracks or [])
    ]
    xml = _Templates.header() + "".join(blocks) + "".join(return_blocks) + _Templates.tail()

    if return_tracks:
        sends_pre = "".join(f'<SendPreBool Id="{i}" Value="false" />' for i in range(len(return_tracks)))
        xml, n = _SENDS_PRE_EMPTY_RE.subn(f"<SendsPre>{sends_pre}</SendsPre>", xml, count=1)
        if n != 1:
            raise ValueError("could not find empty <SendsPre /> to populate (Ableton template format may have changed)")

    max_session_slots = max((spec.session_slot_count for spec in tracks), default=0)
    if max_session_slots:
        xml = _set_scenes_count(xml, max_session_slots, names=scene_names)

    if locators:
        xml, n = _LOCATORS_EMPTY_RE.subn(build_locators_xml(locators), xml, count=1)
        if n != 1:
            raise ValueError("could not find empty <Locators /> to populate (Ableton template format may have changed)")

    tempo_target_m = _TEMPO_TARGET_ID_RE.search(xml)
    if tempo_target_m is None:
        raise ValueError("could not find Tempo's own AutomationTarget Id (Ableton template format may have changed)")
    xml, n = _TEMPO_RE.subn(rf"\g<1>{tempo:g}\g<2>", xml, count=1)
    if n == 0:
        raise ValueError("could not find Tempo block to substitute (Ableton template format may have changed)")
    xml = _patch_shadow_envelope(xml, tempo_target_m.group(1), "FloatEvent", f"{tempo:g}")

    numerator, denominator = time_signature
    encoded_time_sig = _encode_time_signature(numerator, denominator)
    time_sig_target_m = _TIME_SIG_TARGET_ID_RE.search(xml)
    if time_sig_target_m is None:
        raise ValueError("could not find TimeSignature's own AutomationTarget Id (Ableton template format may have changed)")
    xml, n = _TIME_SIG_RE.subn(rf"\g<1>{encoded_time_sig}\g<2>", xml, count=1)
    if n == 0:
        raise ValueError("could not find TimeSignature block to substitute (Ableton template format may have changed)")
    xml = _patch_shadow_envelope(xml, time_sig_target_m.group(1), "EnumEvent", str(encoded_time_sig))

    # Live rejects the file ("NextPointeeId is too low") unless this
    # counter exceeds every Id actually used in the document -- our
    # per-track renumbering easily exceeds the template's original small
    # value. Found the hard way: a real 7-track export failed to load
    # with exactly this error (template's default 4293 vs our highest
    # renumbered Id ~750000+).
    max_id = max((int(m.group(1)) for m in _ID_ATTR_RE.finditer(xml)), default=0)
    xml, n = _NEXT_POINTEE_ID_RE.subn(rf"\g<1>{max_id + 1000}\g<2>", xml, count=1)
    if n == 0:
        raise ValueError("could not find NextPointeeId to bump (Ableton template format may have changed)")
    return xml


def write_als(xml_text: str, als_path: str) -> None:
    with gzip.open(als_path, "wb") as f:
        f.write(xml_text.encode("utf-8"))
