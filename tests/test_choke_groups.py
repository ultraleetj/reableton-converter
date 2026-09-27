from rpp_to_als.adg_builder import is_triaz_hihat_closed, is_triaz_hihat_open, parse_choke_pairs

_HH_CLOSED = r"X:\samplers\TRIAZ\Samples\TRIAZ - Factory Collection\Hihat Closed\808\wa-triaz-hihat_closed-808-808_2.wav"
_HH_OPEN = r"X:\samplers\TRIAZ\Samples\TRIAZ - Factory Collection\HiHat Open\Metallic\wa-triaz-hihat_open-metallic-reamp_metal.wav"
_ORDINARY = r"X:\samplers\TRIAZ\Samples\TRIAZ - Factory Collection\Kick Electronic\Punchy\wa-triaz-kick_elec-punchy-sub_attack.wav"


def test_hihat_closed_detected_case_insensitive_no_space():
    assert is_triaz_hihat_closed(_HH_CLOSED) is True
    assert is_triaz_hihat_open(_HH_CLOSED) is False


def test_hihat_open_detected_despite_camelcase_folder_name():
    # Library folder is "HiHat Open" (capital H), not "Hihat Open" like
    # the closed folder -- must still match via case-insensitive compare.
    assert is_triaz_hihat_open(_HH_OPEN) is True
    assert is_triaz_hihat_closed(_HH_OPEN) is False


def test_ordinary_sample_is_neither():
    assert is_triaz_hihat_closed(_ORDINARY) is False
    assert is_triaz_hihat_open(_ORDINARY) is False


def test_parse_choke_pairs_single():
    assert parse_choke_pairs("42-46") == {42: 1, 46: 1}


def test_parse_choke_pairs_multiple_groups():
    assert parse_choke_pairs("42-46 54-56") == {42: 1, 46: 1, 54: 2, 56: 2}
