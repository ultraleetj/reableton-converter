"""adg_builder.get_wav_frames/get_audio_frames -- see PLAN.md "Track name /
UserName bug, MP3 audio support". Python's stdlib `wave` module only
understands PCM format 1 and raises `wave.Error: unknown format: 3` on
32-bit float WAV, which is REAPER's own recording default -- a real
project's audio track had every WAV item silently skipped because of
this before the mutagen fallback was added.
"""

import struct
import wave

from rpp_to_als.adg_builder import get_wav_frames


def _write_float32_wav(path, frames: int, sample_rate: int) -> None:
    """Hand-crafted minimal IEEE-float (format tag 3) mono WAV -- the
    stdlib `wave` module can only write PCM (format 1), so it can't
    produce the fixture this test needs."""
    data = b"\x00\x00\x00\x00" * frames  # frames * 4 bytes/sample (32-bit)
    fmt_chunk = struct.pack("<HHIIHH", 3, 1, sample_rate, sample_rate * 4, 4, 32)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 4 + (8 + len(fmt_chunk)) + (8 + len(data))))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", len(fmt_chunk)))
        f.write(fmt_chunk)
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


def test_stdlib_wave_rejects_float32_format(tmp_path):
    # Establishes the premise: this is genuinely unreadable via stdlib
    # `wave` alone, not a hypothetical.
    path = tmp_path / "float32.wav"
    _write_float32_wav(path, frames=1000, sample_rate=48000)
    try:
        with wave.open(str(path), "rb"):
            raise AssertionError("expected wave.Error for format-3 WAV")
    except wave.Error:
        pass


def test_get_wav_frames_falls_back_to_mutagen_for_float32(tmp_path):
    path = tmp_path / "float32.wav"
    _write_float32_wav(path, frames=48000, sample_rate=48000)
    get_wav_frames.cache_clear()
    result = get_wav_frames(str(path))
    assert result is not None
    frames, sample_rate = result
    assert sample_rate == 48000
    assert abs(frames - 48000) <= 1  # derived from mutagen's float-seconds duration


def test_get_wav_frames_still_exact_for_pcm(tmp_path):
    path = tmp_path / "pcm.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 1234)
    get_wav_frames.cache_clear()
    assert get_wav_frames(str(path)) == (1234, 44100)
