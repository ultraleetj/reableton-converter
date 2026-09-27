"""Parse a REAPER .rpp project file to find tracks and RS5k FX instances.

RPP's top-level structure (tracks, chunk nesting) is plain text. Each RS5k
instance's parameters are a binary blob within one base64 field of the
`<VST ...>` chunk — layout reverse-engineered against a live REAPER API
param dump (see PLAN.md, `tools/dump_rs5k_params.lua`) and confirmed
across 10 real instances in `chirriado.RPP`:

- The blob's 2nd base64 field ("body") is: absolute WAV path (ASCII,
  null-terminated) + 1 padding byte, then a contiguous array of
  little-endian float64 params starting immediately after, one 8-byte
  slot per RS5k param index (0..15 confirmed reliable; 16+ diverges from
  the live-API index table and isn't needed for v1 scope).
- ``params_base = body.find(b".wav") + 4 + 1``; param *i* lives at
  ``params_base + i * 8``.
- Param 0 (volume) on disk is **2x** the value REAPER's
  `TrackFX_GetParamNormalized` API reports — the raw on-disk value is the
  *actual* linear gain multiplier (0..~2, unity=1.0), which is what
  explains the "+4.1dB shown for a 0.8 normalized value" mapping bug
  documented in `reference/ADG_EXPORT_PLAN.md`'s deferred volume
  follow-up. Use the raw on-disk value directly — it's correct, not the
  live-API-normalized one.
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field

from .model import MidiCCEvent, MidiNote

_VST_RS5K_RE = re.compile(
    r'<VST "VSTi: ReaSamplOmatic5000 \(Cockos\)".*?\n(.*?)\n\s*>',
    re.DOTALL,
)
# Same quoting rules as MARKER names (see _MARKER_RE): bare, "...", or
# '...'/`...` when the name itself contains double quotes.
_NAME_RE = re.compile(r"""^\s*NAME\s+(?:"([^"\n]*)"|'([^'\n]*)'|`([^`\n]*)`|([^\n]*?))\s*$""", re.MULTILINE)


def _name_value(m: re.Match) -> str:
    return next(g for g in m.groups() if g is not None)
# "TEMPO <bpm> <numerator> <denominator> <flag>", project-level (top of file,
# not inside a TRACK chunk).
_TEMPO_RE = re.compile(r"^\s*TEMPO\s+([\d.]+)\s+(\d+)\s+(\d+)", re.MULTILINE)
# A loaded instrument FX (VSTi/VST3i) makes a track need MIDI input --
# REAPER doesn't have a separate "track type" the way Ableton does.
_INSTRUMENT_FX_RE = re.compile(r'<VST\d?\s+"VST\d?i:')
_HASDATA_RE = re.compile(r"^\s*HASDATA\s", re.MULTILINE)  # marks a MIDI item's data block


def project_tempo(rpp_text: str) -> float | None:
    m = _TEMPO_RE.search(rpp_text)
    return float(m.group(1)) if m else None


def project_time_signature(rpp_text: str) -> tuple[int, int] | None:
    """(numerator, denominator) from the same project-level TEMPO line
    project_tempo() reads -- REAPER has no separate time signature line,
    it's packed into TEMPO's 2nd/3rd fields. Single project-level meter
    only, no per-track override exists in .rpp."""
    m = _TEMPO_RE.search(rpp_text)
    return (int(m.group(2)), int(m.group(3))) if m else None


# REAPER only quotes a marker name when it needs to (spaces/empty):
# `MARKER 1 0 intro ...` is bare. Names containing `"` get `'` or `` ` ``
# quotes instead, same as every other RPP string field.
_MARKER_RE = re.compile(
    r'^\s*MARKER\s+\d+\s+([\d.]+)\s+(?:"([^"]*)"|\'([^\']*)\'|`([^`]*)`|(\S+))',
    re.MULTILINE,
)


def project_markers(rpp_text: str) -> list[tuple[float, str]]:
    """Every project-level `MARKER` line as `(position_seconds, name)`
    pairs, sorted by position -- see PLAN.md "Markers/locators" for the
    line format. `MARKER <idx> <pos_seconds> "<name>" <isrgn> <color>
    <flags> ...` -- only the position (always plain seconds, same
    convention as everywhere else project-root-level in this format)
    and name (quoted or bare, see `_MARKER_RE`) are parsed; later fields (region flag, color, a
    literal `R`/GUID/trailing ints seen in a real project) aren't
    understood and aren't needed for a plain point-marker -> Ableton
    Locator conversion. REGIONS (a marker pair with the region flag set)
    aren't distinguished from point markers -- every MARKER line becomes
    one Locator regardless; not attempted to detect/skip region-end
    markers specifically, no real project checked against yet had one."""
    markers = [
        (float(m.group(1)), next(g for g in m.groups()[1:] if g is not None))
        for m in _MARKER_RE.finditer(rpp_text)
    ]
    return sorted(markers, key=lambda m: m[0])


def track_needs_midi(track_chunk: str) -> bool:
    """True if the track has a loaded instrument FX or any MIDI item --
    either means it needs an Ableton MidiTrack, not an AudioTrack."""
    return bool(_INSTRUMENT_FX_RE.search(track_chunk)) or bool(_HASDATA_RE.search(track_chunk))


def track_has_audio_item(track_chunk: str) -> bool:
    return bool(re.search(r"<SOURCE (WAVE|MP3|FLAC|OGG|AIFF)\b", track_chunk))


_TRACK_VOLPAN_RE = re.compile(r"^\s*VOLPAN\s+([\d.eE+-]+)\s+([\d.eE+-]+)", re.MULTILINE)


