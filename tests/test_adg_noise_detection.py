from rpp_to_als.adg_builder import is_triaz_noise_sample, triaz_drum_type

_NOISE_DRUM_TYPE = r"X:\samplers\TRIAZ\Samples\TRIAZ - Factory Collection\Noise\wa-triaz-noise-cassette_lofi.wav"
_NOISE_TAG_UNDER_SNARE = (
    r"X:\samplers\TRIAZ\Samples\TRIAZ - Factory Collection\Snare Electronic\Noise\wa-triaz-snare_elec-noise-analog_snap.wav"
)
_ORDINARY = r"X:\samplers\TRIAZ\Samples\TRIAZ - Factory Collection\Kick Electronic\Punchy\wa-triaz-kick_elec-punchy-sub_attack.wav"


def test_true_noise_drum_type_detected():
    assert triaz_drum_type(_NOISE_DRUM_TYPE) == "Noise"
    assert is_triaz_noise_sample(_NOISE_DRUM_TYPE) is True


def test_noise_as_tag_not_drum_type_is_not_noise():
    # "Noise" here is a tag subfolder under "Snare Electronic", not the
    # drum_type itself -- must not be treated as a Noise pad.
    assert triaz_drum_type(_NOISE_TAG_UNDER_SNARE) == "Snare Electronic"
    assert is_triaz_noise_sample(_NOISE_TAG_UNDER_SNARE) is False


def test_ordinary_sample_not_noise():
    assert is_triaz_noise_sample(_ORDINARY) is False


def test_non_triaz_path_returns_none_type_and_not_noise():
    assert triaz_drum_type(r"C:\some\other\sample.wav") is None
    assert is_triaz_noise_sample(r"C:\some\other\sample.wav") is False
