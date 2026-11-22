"""DVD-referenced LD audio clock: rate and origin of the native EFM WAV on the master timeline.

Port of v38 ``measure_audio_rate.py``, made automatic and more precise:

* The DVD's own audio is in sync with the DVD's picture (that is the reference). Matching a
  window of LD audio to the DVD stereo track gives its DVD time, and the act curves of the
  picture alignment (``fstk.media.alignment``) map that back to stack-master time. Fitting master
  time against WAV time over many windows gives the LD audio clock:

      master_s = intercept + slope × wav_s          rate_hz = 44100 / slope

  ``intercept`` is the master time of WAV sample 0, i.e. the origin. AudioSkip for an output
  whose frame 0 is master time ``delta_s`` is ``delta_s − intercept`` on the corrected (44.1 kHz)
  timeline.
* **Centre-referenced pairs** (finding 0001): correlating a window between clocks that differ by r
  aligns window centres, so each pair is (WAV window centre, master time of the matched centre).
* **Two-stage matching:** a coarse peak on 50 ms envelopes within ±``search_s`` of the prediction,
  then a fine peak on 5 ms envelopes with parabolic interpolation (sub-millisecond).
* **Bootstrapped:** the first prediction comes from a long window matched against the whole DVD,
  so nothing depends on a guessed origin.
* Windows that straddle an act break, fall in a gap of the picture map, are near-silent, have a
  weak or ambiguous peak, or hit the search edge are rejected. The fit is robust (Huber IRLS,
  then outliers beyond max(4·MAD, 20 ms) are dropped), and its spread is reported.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

FPS = 30000.0 / 1001.0
SR = 8000                 # analysis sample rate of the mono decodes
COARSE_HOP = 0.05
FINE_HOP = 0.005
WINDOW_S = 20.0
SPACING_S = 12.0
SEARCH_S = 8.0
FINE_RADIUS_S = 0.25
MIN_CORR = 0.6
MIN_MARGIN = 0.05         # peak minus best value ≥ 1 s away
EDGE_GUARD_S = 15.0       # keep windows this far from the WAV ends


def envelope(x: np.ndarray, hop_s: float, sr: int = SR) -> np.ndarray:
    """Log-RMS envelope (v38 ``measure_audio_rate.envelope``) at ``hop_s``."""
    hop = round(sr * hop_s)
    n = len(x) // hop
    if n == 0:
        return np.empty(0)
    f = x[:n * hop].reshape(n, hop).astype(np.float64)
    return np.log1p(np.sqrt(np.mean(f * f, axis=1)))


def slide_corr(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Pearson correlation of ``x`` against every same-length window of ``y`` (FFT)."""
    x = x.astype(np.float64) - x.mean()
    y = y.astype(np.float64)
    n, m = len(x), len(y)
    if m < n or not np.any(x):
        return np.full(max(m - n + 1, 0), -1.0)
    nfft = 1 << (n + m - 2).bit_length()
    conv = np.fft.irfft(np.fft.rfft(y, nfft) * np.fft.rfft(x[::-1], nfft), nfft)
    dot = conv[n - 1:n - 1 + (m - n + 1)]
    s0 = np.concatenate(([0.0], np.cumsum(y)))
    s1 = np.concatenate(([0.0], np.cumsum(y * y)))
    sums = s0[n:] - s0[:-n]
    energy = s1[n:] - s1[:-n] - sums * sums / n
    return dot / np.sqrt(float(np.dot(x, x)) * np.maximum(energy, 1e-12))


def _parabolic(s: np.ndarray, k: int) -> float:
    if 0 < k < len(s) - 1:
        a, b, c = s[k - 1], s[k], s[k + 1]
        den = a - 2 * b + c
        if den < 0:
            return float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    return 0.0


class ActMap:
    """Master ↔ DVD time through the alignment's act curves (``video.dvd_alignment`` acts)."""

    def __init__(self, acts: list[dict]):
        if not acts:
            raise ValueError("no alignment acts")
        self.acts = []
        for a in acts:
            m = np.asarray(a["knots"], float)                     # master frames
            d = m + np.asarray(a["values"], float)                # DVD frames
            if np.any(np.diff(d) <= 0):
                raise ValueError("an act curve is not monotonic in DVD time")
            self.acts.append((m, d))

    def act_of_master(self, m_frame: float) -> int | None:
        for i, (m, _d) in enumerate(self.acts):
            if m[0] <= m_frame <= m[-1]:
                return i
        return None

    def act_of_dvd(self, d_frame: float) -> int | None:
        for i, (_m, d) in enumerate(self.acts):
            if d[0] <= d_frame <= d[-1]:
                return i
        return None

    def master_to_dvd_s(self, master_s: float) -> float | None:
        i = self.act_of_master(master_s * FPS)
        if i is None:
            return None
        m, d = self.acts[i]
        return float(np.interp(master_s * FPS, m, d)) / FPS

    def dvd_to_master_s(self, dvd_s: float, act: int | None = None) -> float | None:
        i = self.act_of_dvd(dvd_s * FPS) if act is None else act
        if i is None:
            return None
        m, d = self.acts[i]
        if not d[0] <= dvd_s * FPS <= d[-1]:
            return None
        return float(np.interp(dvd_s * FPS, d, m)) / FPS


