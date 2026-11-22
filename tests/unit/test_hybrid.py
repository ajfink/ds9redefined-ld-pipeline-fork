import random

import pytest

from fstk.core.errors import ArtifactError
from fstk.media import hybrid as hy
from fstk.media.cadence_map import ivtc_vpy_script


def rows(film_cycles, video_cycles, tail_film=10):
    out = []
    for _ in range(film_cycles):
        out += [{"match_code": c, "mic": 5} for c in "cnccp"]
    for _ in range(video_cycles):
        out += [{"match_code": "c", "mic": 200} for _ in range(5)]
    for _ in range(tail_film):
        out += [{"match_code": c, "mic": 5} for c in "cnccp"]
    for i, r in enumerate(out):
        r["frame"] = i
    return out


def test_video_stretch_is_found_and_cycle_aligned():
    segs = hy.plan_segments(hy.classify_cycles(rows(20, 24)))
    v = next(s for s in segs if s.kind == hy.VIDEO_I)
    assert 18 <= v.first_cycle <= 22 and v.end_cycle - v.first_cycle >= 20
    assert v.output_frames == (v.first_cycle * 4, v.end_cycle * 4)


def test_short_blips_stay_film():
    assert all(s.kind == hy.FILM for s in hy.plan_segments(hy.classify_cycles(rows(20, 3))))


def test_nearest_offsets_are_the_closest_real_moments():
    # output slot s sits at 2.5·s bob frames / 1.25·s source frames; the chosen frames are within half a step
    for s, k in enumerate(hy.BOB_NEAREST):
        assert abs(k - 2.5 * s) <= 0.5
    for s, k in enumerate(hy.PROG_NEAREST):
        assert abs(k - 1.25 * s) <= 0.5


def _diffs(cycles, plate, rnd):
    out = []
    for _ in range(cycles):
        w = [rnd.uniform(0.02, 0.03) for _ in range(10)]
        if plate:                       # telecine: one repeat per parity, same phase every cycle
            w[3] = w[8] = 0.001 + rnd.uniform(0, 0.002)
        out += w
    return out


def test_film_plate_under_overlay_is_detected_and_true_video_is_not():
    rnd = random.Random(1)
    assert hy.film_plate_verdict(_diffs(12, True, rnd))["plate"] is True
    assert hy.film_plate_verdict(_diffs(12, False, rnd))["plate"] is False
    assert hy.film_plate_verdict([0.0] * 100)["plate"] is False       # static: uninformative, not a plate


def test_choices_and_cycle_map():
    segs = [hy.Segment(hy.FILM, 0, 20), hy.Segment(hy.VIDEO_I, 20, 30), hy.Segment(hy.VIDEO_P, 30, 40),
            hy.Segment(hy.VIDEO_I, 40, 50, plate=True)]
    segs = hy.apply_choices(segs, {"30": "interpolate", "40": "interpolate"})
    m = hy.cycle_treatments(segs)
    assert m[20] == "NI" and m[30] == "IP" and 40 not in m and 0 not in m     # plate stays film
    with pytest.raises(ValueError):
        hy.apply_choices(segs, {"20": "blend"})
    s = hy.summary(segs)
    assert s["video_segments"] == 2 and s["plate_segments"] == 1 and s["interpolated_segments"] == 1


def test_script_replaces_only_video_cycles_and_compiles():
    text = ivtc_vpy_script("m.mkv", 3, 2, 1, [(10, 40)], "pp2", 100, video_cycles={4: "NI", 5: "II", 8: "NP"},
                           probe_json="p.json", probe_head=4, probe_tail=4)
    compile(text, "ivtc.vpy", "exec")
    assert "znedi3.nnedi3(src, field=3)" in text            # pp2 route: znedi3 bob
    assert "SelectEvery(bob, 10, [0, 2, 5, 7])" in text and "SelectEvery(fm, 5, [0, 1, 2, 4])" in text
    assert "_flow(bob, 2)" in text and "FrameEval(dec" in text
    q = ivtc_vpy_script("m.mkv", 3, 0, 0, [(10, 40)], "qtgmc", video_cycles={4: "NI"})
    compile(q, "q.vpy", "exec")
    assert "bob = di" in q and "znedi3" not in q            # QTGMC route reuses its own bob
    with pytest.raises(ArtifactError):
        ivtc_vpy_script("m.mkv", 0, 0, 0, [], "none", video_cycles={1: "XX"})