def track_volume(track_chunk: str) -> float:
    """Track's own linear gain multiplier (mixer fader) -- same `VOLPAN
    <vol> <pan> <?> <?>` tag as an item's, but the TRACK's own line
    always appears before any nested `<ITEM>` block in the chunk (a
    track's own properties come first, items are children), so a plain
    first-match `.search()` over the whole track chunk reliably finds the
    track's own line, not an item's -- confirmed against every real track
    in chirriado.RPP (track VOLPAN offset ~100-200 chars in, first real
    `<ITEM` typically thousands of chars in). Unity = 1.0 if absent."""
    m = _TRACK_VOLPAN_RE.search(track_chunk)
    return float(m.group(1)) if m else 1.0


def track_pan(track_chunk: str) -> float:
    """Track's own pan, -1 (full left) .. 1 (full right) -- same VOLPAN
    line/caveats as track_volume(). Matches Ableton's per-track `<Pan>
    <Manual Value="N" />` range exactly (-1..1), no remapping needed --
    unlike item-level pan, which has no per-clip Ableton equivalent."""
    m = _TRACK_VOLPAN_RE.search(track_chunk)
    return float(m.group(2)) if m else 0.0


_ENVELOPE_ACT_RE = re.compile(r"^\s*ACT\s+(\d+)", re.MULTILINE)
_ENVELOPE_PT_RE = re.compile(r"^\s*PT\s+([\d.eE+-]+)\s+([\d.eE+-]+)", re.MULTILINE)


def _track_envelope_points(track_chunk: str, tag: str) -> list[tuple[float, float]] | None:
    """[(time_seconds, value), ...] breakpoints from a track's own
    envelope sub-chunk (`VOLENV2`/`PANENV2`), or None if the chunk is
    absent, bypassed (`ACT 0` -- REAPER keeps the chunk around even when
    the envelope is switched off), or has no points. Point times are
    plain project seconds, same convention as everywhere else at this
    level (project_markers, item_position_seconds) -- not tempo/beat
    relative, REAPER doesn't store envelope points that way."""
    m = re.search(r"<" + tag + r"\b.*?\n(.*?)\n\s*>", track_chunk, re.DOTALL)
    if not m:
        return None
    body = m.group(1)
    act_m = _ENVELOPE_ACT_RE.search(body)
    if act_m and act_m.group(1) == "0":
        return None
    points = [(float(pm.group(1)), float(pm.group(2))) for pm in _ENVELOPE_PT_RE.finditer(body)]
    # A single point is REAPER's default idle state for an envelope that
    # was never actually drawn on (confirmed against automatest.RPP: e.g.
    # "send vol"/"dummy send" carry `ACT 1` with one PT despite no
    # intentional automation) -- constant value, indistinguishable from
    # just using the track's static VOLPAN/pan field, so don't surface it
    # as automation.
    return points if len(points) >= 2 else None


def track_volume_envelope(track_chunk: str) -> list[tuple[float, float]] | None:
    """[(time_seconds, linear_gain), ...] breakpoints of the track's own
    volume envelope -- same linear-gain convention as track_volume().
    REAPER carries two separate volume-envelope tags per track: `VOLENV2`
    (post-FX/main -- the normal "Volume" lane) and `VOLENV` (Pre-FX --
    a separate lane before the FX chain). Ableton has only one per-track
    Volume automation target, no pre/post-FX split, so both collapse
    onto it -- per user decision (no true mapping exists, use whichever
    one is actually active). `VOLENV2` checked first since it's the
    common case; `VOLENV` used only if `VOLENV2` is absent/bypassed.
    None if neither is active/has points (see _track_envelope_points)."""
    return _track_envelope_points(track_chunk, "VOLENV2") or _track_envelope_points(track_chunk, "VOLENV")


def track_pan_envelope(track_chunk: str) -> list[tuple[float, float]] | None:
    """[(time_seconds, pan), ...] breakpoints of the track's own pan
    envelope -- same -1..1 convention as track_pan(). Same Pre-FX
    (`PANENV`) vs post-FX/main (`PANENV2`) split and same collapse-onto-
    the-one-Ableton-target reasoning as track_volume_envelope(). None if
    neither is active/has points (see _track_envelope_points)."""
    return _track_envelope_points(track_chunk, "PANENV2") or _track_envelope_points(track_chunk, "PANENV")


_AUXRECV_RE = re.compile(r"^\s*AUXRECV\s+(\d+)\s+(\d+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)", re.MULTILINE)


def track_aux_recv_entries(track_chunk: str) -> list[tuple[int, float, float]]:
    """[(source_track_index, linear_gain, pan), ...] for every `AUXRECV`
    line in this track's own chunk. `AUXRECV` lives in the RECEIVING
    track's chunk (not the sender's) -- `source_track_index` is the
    0-based index of the OTHER track sending into this one. A non-empty
    result means this track is a REAPER aux-send bus/return (see
    AUTOMATION.md "Sends") -- the equivalent of an Ableton Return Track,
    detected structurally (no naming convention/flag needed) since
    REAPER always emits `AUXRECV` inside the receiver's own chunk,
    unambiguous either way."""
    return [(int(m.group(1)), float(m.group(3)), float(m.group(4))) for m in _AUXRECV_RE.finditer(track_chunk)]


def track_is_return_bus(track_chunk: str) -> bool:
    """True if another track sends audio into this one via `AUXRECV` --
    see track_aux_recv_entries()."""
    return bool(_AUXRECV_RE.search(track_chunk))


def find_items(track_chunk: str) -> list[str]:
    """Top-level <ITEM> blocks in a track's chunk (items aren't nested)."""
    return _split_top_level_chunks(track_chunk, "ITEM")


