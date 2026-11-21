"""Characterization: v38 tests/test_av_check_act_cuts.py (ported), plus synthetic checks of the
between-track and absolute A/V measurements ported from sync_check.py / av_check_output.py."""
from __future__ import annotations

import numpy as np
import pytest

from fstk.core.errors import ArtifactError as PipelineError
from fstk.qc.sync import (
    AV_HOP as HOP,
)
from fstk.qc.sync import (
    AV_SR,
    FPS,
    MIN_SUB_S,
    SYNC_HOP,
    SYNC_SR,
    OffsetModel,
    absolute_av,
    between_track_sync,
    break_gaps,
    corr_peak,
    envelope,
    fit_windows,
    split_at_cuts,
    sync_sample_points,
    track_lag,
)
from fstk.qc.sync import AV_WIN as WIN


# ------------------------------------------------------------------ v38 tests (verbatim logic)
def test_unstraddled_point_preserves_the_original_window():
    assert split_at_cuts(100.0, [(300.0, 302.0)]) == ([(100.0, WIN, 100.0)], [])


def test_single_cut_yields_separate_pre_and_post_windows():
    kept, dropped = split_at_cuts(900.0, [(903.9, 904.9)])
    assert len(kept) == 1 and kept[0][0] == 904.9 and kept[0][1] == pytest.approx(55.1) and kept[0][2] == 904.9
    assert dropped == [(900.0, 903.9)] and kept[0][1] >= MIN_SUB_S


def test_two_sufficient_sides_are_both_returned_with_local_anchors():
    kept, dropped = split_at_cuts(1900.0, [(1932.2, 1933.4)])
    assert len(kept) == 2 and dropped == []
    assert kept[0][:1] == (1900.0,) and kept[0][1] == pytest.approx(32.2) and kept[0][2] == 1900.0
    assert kept[1][0] == 1933.4 and kept[1][1] == pytest.approx(26.6) and kept[1][2] == 1933.4


def test_synthetic_step_is_measured_as_two_independent_correlations():
    rng = np.random.default_rng(20261116)
    ld_envelope = rng.normal(size=int(WIN / HOP))
    windows, dropped = split_at_cuts(0.0, [(30.0, 32.0)])
    assert dropped == [] and len(windows) == 2
    peaks = []
    for (start, duration, _anchor), shift in zip(windows, [5, 25], strict=True):
        i0, count = round(start / HOP), round(duration / HOP)
        segment = ld_envelope[i0:i0 + count]
        dvd_window = np.concatenate((np.zeros(shift), segment, np.zeros(16)))
        peaks.append(int(np.argmax(corr_peak(segment, dvd_window))))
    assert peaks == [5, 25]


def test_multiple_cuts_are_split_and_short_content_is_reported():
    kept, dropped = split_at_cuts(100.0, [(120.0, 121.0), (150.0, 155.0)])
    assert kept == [(100.0, 20.0, 100.0), (121.0, 29.0, 121.0)] and dropped == [(155.0, 160.0)]


def test_invalid_intervals_and_windows_fail_closed():
    with pytest.raises(PipelineError, match="invalid recorded black interval"):
        split_at_cuts(100.0, [(120.0, 119.0)])
    with pytest.raises(PipelineError, match="positive duration"):
        split_at_cuts(100.0, [], window_s=0)


