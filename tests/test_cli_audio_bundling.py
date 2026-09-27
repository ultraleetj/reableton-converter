"""cli._bundle_audio_clip_samples -- see PLAN.md "Audio/sample bundling
in export-project". Copies each AudioItemSpec's source WAV into
`<dest_dir>/Samples/Imported` and repoints wav_path, deduping by
source path across a shared cache (same file used by multiple clips
copied only once)."""

from rpp_to_als.als_builder import AudioItemSpec
from rpp_to_als.cli import _bundle_audio_clip_samples


def _clip(wav_path, name="clip"):
    return AudioItemSpec(
        wav_path=wav_path,
        name=name,
        start_beats=0.0,
        length_beats=4.0,
        duration_frames=44100,
        sample_rate=44100,
    )


def test_bundle_audio_clip_samples_copies_and_repoints(tmp_path):
    src = tmp_path / "source.wav"
    src.write_bytes(b"RIFF....WAVEfmt ")
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    clips = [_clip(str(src))]
    copied: dict[str, str] = {}
    _bundle_audio_clip_samples(clips, str(dest_dir), copied)

    bundled = dest_dir / "Samples" / "Imported" / "source.wav"
    assert bundled.exists()
    assert bundled.read_bytes() == src.read_bytes()
    assert clips[0].wav_path == str(bundled)


def test_bundle_audio_clip_samples_dedupes_shared_source_across_calls(tmp_path):
    src = tmp_path / "shared.wav"
    src.write_bytes(b"data")
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    copied: dict[str, str] = {}
    clips_a = [_clip(str(src), "a")]
    clips_b = [_clip(str(src), "b")]
    _bundle_audio_clip_samples(clips_a, str(dest_dir), copied)
    _bundle_audio_clip_samples(clips_b, str(dest_dir), copied)

    assert clips_a[0].wav_path == clips_b[0].wav_path
    assert len(copied) == 1
