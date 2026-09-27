"""Requires a real .rpp with RS5k instances; skips if not present on disk.

The param-extraction test cross-checks decoded on-disk values against
reference/rs5k_param_dump.csv, a ground-truth dump of the same project's
RS5k params read live via REAPER's API (tools/dump_rs5k_params.lua) — see
PLAN.md for how the on-disk byte layout was reverse-engineered against it.
"""

import csv
import os

import pytest

from rpp_to_als.rpp_parse import extract_rs5k_fields, find_rs5k_blocks, track_names

# Deliberately a frozen snapshot, NOT the user's live working copy (that's
# now at D:\REAPER Media\projs\jotas\chirriado\chirriado.RPP, used by the
# other test files) -- this one is paired 1:1 with the committed CSV
# ground-truth dump, taken from this exact snapshot's state. The live copy
# has since gained more RS5k instances, so pointing this test at it would
# break the row-count cross-check without indicating any real bug.
SAMPLE_RPP = r"C:\inProgress\docs\rpps\chirriado\chirriado.RPP"
PARAM_DUMP_CSV = os.path.join(os.path.dirname(__file__), "..", "reference", "rs5k_param_dump.csv")


@pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")
def test_finds_rs5k_blocks_and_wav_paths():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    blocks = find_rs5k_blocks(text)
    assert len(blocks) > 0
    paths = [b.wav_path for b in blocks if b.wav_path]
    assert len(paths) > 0
    assert all(p.lower().endswith(".wav") for p in paths)


@pytest.mark.skipif(not os.path.exists(SAMPLE_RPP), reason="sample project not present")
def test_finds_track_names():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    names = track_names(text)
    assert len(names) > 0


@pytest.mark.skipif(
    not (os.path.exists(SAMPLE_RPP) and os.path.exists(PARAM_DUMP_CSV)),
    reason="sample project or ground-truth param dump not present",
)
def test_extracted_params_match_live_api_dump():
    text = open(SAMPLE_RPP, encoding="utf-8").read()
    blocks = find_rs5k_blocks(text)
    with open(PARAM_DUMP_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == len(blocks)
    checked = 0
    for row, block in zip(rows, blocks):
        fields = extract_rs5k_fields(block)
        if fields is None:
            assert row["file0"] == ""  # empty RS5k slot, no params to check
            continue
        assert abs(fields.volume - float(row["param0"]) * 2) < 1e-6
        assert abs(fields.pan - float(row["param1"])) < 1e-6
        assert fields.note_lo == round(float(row["param3"]) * 127)
        assert fields.note_hi == round(float(row["param4"]) * 127)
        checked += 1
    assert checked > 0