_ITEM_POSITION_RE = re.compile(r"^\s*POSITION\s+([\d.eE+-]+)", re.MULTILINE)
_ITEM_LENGTH_RE = re.compile(r"^\s*LENGTH\s+([\d.eE+-]+)", re.MULTILINE)
_HASDATA_HEADER_RE = re.compile(r"^\s*HASDATA\s+\d+\s+(\d+)\s+QN", re.MULTILINE)
# "<delta_ticks_decimal> <status_hex> <data1_hex> <data2_hex>", e/E = event
# selection state in the REAPER editor, doesn't affect the data.
_EVENT_LINE_RE = re.compile(r"^\s*[eE]\s+(\d+)\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})", re.MULTILINE)
# `<X <delta_ticks> ...>` -- a sysex/text-meta event (REAPER re-embeds the
# source MIDI file's own TITLE/TITLE1/copyright text at every glue-repeat
# boundary in a glued take). No note/CC payload, but the leading tick delta
# is real and must be counted in the running tick accumulation below --
# every event's tick field is a delta from the PREVIOUS event regardless of
# type, so skipping this line's delta undercounts every event after it.
# Confirmed against a real glued item (voleoModular.RPP, "kit" track, bar
# 90): 4 glue-repeat boundaries each hid a real 180-tick delta, silently
# undercounting the item's true length by 720 ticks (45525 -> should be
# 46245, i.e. 47.421875 QN vs REAPER's own reported 48.1719 QN) and, worse,
# compounding a small drift into every note position after each boundary.
_META_EVENT_TICK_RE = re.compile(r"^\s*<X\s+(\d+)", re.MULTILINE)


def _iter_midi_take_events(item_chunk: str):
    """Every event in a MIDI take's raw event stream, in true document
    order, as (tick_delta, note_match | None) -- note_match is the
    _EVENT_LINE_RE match (so callers can still read status/data bytes) for
    plain e/E lines, None for <X ...> meta lines (delta-only). Merging both
    line types in position order (not just filtering-then-concatenating) is
    required to get the running tick sum right -- see _META_EVENT_TICK_RE."""
    events = [(m.start(), int(m.group(1)), m) for m in _EVENT_LINE_RE.finditer(item_chunk)]
    events += [(m.start(), int(m.group(1)), None) for m in _META_EVENT_TICK_RE.finditer(item_chunk)]
    events.sort(key=lambda e: e[0])
    for _, delta, note_match in events:
        yield delta, note_match


def item_position_seconds(item_chunk: str) -> float | None:
    m = _ITEM_POSITION_RE.search(item_chunk)
    return float(m.group(1)) if m else None


def item_length_seconds(item_chunk: str) -> float | None:
    m = _ITEM_LENGTH_RE.search(item_chunk)
    return float(m.group(1)) if m else None


def is_midi_item(item_chunk: str) -> bool:
    return "<SOURCE MIDI" in item_chunk


_ITEM_VOLPAN_RE = re.compile(r"^\s*VOLPAN\s+([\d.eE+-]+)", re.MULTILINE)


def item_volume(item_chunk: str) -> float:
    """Item's own linear gain multiplier -- `VOLPAN <vol> <pan> <?> <?>`,
    only the first field (unity = 1.0, matches Ableton's AudioClip
    `SampleVolume` convention directly, no remapping needed). Pan (2nd
    field) has no per-clip Ableton equivalent -- Live only has per-track
    pan, not per-clip -- so it's intentionally not parsed here."""
    m = _ITEM_VOLPAN_RE.search(item_chunk)
    return float(m.group(1)) if m else 1.0


_ITEM_CHANMODE_RE = re.compile(r"^\s*CHANMODE\s+(\d+)", re.MULTILINE)


def item_chanmode(item_chunk: str) -> int:
    """Item's own `CHANMODE <n>` -- 0=normal stereo, 1=reverse stereo,
    2=mono downmix, 3=mono left, 4=mono right. No equivalent field in
    Ableton's AudioClip XML (see PLAN.md "Item CHANMODE not carried
    over") -- exposed only so callers can flag non-default items
    (e.g. append a marker to the track name) rather than silently
    exporting them as plain stereo."""
    m = _ITEM_CHANMODE_RE.search(item_chunk)
    return int(m.group(1)) if m else 0


_ITEM_PLAYRATE_RE = re.compile(r"^\s*PLAYRATE\s+([\d.eE+-]+)", re.MULTILINE)
_ITEM_PLAYRATE_PITCH_RE = re.compile(
    r"^\s*PLAYRATE\s+[\d.eE+-]+\s+[\d.eE+-]+\s+([\d.eE+-]+)", re.MULTILINE
)
_ITEM_LOOP_RE = re.compile(r"^\s*LOOP\s+(\d+)", re.MULTILINE)
_ITEM_SOFFS_RE = re.compile(r"^\s*SOFFS\s+([\d.eE+-]+)\s+([\d.eE+-]+)", re.MULTILINE)


def item_source_offset_beats(item_chunk: str) -> float:
    """`SOFFS <seconds> <beats>` -- how far into the underlying MIDI
    source the item's own playback actually starts (both fields are the
    same offset, in two units; the second, beats/QN field is already in
    the same raw tick/ppq coordinate space as MidiNote.position). Common
    when an item was recorded with pre-roll: the raw MIDI take includes
    a leading stretch of silence (the count-in) that isn't actually part
    of the performance, trimmed by SOFFS rather than removed from the
    take. Confirmed against a real chirriado.RPP item ("cucharas"): the
    first actual note lands at exactly the SOFFS beat offset, zero notes
    exist before it -- exporting from raw tick 0 instead of SOFFS played
    that leading silence as real clip content, shifting everything later
    by roughly a bar. 0.0 (no offset) if SOFFS is absent or its beats
    field is 0. See PLAN.md "Item SOFFS (source offset / pre-roll)"."""
    m = _ITEM_SOFFS_RE.search(item_chunk)
    return float(m.group(2)) if m else 0.0