# ------------------------------------------------------------------ between-track
def _bursty(rng, seconds, sr):
    """Speech-like amplitude bursts: an envelope with structure at the 5–200 ms scale."""
    n = int(seconds * sr)
    env = np.repeat(rng.random(int(seconds * 20)) ** 3, sr // 20)[:n]
    return (rng.normal(size=n) * env * 8000).astype(np.float32)


def test_track_lag_recovers_a_known_offset():
    rng = np.random.default_rng(1)
    x = _bursty(rng, 30, SYNC_SR)
    shift = int(0.120 * SYNC_SR)               # LD 120 ms later than 5.1
    a = envelope(np.concatenate((np.zeros(shift, np.float32), x))[: len(x)], SYNC_SR, SYNC_HOP)
    b = envelope(x, SYNC_SR, SYNC_HOP)
    lag, corr = track_lag(a, b)
    assert lag == pytest.approx(0.120, abs=0.006) and corr > 0.9


def test_between_track_constant_offset_passes_and_drift_fails():
    rng = np.random.default_rng(2)
    source = _bursty(rng, 400, SYNC_SR)

    def grab_with(drift_ppm):
        def grab(selector, t):
            i = int(t * SYNC_SR)
            seg = source[i:i + 60 * SYNC_SR]
            if selector == "0:a:1":
                return seg
            lag = 0.080 + drift_ppm * 1e-6 * t
            k = round(lag * SYNC_SR)
            return np.concatenate((np.zeros(k, np.float32), seg))[: len(seg)]
        return grab

    points = [20.0, 120.0, 220.0, 320.0]
    ok = between_track_sync(points, grab_with(0))
    assert ok["pass"] and ok["constant_ms"] == pytest.approx(80, abs=6) and abs(ok["slope_ppm"]) < 30
    bad = between_track_sync(points, grab_with(400))
    assert not bad["pass"] and bad["slope_ppm"] > 30


def test_sync_points_avoid_act_breaks():
    gaps = break_gaps([{"ld_black": [600.0, 602.0]}, {"ld_black": [1300.0, 1302.0]}], delta_s=10.0)
    assert gaps == [(590.0, 592.0), (1290.0, 1292.0)]
    pts = sync_sample_points(gaps, 2000.0)
    assert pts == [295.0, 941.0, 1646.0]
    with pytest.raises(PipelineError, match="no clear segment"):
        sync_sample_points([], 100.0)


# ------------------------------------------------------------------ absolute A/V
def test_fit_windows_and_offset_model_never_extrapolate():
    wins, short = fit_windows([(600.0, 602.0), (620.0, 621.0)], 1500.0)
    assert wins == [(0.0, 598.0), (623.0, 1500.0)] and short == [(604.0, 618.0)]
    master = np.arange(0, 1400 * FPS, 15)                        # alignment samples every 0.5 s
    align = np.stack([master, np.full_like(master, 12.0)], axis=1)  # DVD 12 frames later
    model = OffsetModel(align, wins)
    assert model(300.0) == pytest.approx(12.0) and model(610.0) == pytest.approx(12.0)
    with pytest.raises(PipelineError, match="outside the sampled alignment range"):
        model(1450.0)


def test_absolute_av_measures_a_known_audio_offset():
    rng = np.random.default_rng(3)
    duration, delta, off_frames, av_true = 900.0, 5.0, 12.0, 0.040   # D_a − D_v = +40 ms
    ld = _bursty(rng, duration, AV_SR)
    # Picture at output t is DVD time delta + t + off/FPS (D_v); the sound heard at t is found in the
    # DVD audio at t + k (D_a). Make it 40 ms later than the picture's DVD moment.
    k = delta + off_frames / FPS + av_true
    pad = round(k * AV_SR)
    dvd = np.concatenate((rng.normal(size=pad).astype(np.float32) * 10, ld))
    master = np.arange(0, (duration + 100) * FPS, 15)
    model = OffsetModel(np.stack([master, np.full_like(master, off_frames)], axis=1),
                        fit_windows([(400.0, 401.0)], duration + 50)[0])
    result = absolute_av(ld_audio=ld, dvd_audio=dvd, delta_s=delta, offset=model, blacks_out=[(395.0, 396.0)],
                         points=(50.0, 200.0, 380.0, 600.0, 800.0))
    assert result["pass"], result
    assert result["constant_ms"] == pytest.approx(av_true * 1000, abs=12) and abs(result["slope_ppm"]) < 30
    assert len(result["rows"]) == 6                   # the 380 s window is split around the black
