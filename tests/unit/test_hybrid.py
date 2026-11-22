from fractions import Fraction

from fstk.media import hybrid as hy


def rows(film_cycles, video_cycles, tail_film=10):
    out = []
    pattern = "ccpnc"[:5]
    for _ in range(film_cycles):
        out += [{"match_code": c, "mic": 5} for c in "cnccp"]
    for _ in range(video_cycles):
        out += [{"match_code": "c", "mic": 200} for _ in range(5)]
    for _ in range(tail_film):
        out += [{"match_code": c, "mic": 5} for c in "cnccp"]
    del pattern
    for i, r in enumerate(out):
        r["frame"] = i
    return out


def test_video_stretch_is_found_and_cycle_aligned():
    segs = hy.plan_segments(hy.classify_cycles(rows(20, 24)))
    kinds = [(s.kind, s.first_cycle, s.end_cycle) for s in segs]
    assert kinds[0][0] == hy.FILM and ("video-interlaced" in [k[0] for k in kinds])
    v = next(s for s in segs if s.kind == hy.VIDEO_I)
    assert 18 <= v.first_cycle <= 22 and v.end_cycle - v.first_cycle >= 20


def test_short_blips_stay_film():
    segs = hy.plan_segments(hy.classify_cycles(rows(20, 3)))
    assert all(s.kind == hy.FILM for s in segs)


def test_duration_is_unchanged_by_the_rate_change():
    labels = hy.classify_cycles(rows(20, 24))
    segs = hy.plan_segments(labels)
    cfr = sum(s.cfr_frames for s in segs) * 1001 / 24000
    assert abs(hy.total_duration_s(segs) - cfr) < 1e-6          # same programme length: audio sync is unaffected
    tc = hy.timecodes_v2(segs).splitlines()
    assert tc[0].startswith("#") and len(tc) - 1 == sum(s.frames for s in segs)
    assert float(tc[-1]) < cfr * 1000 and Fraction(1001, 60000) * 1000 < Fraction(1001, 24000) * 1000


def test_summary_recommends_hybrid_only_when_needed():
    assert not hy.summary(hy.plan_segments(hy.classify_cycles(rows(30, 0))))["recommend_hybrid"]
    assert hy.summary(hy.plan_segments(hy.classify_cycles(rows(20, 24))))["recommend_hybrid"]
