"""DVD-referenced LD audio clock (rate + origin through the picture act curves)."""
from __future__ import annotations

import numpy as np
import pytest

from fstk.media import dvd_audio as da

FPS = da.FPS
SLOPE, ICPT = 1.001, 3.2          # master_s = 3.2 + 1.001 × wav_s  (≈ −999 ppm, origin 3.2 s)
# two acts: DVD runs 30 frames ahead of master, then (after a 15-frame LD-only insert) 45 frames
ACTS = [{"knots": [0.0, 150 * FPS], "values": [-30.0, -30.0]},
        {"knots": [151 * FPS, 330 * FPS], "values": [-45.0, -45.0]}]


def content(m_s: np.ndarray, rng_seed=1) -> np.ndarray:
    """A programme 'sound' as a function of master time: noise under a slowly varying envelope."""
    rng = np.random.default_rng(rng_seed)
    knots = np.arange(-10, 400, 0.15)
    level = np.exp(rng.normal(0, 1.0, len(knots)))
    env = np.interp(m_s, knots, level)
    carrier = np.sin(2 * np.pi * 437 * m_s) + 0.5 * np.sin(2 * np.pi * 1171 * m_s + 1)
    return (env * carrier * 2000).astype(np.float32)


def make():
    t_ld = np.arange(int(320 * da.SR)) / da.SR                       # LD WAV time
    ld = content(ICPT + SLOPE * t_ld)
    amap = da.ActMap(ACTS)
    t_dvd = np.arange(int(325 * da.SR)) / da.SR
    m = np.array([amap.dvd_to_master_s(t) if amap.act_of_dvd(t * FPS) is not None else np.nan
                  for t in t_dvd[::80]])                              # every 10 ms, then interpolate
    m = np.interp(t_dvd, t_dvd[::80][np.isfinite(m)], m[np.isfinite(m)])
    dvd = content(m)
    return ld, dvd


@pytest.fixture(scope="module")
def fit():
    ld, dvd = make()
    return da.measure_clock(ld, dvd, ACTS)


def test_recovers_rate_and_origin(fit):
    assert abs(fit.intercept_s - ICPT) * 1000 < 2.0, fit.intercept_s
    assert abs(fit.rate_hz - 44100 / SLOPE) < 0.1, fit.rate_hz           # ≈ 2 ppm
    assert fit.windows_used >= 15 and fit.residual_mad_ms < 5


def test_windows_across_the_act_break_are_rejected(fit):
    straddle = [w for w in fit.windows if "break" in w.reason]
    assert straddle and all(not w.used for w in straddle)
    assert all(abs(w.wav_centre_s - (150 - ICPT) / SLOPE) < 40 for w in straddle)


def test_act_map_round_trips():
    amap = da.ActMap(ACTS)
    for m in (10.0, 149.0, 200.0, 320.0):
        d = amap.master_to_dvd_s(m)
        assert abs(amap.dvd_to_master_s(d) - m) < 1e-9
    assert amap.master_to_dvd_s(150.5) is None                         # LD-only insert: no DVD picture