_AUDIO_SOURCE_FILE_RE = re.compile(r'<SOURCE (?:WAVE|MP3|FLAC|OGG|AIFF)\b.*?FILE\s+"([^"]+)"', re.DOTALL)
_ITEM_SOFFS_SECONDS_ONLY_RE = re.compile(r"^\s*SOFFS\s+([\d.eE+-]+)\s*$", re.MULTILINE)


def item_name(item_chunk: str) -> str | None:
    """Item's own `NAME` tag (quoted or bare) -- for an audio item this is
    normally the source WAV's filename unless the user renamed it, used as
    the exported AudioClip's display name since multiple audio items on one
    track need distinct names (unlike MIDI clips, which use the track
    name)."""
    m = _NAME_RE.search(item_chunk)
    return _name_value(m) if m else None


def audio_item_file_path(item_chunk: str) -> str | None:
    """Absolute source file path from an audio item's `<SOURCE WAVE ... FILE
    "path">` block (or MP3/FLAC/OGG/AIFF -- see track_has_audio_item)."""
    m = _AUDIO_SOURCE_FILE_RE.search(item_chunk)
    return m.group(1) if m else None


def item_source_offset_seconds(item_chunk: str) -> float:
    """An audio item's `SOFFS <seconds>` -- a SINGLE field, unlike a MIDI
    item's two-field `SOFFS <seconds> <beats>` (see item_source_offset_beats)
    -- audio has no tick/ppq coordinate space, so REAPER only stores the
    seconds value. How far into the source WAV file the item's own loop
    region/playback actually starts (REAPER's `OFFSET` concept). The
    single-field-only regex here naturally never matches a two-field MIDI
    SOFFS line (trailing `\\s*$` requires nothing else on the line). 0.0 if
    absent."""
    m = _ITEM_SOFFS_SECONDS_ONLY_RE.search(item_chunk)
    return float(m.group(1)) if m else 0.0


def item_playrate(item_chunk: str) -> float:
    """Item's time-stretch factor (`PLAYRATE <rate> <pitch-preserve flags...>`,
    only the first field matters here) -- 1.0 (native speed) if absent.
    A raw MIDI item's note ticks are unaffected by this (REAPER stores the
    source content's own tick positions regardless of playback rate); at
    rate < 1 the same ticks take proportionally longer to play back, so
    note position/length in the *project's* timeline must be scaled by
    `1 / playrate`, not used as-is. See PLAN.md "Item PLAYRATE + LOOP"."""
    m = _ITEM_PLAYRATE_RE.search(item_chunk)
    return float(m.group(1)) if m else 1.0


def item_pitch_semitones(item_chunk: str) -> float:
    """Item's REAPER "pitch adjust" (`PLAYRATE <rate> <preserve_pitch>
    <pitch_semitones> <mode> ...`, third field) -- independent of
    PLAYRATE's own rate field, applied at playback without altering the
    underlying source content. 0.0 (no shift) if PLAYRATE is absent or
    the field parses as 0. Confirmed against a real carrizal.RPP item
    (track "chords", octave-down item): `PLAYRATE 1 1 -12 -1 0 0.0025`
    vs sibling items on the same track reading `... 1 0 -1 0 0.0025`."""
    m = _ITEM_PLAYRATE_PITCH_RE.search(item_chunk)
    return float(m.group(1)) if m else 0.0


def item_loop_enabled(item_chunk: str) -> bool:
    """`LOOP 1` -- REAPER tiles/repeats the item's own source content to
    fill the item's full displayed LENGTH (which need not be a whole
    multiple of the source's natural length; REAPER allows loop-dragging
    an item to an arbitrary length, cutting the last repeat short)."""
    m = _ITEM_LOOP_RE.search(item_chunk)
    return m is not None and m.group(1) != "0"


def parse_midi_item_notes(item_chunk: str) -> list[MidiNote]:
    """Extract note events from a REAPER MIDI item, positions/lengths in
    beats relative to the item's own start (tick 0), not absolute project
    position -- item placement (POSITION, PLAYRATE time-stretch) is a
    separate, not-yet-implemented step. Returns [] if the item has no
    <SOURCE MIDI> block or no HASDATA header."""
    header_m = _HASDATA_HEADER_RE.search(item_chunk)
    if header_m is None:
        return []
    ppq = int(header_m.group(1))
    if ppq <= 0:
        return []

    notes: list[MidiNote] = []
    open_notes: dict[tuple[int, int], tuple[int, int]] = {}  # (channel, pitch) -> (start_tick, velocity)
    tick = 0
    for delta, m in _iter_midi_take_events(item_chunk):
        tick += delta
        if m is None:
            continue
        status = int(m.group(2), 16)
        event_type = status >> 4
        channel = status & 0x0F
        data1 = int(m.group(3), 16)
        data2 = int(m.group(4), 16)

        if event_type == 0x9 and data2 > 0:  # note-on
            open_notes[(channel, data1)] = (tick, data2)
        elif event_type == 0x8 or (event_type == 0x9 and data2 == 0):  # note-off
            key = (channel, data1)
            if key in open_notes:
                start_tick, velocity = open_notes.pop(key)
                notes.append(
                    MidiNote(
                        position=start_tick / ppq,
                        pitch=data1,
                        velocity=velocity,
                        length=(tick - start_tick) / ppq,
                    )
                )
    return notes