@dataclass
class Window:
    wav_centre_s: float
    master_centre_s: float | None
    dvd_centre_s: float | None
    corr: float
    margin: float
    used: bool
    reason: str = ""


@dataclass
class ClockFit:
    slope: float                          # master s per WAV s
    intercept_s: float                    # master time of WAV sample 0
    rate_hz: float                        # 44100 / slope
    rate_ppm: float                       # (rate / 44100 − 1)·1e6
    residual_mad_ms: float
    residual_max_ms: float
    windows_used: int
    windows_total: int
    coverage: float                       # used-window span / WAV duration
    loo_rate_ppm_range: list[float]
    intercept_stderr_ms: float
    slope_stderr_ppm: float
    windows: list[Window] = field(default_factory=list)
    #: Mean residual per picture act (ms). Structure here is the picture map's error, not the audio's:
    #: the LD audio clock is one straight line (S01E20: one act sits ≈ 1 frame off).
    per_act_residual_ms: list[dict] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d["windows"] = [asdict(w) for w in self.windows]
        return d


def _fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float, np.ndarray]:
    A = np.vstack([x, np.ones_like(x)]).T
    w = np.ones_like(x)
    sol = np.zeros(2)
    for _ in range(25):
        sol = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0]
        r = y - A @ sol
        scale = 1.4826 * float(np.median(np.abs(r))) + 1e-5
        w = 1.0 / np.maximum(1.0, np.abs(r) / (2 * scale))
    return float(sol[0]), float(sol[1]), y - A @ sol


def bootstrap(ld_coarse: np.ndarray, dvd_coarse: np.ndarray, amap: ActMap, *, at_s: float | None = None,
              length_s: float = 60.0) -> tuple[float, float]:
    """(intercept guess, corr): match one long LD window against the whole DVD envelope."""
    total = len(ld_coarse) * COARSE_HOP
    at = at_s if at_s is not None else total * 0.3
    n = int(length_s / COARSE_HOP)
    a0 = int(at / COARSE_HOP)
    x = ld_coarse[a0:a0 + n]
    s = slide_corr(x, dvd_coarse)
    k = int(np.argmax(s))
    dvd_start = k * COARSE_HOP
    master = amap.dvd_to_master_s(dvd_start + length_s / 2)
    if master is None:
        raise ValueError("bootstrap window matched outside the picture map")
    return master - (at + length_s / 2), float(s[k])


