"""Method validation: the envelope cross-correlation + Theil–Sen fit recovers a known clock error.

v38's tests check the fit on hand-made points; this drives the whole measurement chain
(envelope → sliding correlation → per-window offsets → robust line) on synthetic audio whose
true drift is known, so a regression in any step shows up as a wrong ppm.
"""
from __future__ import annotations

import numpy as np

from fstk.media.audio_timing import HOP, SR, envelope, robust_line_fit, slide_corr


def _signal(seconds: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    # bursty "dialogue-like" envelope: noise gated by a random slow on/off pattern
    gate = np.repeat(rng.random(int(seconds * 4)) > 0.45, SR // 4)[:n]
    return (rng.standard_normal(n) * (0.05 + gate)).astype(np.float32)


def test_recovers_known_drift_ppm():
    true_ppm = 864.0
    slope = 1.0 + true_ppm * 1e-6          # master_s = 0.37 + slope * wav_s
    offset = 0.37
    dur = 1500.0
    master = _signal(dur + 30)
    t_wav = np.arange(int(dur * SR)) / SR
    wav = np.interp(offset + slope * t_wav, np.arange(len(master)) / SR, master).astype(np.float32)
    env_m, env_w = envelope(master), envelope(wav)
    starts = []
    win, search = int(50.0 / HOP), int(8.0 / HOP)
    for centre_s in np.arange(100.0, dur - 100.0, 120.0):
        w0 = int(centre_s / HOP)
        guess = int((offset + slope * centre_s) / HOP)
        seg = env_w[w0:w0 + win]
        region = env_m[guess - search:guess + search + win]
        corr = slide_corr(seg, region)
        k = int(np.argmax(corr))
        # parabolic sub-hop refinement
        if 0 < k < len(corr) - 1:
            a, b, c = corr[k - 1], corr[k], corr[k + 1]
            k = k + 0.5 * (a - c) / (a - 2 * b + c)
        starts.append((centre_s, (guess - search + k) * HOP))
    half = 25.0  # W/2: reference each pair at the window centre (docs/findings/0001)
    xs = np.array([s[0] + half for s in starts])
    ys = np.array([s[1] + half for s in starts])
    fit_slope, intercept, mad, loo = robust_line_fit(xs, ys)
    assert abs((fit_slope - 1.0) * 1e6 - true_ppm) < 15.0
    assert abs(intercept - offset) < 0.010
    assert (loo[1] - loo[0]) * 1e6 < 30.0
    # window-start referencing (v38's convention) carries a drift-proportional origin bias
    xs0 = np.array([s[0] for s in starts])
    ys0 = np.array([s[1] for s in starts])
    _, intercept0, _, _ = robust_line_fit(xs0, ys0)
    assert abs((intercept0 - offset) - true_ppm * 1e-6 * half) < 0.010