def parse_midi_item_cc_events(item_chunk: str) -> list[MidiCCEvent]:
    """Extract Control Change events (status nibble 0xB) from a REAPER MIDI
    item, positions in beats relative to the item's own start (tick 0) --
    same tick-accumulation convention as parse_midi_item_notes, same
    HASDATA/ppq gating. Ableton's own MIDI CC lanes are per (track, CC
    number) -- see PLAN.md "MIDI CC automation" -- so channel is not
    tracked here, only CC number and value. Returns [] under the same
    conditions parse_midi_item_notes does."""
    header_m = _HASDATA_HEADER_RE.search(item_chunk)
    if header_m is None:
        return []
    ppq = int(header_m.group(1))
    if ppq <= 0:
        return []

    events: list[MidiCCEvent] = []
    tick = 0
    for delta, m in _iter_midi_take_events(item_chunk):
        tick += delta
        if m is None:
            continue
        status = int(m.group(2), 16)
        if status >> 4 != 0xB:
            continue
        events.append(MidiCCEvent(position=tick / ppq, cc_number=int(m.group(3), 16), value=int(m.group(4), 16)))
    return events


def midi_item_raw_length_beats(item_chunk: str) -> float | None:
    """The MIDI source's own natural length in raw (unscaled-by-playrate)
    beats -- the true loop-cycle boundary for a `LOOP 1` item, NOT
    reliably the same as the last note's sounding end. A pattern's last
    note commonly ends shortly before the true loop point (a rest before
    the pattern repeats), so `max(note.position + note.length)` alone
    undershoots it -- confirmed against a real chirriado.RPP item where
    the last note ends at 11.06 raw beats but the pattern's real length,
    revealed by a trailing non-note event (e.g. a CC) at the very end of
    the take, is exactly 12.0 (a clean 4 bars at 3/4) -- exporting with
    the note-only estimate made the loop restart audibly early ("a
    quarter note too soon") once scaled up to project beats. This scans
    ALL event lines (not just note-on/off) and returns the position of
    the LAST one, same tick-accumulation logic as parse_midi_item_notes.
    Returns None under the same conditions that function returns []."""
    header_m = _HASDATA_HEADER_RE.search(item_chunk)
    if header_m is None:
        return None
    ppq = int(header_m.group(1))
    if ppq <= 0:
        return None

    tick = 0
    max_tick = 0
    for delta, _ in _iter_midi_take_events(item_chunk):
        tick += delta
        max_tick = max(max_tick, tick)
    return max_tick / ppq if max_tick > 0 else None


@dataclass
class RawRs5kBlock:
    """The three base64 sub-fields of one RS5k VST chunk, undecoded params."""

    header: bytes
    body: bytes  # WAV path + packed params (format not yet reverse-engineered)
    tail: bytes
    wav_path: str | None = field(default=None)


_B64_WRAP_WIDTH = 128  # RPP wraps embedded base64 fields at this column width


def _decode_b64_fields(inner_text: str) -> list[bytes]:
    """A <VST ...> block holds several base64 fields (header/body/tail...),
    each possibly wrapped across multiple lines. Confirmed empirically
    (chirriado.RPP): continuation lines are exactly _B64_WRAP_WIDTH chars;
    a field ends at the first line shorter than that width."""
    fields = []
    buf: list[str] = []
    for raw_line in inner_text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        buf.append(line)
        if len(line) < _B64_WRAP_WIDTH or "=" in line:
            fields.append(base64.b64decode("".join(buf)))
            buf = []
    if buf:
        fields.append(base64.b64decode("".join(buf)))
    return fields


def find_rs5k_blocks(rpp_text: str) -> list[RawRs5kBlock]:
    blocks = []
    for m in _VST_RS5K_RE.finditer(rpp_text):
        parts = _decode_b64_fields(m.group(1))
        if len(parts) < 2:
            continue
        header, body = parts[0], parts[1]
        tail = parts[2] if len(parts) > 2 else b""
        wav_path = None
        end = body.find(b".wav")
        if end != -1:
            start = body.rfind(b"\x00", 0, end)
            start = 0 if start == -1 else start + 1
            wav_path = body[start : end + 4].decode("utf-8", errors="replace")
        blocks.append(RawRs5kBlock(header=header, body=body, tail=tail, wav_path=wav_path))
    return blocks


_VST_BLOCK_RE = re.compile(r'<VST "([^"]+)"([^\n]*)\n(.*?)\n\s*>', re.DOTALL)
_VST3_GUID_RE = re.compile(r"\{([0-9A-Fa-f]{32})\}")


@dataclass
class RawVstBlock:
    """One `<VST ...>` FX block, not RS5k-specific -- FX-matching research
    (2026-07-24), see PLAN.md "FX/device matching research". `name` is the
    FX's own display string, e.g. `"VST3i: Surge XT (Surge Synth Team)
    (2->6ch)"` or `"VSTi: ReaSamplOmatic5000 (Cockos)"`. `fields` are the
    block's undecoded base64 sub-fields (same field-splitting as RS5k's
    `header`/`body`/`tail`, but generic: a non-RS5k plugin can have a
    different field count/meaning). `guid_hex` is the plugin's own raw
    32-hex-digit VST3 class id from the header line's `{HEXID}` (VST3
    only -- `None` for VST2, which uses angle brackets `<HEXID>`
    instead, not captured here since nothing needs it yet) -- this is
    the SAME real id Ableton's own `<Uid>`/`BranchDeviceId` fields
    encode for a given plugin (confirmed for Surge XT and every Waves
    plugin checked so far), usable to build a device xml for a plugin
    with no dedicated reference-extracted template, see
    als_builder.build_waves_generic_effect_device_xml."""

    name: str
    is_vst3: bool
    fields: list[bytes]
    guid_hex: str | None = None


