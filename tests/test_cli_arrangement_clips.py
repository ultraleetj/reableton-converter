"""cli._arrangement_midi_clips_for_track: REAPER item PLAYRATE + LOOP
handling -- a raw MIDI item's note ticks are unaffected by PLAYRATE (the
same ticks just take proportionally longer/shorter to actually play), so
position/length must be scaled by `1 / playrate` to land correctly on the
project's own beat timeline. When `LOOP 1` is set, the item's displayed
length can exceed the source content's own natural length -- REAPER
tiles/repeats it to fill the gap. See PLAN.md "Item PLAYRATE + LOOP",
motivated by chirriado.RPP's first track (PLAYRATE 0.5, LOOP 1).
"""

from rpp_to_als.cli import _arrangement_midi_clips_for_track


def _item(position=10.0, length=4.0, playrate=1.0, loop=False, soffs_beats=0.0, notes="e 0 90 3c 64\n        e 240 80 3c 00"):
    return f"""    <ITEM
      POSITION {position}
      LENGTH {length}
      LOOP {1 if loop else 0}
      SOFFS 0 {soffs_beats}
      PLAYRATE {playrate} 1 0 -1 0 0.0025
      <SOURCE MIDI
        HASDATA 1 480 QN
        {notes}
      >
    >
"""


def test_playrate_scales_note_position_and_length():
    # ppq=480; note at tick 0, length 240 ticks == 0.5 raw beats.
    item = _item(playrate=0.5, loop=False)
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    assert len(clips) == 1
    notes, start_beats, length_beats, loop_length_beats, _cc_events = clips[0]
    assert len(notes) == 1
    # rate 0.5 (half speed) -> same ticks take 2x as long -> beats double
    assert notes[0].position == 0.0
    assert notes[0].length == 1.0


def test_playrate_one_leaves_notes_unscaled():
    item = _item(playrate=1.0, loop=False)
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, *_ = clips[0]
    assert notes[0].length == 0.5


def test_loop_enabled_sets_loop_length_to_note_span_not_full_clip():
    # LENGTH=4s at tempo=120 -> 8 beats; the single note only spans 1 beat
    # (after playrate 0.5 scaling) -- with LOOP set, the clip's loop
    # region should be that 1-beat span, not the full 8-beat clip length,
    # so Ableton tiles the note across the whole clip like REAPER does.
    item = _item(playrate=0.5, loop=True)
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, start_beats, length_beats, loop_length_beats, _cc_events = clips[0]
    assert length_beats == 8.0
    assert loop_length_beats == 1.0


def test_loop_disabled_leaves_loop_length_equal_to_full_clip():
    item = _item(playrate=0.5, loop=False)
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, start_beats, length_beats, loop_length_beats, _cc_events = clips[0]
    assert loop_length_beats == length_beats


def test_loop_length_uses_trailing_non_note_event_not_last_note_end():
    # Regression: chirriado.RPP's real items have a trailing non-note
    # event (e.g. a CC) marking the true end of the take, AFTER the last
    # note actually sounds -- a pattern's last note commonly ends
    # shortly before the real loop point (a trailing rest). Using
    # max(note.position+note.length) alone undershoots the loop length,
    # making the pattern restart audibly early once scaled to project
    # beats ("a quarter note too soon", user-reported). Here: note ends
    # at tick 240 (0.5 raw beat), but a trailing CC event at tick 480
    # (1.0 raw beat) marks the real pattern boundary -- loop_length_beats
    # must reflect the CC's position, not the note's.
    item = _item(
        playrate=1.0,
        loop=True,
        length=4.0,
        notes="e 0 90 3c 64\n        e 240 80 3c 00\n        e 240 b0 07 40",
    )
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, start_beats, length_beats, loop_length_beats, _cc_events = clips[0]
    assert len(notes) == 1
    assert notes[0].position + notes[0].length == 0.5  # note itself unaffected
    assert loop_length_beats == 1.0  # but loop point follows the trailing CC


def test_loop_enabled_but_notes_already_fill_clip_leaves_loop_length_as_full():
    # notes spanning the whole clip (no gap to fill) should not produce a
    # loop_length_beats larger than or equal to length_beats.
    item = _item(position=0.0, length=0.5, playrate=1.0, loop=True, notes="e 0 90 3c 64\n        e 480 80 3c 00")
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, start_beats, length_beats, loop_length_beats, _cc_events = clips[0]
    assert loop_length_beats == length_beats


def test_soffs_shifts_notes_to_start_at_clip_zero():
    # Regression: "cucharas" on chirriado.RPP was recorded with pre-roll
    # -- the raw MIDI take includes a leading stretch of silence (the
    # count-in) before the real performance, trimmed by SOFFS rather than
    # removed from the take. Without applying SOFFS, the exported clip
    # played that leading silence as real content, shifting everything
    # later by roughly a bar (confirmed: chirriado.RPP's cucharas item
    # has its first real note landing at EXACTLY the SOFFS beat offset,
    # zero notes before it). Here: note at raw tick 480 (1.0 beat) with
    # SOFFS of 1.0 beat should land at clip position 0.0, not 1.0.
    item = _item(
        playrate=1.0,
        soffs_beats=1.0,
        notes="e 480 90 3c 64\n        e 240 80 3c 00",
    )
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, *_ = clips[0]
    assert len(notes) == 1
    assert notes[0].position == 0.0


def test_soffs_drops_notes_before_the_offset():
    # A note that (per raw tick data) falls entirely before SOFFS is part
    # of the trimmed pre-roll, not real clip content -- must not appear
    # at a negative position in the exported clip.
    item = _item(
        playrate=1.0,
        soffs_beats=2.0,
        notes="e 0 90 3c 64\n        e 240 80 3c 00\n        e 720 90 3e 64\n        e 240 80 3e 00",
    )
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, *_ = clips[0]
    assert all(note.position >= 0 for note in notes)
    assert len(notes) == 1  # only the second note (pitch 0x3e) survives SOFFS


def test_soffs_reduces_loop_length_before_playrate_scaling():
    # Regression: chirriado.RPP's "swing th1..." item index 9 has both
    # PLAYRATE 0.5 and SOFFS 6.0 raw beats -- without subtracting SOFFS
    # first, the computed loop length was double the correct value
    # (reps=0.5, an impossible partial loop); with it, reps become a
    # clean whole number. Here: raw take spans 12 beats total, SOFFS
    # trims the first 6 -- effective raw pattern is 6 beats, /playrate
    # 0.5 = 12 project beats. Clip length is deliberately 13 beats (not
    # exactly 12) so the "loop_length_beats < length_beats" tiling branch
    # is unambiguously exercised rather than coinciding with the
    # no-tiling-needed boundary case.
    item = _item(
        playrate=0.5,
        loop=True,
        soffs_beats=6.0,
        length=6.5,  # 6.5s * 120bpm/60 = 13 beats
        notes="e 0 90 3c 64\n        e 240 80 3c 00\n        e 5280 90 3e 64\n        e 240 80 3e 00",
    )
    clips = _arrangement_midi_clips_for_track(item, tempo=120.0)
    notes, start_beats, length_beats, loop_length_beats, _cc_events = clips[0]
    assert length_beats == 13.0
    assert loop_length_beats == 12.0
