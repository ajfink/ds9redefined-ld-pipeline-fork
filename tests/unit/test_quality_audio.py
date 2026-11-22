import numpy as np

from fstk.quality import audio as qa

RATE = 48000
rng = np.random.default_rng(5)


def tone(seconds=2.0):
    t = np.arange(int(RATE * seconds)) / RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 1330 * t) + 0.005 * rng.normal(size=t.size)).astype(np.float64)


def test_click_found_and_repaired_without_touching_the_rest():
    x = tone()
    y = x.copy()
    y[60000] += 0.8
    ev = qa.find_clicks(y, RATE)
    assert any(abs(e.start - 60000) < 4 for e in ev)
    fixed, done, left = qa.repair(y, [e for e in ev if abs(e.start - 60000) < 4], RATE)
    assert not left and abs(fixed[60000] - x[60000]) < 0.05
    mask = np.ones(len(x), bool); mask[59990:60012] = False
    assert np.array_equal(fixed[mask], y[mask])                # nothing else changed, bit for bit


def test_short_gap_is_filled_long_gap_is_only_flagged():
    x = tone(3.0)
    y = x.copy(); y[50000:50240] = 0          # 5 ms
    y[100000:101200] = 0                      # 25 ms
    gaps = qa.find_gaps(y, RATE)
    assert {round(g.amount) for g in gaps} == {5, 25}
    fixed, done, left = qa.repair(y, gaps, RATE)
    assert len(done) == 1 and done[0].start == 50000 and len(left) == 1
    assert np.abs(fixed[50000:50240] - x[50000:50240]).max() < 0.06
    assert np.all(fixed[100000:101200] == 0)


def test_digital_silence_between_quiet_passages_is_not_a_gap():
    x = np.zeros(RATE * 2); assert qa.find_gaps(x, RATE) == []


def test_channels_phase_swap_lfe():
    a = tone(); b = -a
    rows = qa.channel_findings(np.stack([a, b], 1), ["L", "R"])
    assert next(r for r in rows if "correlation" in r["check"])["status"] == "FLAG"
    other = 0.3 * np.sin(2 * np.pi * 523 * np.arange(len(a)) / RATE) + 0.01 * rng.normal(size=len(a))
    ref = np.stack([a, other], 1)
    assert qa.swap_hint(np.stack([ref[:, 1], ref[:, 0]], 1), ref)["status"] == "FLAG"
    assert qa.lfe_findings(np.sin(2 * np.pi * 60 * np.arange(RATE * 2) / RATE), RATE)["status"] == "PASS"
    assert qa.lfe_findings(tone(), RATE)["status"] == "FLAG"


def test_ebur128_parse_and_range_guard():
    text = "...\nSummary:\n\n  Integrated loudness:\n    I:         -24.1 LUFS\n    Threshold: -34.6 LUFS\n\n  Loudness range:\n    LRA:        11.3 LU\n\n  True peak:\n    Peak:       -2.0 dBFS\n"
    got = qa.parse_ebur128(text)
    assert got == {"integrated_lufs": -24.1, "lra_lu": 11.3, "true_peak_dbfs": -2.0}
    squashed = {"integrated_lufs": -24.1, "lra_lu": 6.0, "true_peak_dbfs": -8.0}
    assert [r["status"] for r in qa.dynamic_range_preserved(got, squashed)] == ["FAIL", "FAIL"]
    assert all(r["status"] == "PASS" for r in qa.dynamic_range_preserved(got, dict(got)))


def test_subtitle_offset_finds_a_late_track():
    hop = 0.05
    env = np.full(int(60 / hop), -70.0)
    cues = [(5 + 7 * k, 7 + 7 * k) for k in range(7)]
    for a, b in cues:                                  # speech actually 0.4 s later than the cues say
        env[int((a + 0.4) / hop):int((b + 0.4) / hop)] = -25.0
    r = qa.subtitle_offset(cues, env)
    assert abs(r["best_shift_s"] - 0.4) < 0.06 and r["status"] == "FLAG" and r["score_db_best"] > r["score_db_zero"]


def test_programme_transient_is_not_mistaken_for_a_click():
    x = tone(2.0)
    y = x.copy()
    k = np.arange(300)
    y[60000:60300] += 0.7 * np.exp(-k / 40) * np.sin(2 * np.pi * 900 * k / RATE)     # decaying hit: carries on
    hit = qa.Event(0, 60000, 3, "click", 30.0)
    _fixed, done, left = qa.repair(y, [hit], RATE)
    assert not done and "programme material" in left[0][1]