def find_vst_blocks(rpp_text: str) -> list[RawVstBlock]:
    """Every `<VST ...>` FX block in the given text (usually a single
    track's chunk), regardless of plugin -- generalizes `find_rs5k_blocks`.
    `is_vst3` is inferred from the display name's own convention (REAPER
    prefixes it `"VST3i: ..."`/`"VST3: ..."` for VST3 vs `"VSTi: ..."`/
    `"VST: ..."` for VST2 -- confirmed against real `chirriado.RPP` FX:
    RS5k/VST2 uses angle-bracket ids `<HEXID>`, VST3 uses curly-brace ids
    `{HEXID}`, and the display-name prefix tracks this consistently)."""
    blocks = []
    for m in _VST_BLOCK_RE.finditer(rpp_text):
        name = m.group(1)
        guid_match = _VST3_GUID_RE.search(m.group(2))
        fields = _decode_b64_fields(m.group(3))
        blocks.append(
            RawVstBlock(
                name=name,
                is_vst3=name.startswith("VST3"),
                fields=fields,
                guid_hex=guid_match.group(1) if guid_match else None,
            )
        )
    return blocks


_PRESET_XML_MARKERS = (
    (b"<PresetChunkXMLTree", b"</PresetChunkXMLTree>"),
    (b"<?xml", None),
)


def extract_embedded_preset_xml(data: bytes) -> str | None:
    """Some VST3 plugins' own state chunk (opaque bytes REAPER just
    stores verbatim, defined entirely by the plugin/vendor, not by
    REAPER or Ableton -- see PLAN.md "FX/device matching research") is
    itself a readable, self-describing XML preset rather than raw binary:

    - JUCE-based plugins (confirmed: Surge XT) serialize their whole
      state as a plain `<?xml ...>` document (Surge's own native preset
      format, the same one used for `.fxp` files).
    - Many other VST2/VST3 plugins (confirmed: Waves' "WaveShell1-VST3"
      shell, wrapping CLA Guitars/RVerb) use a `<PresetChunkXMLTree
      version="..."><Preset>...<Chunk>...(base64 DSP state)...</Chunk>
      </Preset></PresetChunkXMLTree>` wrapper -- a known, documented VST2
      preset-interchange convention (not this project's invention),
      readable/host-independent even though the inner `<Chunk>` payload
      is the plugin's own opaque binary state.

    Returns the raw XML text (decoded permissively, `errors="replace"`),
    or None if neither marker is found -- e.g. genuinely opaque
    proprietary binary state with no embedded self-describing wrapper at
    all, which is also a real, expected case (not every plugin uses
    either convention)."""
    for start_marker, end_marker in _PRESET_XML_MARKERS:
        i = data.find(start_marker)
        if i == -1:
            continue
        if end_marker is not None:
            j = data.find(end_marker, i)
            if j == -1:
                continue
            chunk = data[i : j + len(end_marker)]
        else:
            j = data.find(b"\x00\x00\x00\x00", i)
            chunk = data[i:j] if j != -1 else data[i:]
        return chunk.decode("utf-8", errors="replace")
    return None


def vst3_processor_state_hex(block: RawVstBlock) -> str | None:
    """The transplant-ready state blob for a JUCE-based VST3 plugin
    (confirmed: Surge XT -- see PLAN.md "FX/device matching research"),
    as uppercase hex ready to drop into Ableton's own `<ProcessorState>`
    field verbatim.

    Byte-diffed against a real Ableton-saved reference (same Surge XT
    preset, saved from both REAPER and Ableton): Ableton's
    `<Vst3Preset><ProcessorState>` is hex-encoded (not base64, unlike
    REAPER's own VST wrapping) but otherwise carries the EXACT SAME
    `"sub3" + 4-byte LE length + 24-byte zero pad + <?xml ...>
    <patch>...</patch> + "JUCEPrivateData"` structure REAPER stores --
    just missing REAPER's own extra 8-byte outer envelope
    (`len1`/`flag` uint32s REAPER itself prepends before "sub3", and an
    8-byte all-zero field REAPER appends after "JUCEPrivateData" that
    Ableton's own field doesn't have either). Content was byte-identical
    between hosts except one live-performance field (`<modwheel>`, a
    momentary controller position, not a saved patch parameter) --
    confirmed NOT evidence of data loss.

    Scans every field (not a fixed index -- REAPER's field count/order
    isn't guaranteed the same across plugins) for the first one
    containing both markers; slices from `"sub3"` through the end of
    `"JUCEPrivateData"`, dropping REAPER's extra envelope bytes on both
    ends the same way the byte-diff showed Ableton's own field excludes
    them. Returns `None` if no field has both markers (e.g. a non-JUCE
    or non-VST3 plugin -- no transplant shortcut available for those,
    see `extract_embedded_preset_xml` for the Waves/PresetChunkXMLTree
    case instead)."""
    end_tag = b"JUCEPrivateData"
    for raw in block.fields:
        start = raw.find(b"sub3")
        if start == -1:
            continue
        end_marker = raw.find(end_tag, start)
        if end_marker == -1:
            continue
        end = end_marker + len(end_tag)
        return raw[start:end].hex().upper()
    return None


