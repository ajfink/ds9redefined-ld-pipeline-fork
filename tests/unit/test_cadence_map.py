from __future__ import annotations

import random

import pytest

from fstk.core.errors import ArtifactError
from fstk.media.cadence import analyze_tfm_rows, parse_tfm_log
from fstk.media.cadence_map import di_map_text, ivtc_vpy_script, parse_ranges, repair_mask, repair_ranges


def synthetic_log(n=3000, seed=3):
    rng = random.Random(seed)
    lines = []
    cadence = "ccnnc"
    for i in range(n):
        code, mic = cadence[i % 5], rng.randint(0, 40)
        if 1200 <= i < 1320:            # interlaced insert: alternating combed frames
            mic = 95 if i % 2 else 50
        if i in (400, 401, 2500):
            mic = 70                     # the 65 < mic <= 80 band v38 deliberately repairs
        if i == 2700:
            code = "u"
        lines.append(f"{i} {code} [{mic}]")
    return "\n".join(lines) + "\n"


def test_powershell_rules_match_python_cadence_analysis():
    rows = parse_tfm_log(synthetic_log(), 3000)
    codes = [r["match_code"] for r in rows]
    mics = [int(r["mic"]) for r in rows]
    mask = repair_mask(codes, mics)
    analysis = analyze_tfm_rows(rows)
    assert sum(mask) == analysis["repair_frame_count"]
    ranges = repair_ranges(mask)
    assert (400, 401) in ranges and (2500, 2500) in ranges and (2700, 2700) in ranges
    insert = next(r for r in ranges if r[0] <= 1250 <= r[1])
    assert insert[0] <= 1200 and insert[1] >= 1319   # whole insert, not just the >80 frames
    assert parse_ranges(di_map_text(ranges)) == ranges


def test_repair_ranges_edges():
    assert repair_ranges([]) == []
    assert repair_ranges([True, True, False, True]) == [(0, 1), (3, 3)]


def test_tail_trim_drops_frames_from_end():
    script = ivtc_vpy_script("/m.mov", 118, 12, 30, [], "none", expected_frames=60000)
    assert "out = core.std.Trim(out, TRIM_START, out.num_frames - 1 - TRIM_END)" in script
    assert "assert out.num_frames == 60000" in script
    assert "TRIM_END = 30" in script
    no_tail = ivtc_vpy_script("/m.mov", 118, 12, 0, [], "none")
    assert "core.std.Trim(out, TRIM_START, None)" in no_tail


def test_measured_ranges_require_a_repair_choice():
    with pytest.raises(ArtifactError, match="deinterlace stage is required"):
        ivtc_vpy_script("/m.mov", 0, 0, 0, [(5, 9)], "none")
    s = ivtc_vpy_script("/m.mov", 0, 0, 0, [(5, 9)], "qtgmc")
    assert "QTempGaussMC" in s and "REPAIR_RANGES = [(5, 9)]" in s
    compile(s, "ivtc.vpy", "exec")
    assert "PP=2" in ivtc_vpy_script("/m.mov", 0, 0, 0, [(5, 9)], "pp2")