def measure_clock(ld: np.ndarray, dvd: np.ndarray, acts: list[dict], *, sr: int = SR,
                  window_s: float = WINDOW_S, spacing_s: float = SPACING_S, search_s: float = SEARCH_S,
                  progress=None) -> ClockFit:
    """Fit the LD WAV clock against the DVD-referenced master timeline. ``ld``/``dvd``: mono PCM at ``sr``."""
    amap = ActMap(acts)
    ld_c, dvd_c = envelope(ld, COARSE_HOP, sr), envelope(dvd, COARSE_HOP, sr)
    ld_f, dvd_f = envelope(ld, FINE_HOP, sr), envelope(dvd, FINE_HOP, sr)
    intercept, _ = bootstrap(ld_c, dvd_c, amap)
    slope = 1.0
    wav_dur = len(ld) / sr
    starts = np.arange(EDGE_GUARD_S, wav_dur - window_s - EDGE_GUARD_S, spacing_s)
    windows: list[Window] = []
    nc, nf = round(window_s / COARSE_HOP), round(window_s / FINE_HOP)
    quiet = np.percentile(ld_c, 20)
    for i, w_req in enumerate(starts):
        if progress and i % 20 == 0:
            progress(i / len(starts))
        ic = round(w_req / COARSE_HOP)
        w0 = ic * COARSE_HOP                      # window start exactly on both envelope grids
        i_f = round(w0 / FINE_HOP)
        wc = w0 + window_s / 2
        m0, m1 = intercept + slope * w0, intercept + slope * (w0 + window_s)
        a0, a1 = amap.act_of_master(m0 * FPS), amap.act_of_master(m1 * FPS)
        if a0 is None or a0 != a1:
            windows.append(Window(wc, None, None, 0.0, 0.0, False, "straddles an act break or a map gap"))
            continue
        x = ld_c[ic:ic + nc]
        if len(x) < nc or np.std(x) < 1e-3 or np.median(x) <= quiet:
            windows.append(Window(wc, None, None, 0.0, 0.0, False, "near-silent"))
            continue
        d_pred = amap.master_to_dvd_s(m0)
        jc = max(0, round((d_pred - search_s) / COARSE_HOP))
        y = dvd_c[jc:round((d_pred + window_s + search_s) / COARSE_HOP)]
        s = slide_corr(x, y)
        if not len(s):
            windows.append(Window(wc, None, None, 0.0, 0.0, False, "outside the DVD"))
            continue
        k = int(np.argmax(s))
        far = s.copy()
        far[max(0, k - 20):k + 21] = -1.0
        margin = float(s[k] - far.max()) if len(far) else 0.0
        if k <= 1 or k >= len(s) - 2:
            windows.append(Window(wc, None, None, float(s[k]), margin, False, "search edge"))
            continue
        coarse_start = (jc + k) * COARSE_HOP
        # fine stage
        xf = ld_f[i_f:i_f + nf]
        jf = max(0, round((coarse_start - FINE_RADIUS_S) / FINE_HOP))
        yf = dvd_f[jf:round((coarse_start + window_s + FINE_RADIUS_S) / FINE_HOP)]
        sf = slide_corr(xf, yf)
        if len(xf) < nf or not len(sf):
            windows.append(Window(wc, None, None, float(s[k]), margin, False, "fine window incomplete"))
            continue
        kf = int(np.argmax(sf))
        dvd_start = (jf + kf + _parabolic(sf, kf)) * FINE_HOP
        dvd_centre = dvd_start + window_s / 2
        act = amap.act_of_dvd(dvd_start * FPS)
        if act is None or act != amap.act_of_dvd((dvd_start + window_s) * FPS):
            windows.append(Window(wc, None, dvd_centre, float(sf[kf]), margin, False, "DVD match straddles a break"))
            continue
        master_c = amap.dvd_to_master_s(dvd_centre, act)
        ok = sf[kf] >= MIN_CORR and margin >= MIN_MARGIN
        windows.append(Window(wc, master_c, dvd_centre, float(sf[kf]), margin, bool(ok),
                              "" if ok else f"weak peak (corr {sf[kf]:.2f}, margin {margin:.2f})"))
        # refresh the prediction as evidence accumulates
        used = [(w.wav_centre_s, w.master_centre_s) for w in windows if w.used]
        if len(used) >= 6:
            xs, ys = np.array(used).T
            slope, intercept, _ = _fit(xs, ys)
    used = [w for w in windows if w.used]
    if len(used) < 8:
        raise ValueError(f"only {len(used)} usable windows (of {len(windows)}); cannot fit the LD clock")
    x = np.array([w.wav_centre_s for w in used])
    y = np.array([w.master_centre_s for w in used])
    slope, intercept, r = _fit(x, y)
    mad = 1.4826 * float(np.median(np.abs(r)))
    keep = np.abs(r) <= max(4 * mad, 0.020)
    for w, k in zip(used, keep, strict=True):
        if not k:
            w.used, w.reason = False, f"outlier ({1000 * (w.master_centre_s - (intercept + slope * w.wav_centre_s)):+.1f} ms)"
    x, y = x[keep], y[keep]
    slope, intercept, r = _fit(x, y)
    n = len(x)
    A = np.vstack([x, np.ones_like(x)]).T
    sigma = float(np.sqrt(np.sum(r ** 2) / max(n - 2, 1)))
    cov = sigma ** 2 * np.linalg.inv(A.T @ A)
    loo = []
    for j in range(0, n, max(1, n // 40)):
        s_, _, _ = _fit(np.delete(x, j), np.delete(y, j))
        loo.append((1.0 / s_ - 1.0) * 1e6)
    per_act = []
    for i, (mk, _dk) in enumerate(amap.acts):
        sel = (y * FPS >= mk[0]) & (y * FPS <= mk[-1])
        if sel.any():
            per_act.append({"act": i, "start_s": round(mk[0] / FPS, 2), "end_s": round(mk[-1] / FPS, 2),
                            "windows": int(sel.sum()), "mean_ms": round(1000 * float(r[sel].mean()), 2),
                            "sd_ms": round(1000 * float(r[sel].std()), 2)})
    return ClockFit(per_act_residual_ms=per_act,
                    slope=slope, intercept_s=intercept, rate_hz=44100.0 / slope, rate_ppm=(1.0 / slope - 1.0) * 1e6,
                    residual_mad_ms=1000 * 1.4826 * float(np.median(np.abs(r))),
                    residual_max_ms=1000 * float(np.abs(r).max()), windows_used=n, windows_total=len(windows),
                    coverage=float((x.max() - x.min()) / wav_dur), loo_rate_ppm_range=[min(loo), max(loo)],
                    intercept_stderr_ms=1000 * float(np.sqrt(cov[1, 1])),
                    slope_stderr_ppm=1e6 * float(np.sqrt(cov[0, 0])), windows=windows)