def waves_vst3_processor_state_hex(block: RawVstBlock) -> str | None:
    """The transplant-ready state blob for a Waves VST3 plugin (confirmed:
    RVerb -- see PLAN.md "FX/device matching research"), as uppercase hex
    ready to drop into Ableton's own `<ProcessorState>` field verbatim.
    Sibling to `vst3_processor_state_hex` (the JUCE/Surge case) -- same
    idea, different wrapper format, confirmed the SAME way (byte-diffed
    a real Ableton-saved reference against REAPER's own stored state for
    the identical RVerb preset, both hosts holding byte-identical content
    apart from the specific knob values the user changed between saves).

    REAPER stores this as `<len:4 BE><"\\x00\\x00\\x00\\x03"><"\\x00\\x00\\x00
    \\x01"><4-char-plugin-code>"setA"<len:4 BE>"XPst"<PresetChunkXMLTree
    ...>...</PresetChunkXMLTree>\\x00\\x00\\x00\\x00<Bypass .../>\\n` -- a
    real, documented VST2-era preset-interchange convention (see
    `extract_embedded_preset_xml`), NOT this project's invention -- plus
    REAPER's own extra 8-byte outer envelope before it and 8 trailing
    zero bytes after it, neither of which Ableton's own field carries
    (identical shape to the JUCE case's own envelope, just a different
    inner wrapper).

    The 4-char code right before `"setA"` identifies the specific Waves
    plugin (confirmed: `"RRFX"` for RVerb, `"CGTX"` for CLA Guitars --
    PLAN.md's earlier research already flagged `"CGTXsetA"`/`"RRFXsetA"`
    as both real, just hadn't confirmed a second one until CLA Guitars),
    so this searches for the generic `<4 uppercase letters>"setA"`
    pattern rather than hardcoding RVerb's own tag.

    Marker-sliced (not a fixed byte offset) for robustness: start is 12
    bytes before the matched tag (the outer BE length field plus two
    4-byte tag fields that immediately precede it, all confirmed present
    verbatim in Ableton's own field too, for both RVerb and CLA Guitars);
    end is the newline right after the trailing `<Bypass .../>` element.
    Returns `None` if no field has a `setA`-tagged Waves marker (e.g. a
    non-Waves plugin -- no transplant shortcut for those)."""
    tag_re = re.compile(rb"[A-Z]{4}setA")
    for raw in block.fields:
        m = tag_re.search(raw)
        if m is None or m.start() < 12:
            continue
        tag_pos = m.start()
        start = tag_pos - 12
        bypass_pos = raw.find(b"<Bypass", tag_pos)
        if bypass_pos == -1:
            continue
        end = raw.find(b"\n", bypass_pos)
        if end == -1:
            continue
        end += 1
        return raw[start:end].hex().upper()
    return None


def vst2_processor_state_hex(block: RawVstBlock) -> str | None:
    """The transplant-ready state blob for a native (non-Waves) ReaPlugs
    VST2 effect (confirmed: ReaDelay -- byte-diffed a real Ableton-saved
    reference against REAPER's own stored state for the SAME preset,
    saved from both hosts after the user matched knob values across
    them: identical byte-for-byte across the whole 76-byte field except
    two 4-byte float32 slots, which were the two knob values the user's
    match wasn't numerically exact on -- confirms this is a direct,
    unwrapped transplant, unlike the JUCE/Waves cases which need
    envelope-stripping).

    REAPER splits a native VST2 plugin's raw state into 3 base64 fields
    the same header/body/tail shape as RS5k's own split (see
    `find_rs5k_blocks`) -- field[0] is a short REAPER-internal header,
    field[2] a short trailer, and field[1] (the "body") is Ableton's own
    `<Buffer>` content verbatim, no REAPER envelope to strip at all.

    Returns `None` if the block doesn't have exactly this 3-field shape
    (e.g. a plugin using a different wrapping convention -- no transplant
    shortcut available for those)."""
    if len(block.fields) != 3:
        return None
    return block.fields[1].hex().upper()


_JS_BLOCK_RE = re.compile(r'(?:BYPASS\s+(\d)\s+\d+\s+\d+\s*\n\s*)?<JS (\S+) ""\s*\n\s*([^\n]+)\n\s*>')


@dataclass
class RawJsBlock:
    """One `<JS ...>` FX block (a REAPER JSFX effect instance, distinct
    from `<VST ...>` blocks -- JS FX chunks are REAPER's own dedicated
    plain-text serialization, not base64). `name` is REAPER's raw token
    verbatim, e.g. `"SStillwell/delay_pong"` -- confirmed (byte-diffed a
    real Ableton-saved reajs reference, `carrizal.RPP`'s own `<JS
    SStillwell/delay_pong ...>` instance) that this token is copied
    verbatim into Ableton's own `<Buffer>`, no stripping needed. The
    doubled "S" is NOT a REAPER-added marker (initial hypothesis, since
    "Stillwell" itself also starts with S making that easy to mis-read)
    -- it's genuinely part of the real path: the effect's actual file on
    disk lives at `REAPER/Effects/SStillwell/delay_pong` (confirmed
    against a real REAPER user Effects folder), i.e. the vendor folder
    really is named "SStillwell". Use `name` as-is, unmodified, for the
    Ableton transplant. `params` is the raw space-separated param line
    (floats in REAPER's own short ASCII form, `"-"` for an unset/default
    slot) -- also confirmed to transplant as plain ASCII text, since
    ReaJS/Ableton's own reference `<Buffer>` for the same instance stores
    the identical convention (just formatted with full float precision,
    which doesn't matter -- both are parsed as text, not fixed-width
    binary). `enabled` is `None` if no `BYPASS` line was found
    immediately before the block (REAPER always writes one directly
    above each FX's chunk in both confirmed real instances, but this
    isn't assumed to be universal)."""

    name: str
    params: str
    enabled: bool | None = None


def find_js_blocks(rpp_text: str) -> list[RawJsBlock]:
    blocks = []
    for m in _JS_BLOCK_RE.finditer(rpp_text):
        bypass_flag, name, params = m.groups()
        blocks.append(
            RawJsBlock(
                name=name,
                params=params.strip(),
                enabled=(bypass_flag == "0") if bypass_flag is not None else None,
            )
        )
    return blocks


def reajs_processor_state_hex(name: str, params: str) -> str:
    """Build the transplant-ready state blob for a `reajs` (ReaJS/YSFX-
    sibling JSFX host) device's `<Buffer>`, from a `RawJsBlock`'s own
    `name`/`params`. Confirmed format (byte-diffed a real Ableton-saved
    reajs reference loading `Stillwell/delay_pong`): plain ASCII
    `<name>\\x00<params>` followed by a handful of trailing null bytes --
    no REAPER envelope, no fixed total length (a text buffer parsed at
    runtime by ReaJS itself, not consulted as fixed-width binary by
    Ableton). 5 trailing nulls matches the real reference exactly; not
    load-bearing (any reasonable null padding should work), kept for
    parity with the one confirmed-working reference."""
    payload = name.encode("ascii") + b"\x00" + params.encode("ascii") + b"\x00" * 5
    return payload.hex().upper()


# Any FX chunk header: `<VST "VST3: Name (Vendor)" ...`, `<CLAP "CLAPi: ..."`,
# `<AU "AU: ..."`, `<LV2 ...`, `<DX ...`, `<JS path ""` / `<JS "path with space" ""`.
_ANY_FX_RE = re.compile(r'^\s*<(VST|CLAP|AU|LV2|DX|JS)\s+(?:"([^"]*)"|(\S+))', re.MULTILINE)


def list_track_fx(track_chunk: str) -> list[str]:
    """Display name of every FX in a track chunk, in document order --
    same names `find_vst_blocks` reports (`"VST3: CLA Bass Stereo
    (Waves)"`), JS FX as `"JS: <path>"`. Covers the same scope the
    device matchers search (the whole chunk, take FX included), so the FX
    report lines up with what was actually converted."""
    out = []
    for m in _ANY_FX_RE.finditer(track_chunk):
        kind, quoted, bare = m.groups()
        name = quoted if quoted is not None else bare
        out.append(f"JS: {name}" if kind == "JS" else name)
    return out


def track_names(rpp_text: str) -> list[str]:
    return [_name_value(m) for m in _NAME_RE.finditer(rpp_text)]


def _split_top_level_chunks(text: str, tag: str) -> list[str]:
    """RPP's format is line-based bracket nesting: a chunk opens with a line
    starting `<TAG ...` and closes with a line that's just `>`, at matching
    depth (any other line starting with `<` opens a nested chunk). Returns
    the raw text of each top-level chunk with the given tag name (e.g.
    "TRACK") — tracks aren't nested inside each other, so this finds each
    track's own block without picking up nested item/FX sub-blocks."""
    lines = text.split("\n")
    tag_re = re.compile(r"^\s*<" + re.escape(tag) + r"\b")
    chunks = []
    depth = 0
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if depth == 0:
            if tag_re.match(line):
                start = i
                depth = 1
            continue
        if stripped.startswith("<"):
            depth += 1
        elif stripped == ">":
            depth -= 1
            if depth == 0:
                chunks.append("\n".join(lines[start : i + 1]))
                start = None
    return chunks


@dataclass
class Track:
    name: str
    chunk: str  # this track's raw RPP text, for further parsing (e.g. RS5k instances)


def parse_tracks(rpp_text: str) -> list[Track]:
    tracks = []
    for chunk in _split_top_level_chunks(rpp_text, "TRACK"):
        m = _NAME_RE.search(chunk)
        tracks.append(Track(name=_name_value(m) if m else "", chunk=chunk))
    return tracks


# Params reliably readable on-disk (index 0..15 of the live-API table,
# see rs5k.py PARAM). Higher indices (loop mode variants, choke-related)
# don't line up with this layout and aren't needed for v1 export scope.
_RS5K_ONDISK_PARAM_COUNT = 16


@dataclass
class Rs5kFields:
    wav_path: str
    volume: float  # linear gain, unity = 1.0 (already correct, not 2x-scaled)
    pan: float  # 0..1, 0.5 = center
    gain_min_vel: float
    note_lo: int  # MIDI note 0-127
    note_hi: int
    pitch_note_lo: float
    pitch_note_hi: float
    attack: float  # raw normalized param, see rs5k.py for real-unit conversion
    release: float
    obey_note_off: bool
    loop: bool
    pitch_st_param: float  # normalized 0..1; see rs5k.py param_to_pitch()


def extract_rs5k_fields(block: RawRs5kBlock) -> Rs5kFields | None:
    """Decode the on-disk param array for one RS5k block. Returns None if
    no WAV is loaded (empty slot) or the body is too short to contain the
    full param array."""
    if block.wav_path is None:
        return None
    path_end = block.body.find(b".wav") + 4
    base = path_end + 1
    needed = base + _RS5K_ONDISK_PARAM_COUNT * 8
    if needed > len(block.body):
        return None

    def p(i: int) -> float:
        return struct.unpack_from("<d", block.body, base + i * 8)[0]

    return Rs5kFields(
        wav_path=block.wav_path,
        volume=p(0),
        pan=p(1),
        gain_min_vel=p(2),
        note_lo=round(p(3) * 127),
        note_hi=round(p(4) * 127),
        pitch_note_lo=p(5),
        pitch_note_hi=p(6),
        attack=p(9),
        release=p(10),
        obey_note_off=p(11) >= 0.5,
        loop=p(12) >= 0.5,
        pitch_st_param=p(15),
    )
