"""Audio sync gates: between-track drift (v38 ``sync_check.py``) and absolute A/V against the DVD
through the episode's picture map (v38 ``av_check_output.py``). Math ported unchanged.

Between-track: positive lag = LD event later than the 5.1 event. A constant offset between the two
tracks is expected; only drift is a defect. 60 s windows at the midpoints of the content segments
between act breaks. Gates: rms ≤ 40 ms, |slope| ≤ 30 ppm.

Absolute A/V: for an output window at t, ``D_v = (DELTA + t) + off(master)/FPS`` is the DVD time
of the picture and ``D_a = t + k`` the measured DVD-audio match; ``A/V = D_a − D_v = k − DELTA − off/FPS``.
Sign (stated precisely because v38's "positive = audio content later (LATE)" reads both ways):
positive means the sound heard at output time t belongs to a *later* DVD moment than the picture
shown at t, i.e. the sound arrives before its picture. The gate is symmetric. The offset model is fitted only inside content windows ≥ 2 s clear of
every fade (music-through blacks excluded), never extrapolated beyond the sampled alignment path.
Gates: |constant| ≤ 100 ms, |slope| ≤ 30 ppm, rms ≤ 40 ms.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from fstk.core.errors import ArtifactError

# ---------------------------------------------------------------- between-track constants
SYNC_SR = 48000
SYNC_HOP = 0.005
SYNC_WIN = 60.0
SYNC_MAX_WINDOWS = 7
SYNC_GATE_RMS_MS, SYNC_GATE_SLOPE_PPM = 40.0, 30.0

# ---------------------------------------------------------------- absolute A/V constants
FPS = 30000.0 / 1001.0
AV_SR = 16000
AV_HOP = 0.02
AV_WIN = 60.0
EDGE_CLEAR_S = 2.0
MIN_SUB_S = 8.0
MIN_SPAN_S = 30.0
DEFAULT_POINTS = (100.0, 500.0, 900.0, 1400.0, 1900.0, 2400.0, 2600.0)
AV_GATE_CONST_MS, AV_GATE_SLOPE_PPM, AV_GATE_RMS_MS = 100.0, 30.0, 40.0


def envelope(x: np.ndarray, sr: int, hop: float, *, minimum: int = 100) -> np.ndarray:
    n = int(sr * hop)
    m = len(x) // n
    if m < minimum:
        raise ArtifactError(f"window too short ({len(x)} samples)")
    return np.sqrt((x[: m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)


def _parabolic(y0: float, y1: float, y2: float) -> float:
    den = y0 - 2 * y1 + y2
    return 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0


def track_lag(a: np.ndarray, b: np.ndarray, hop: float = SYNC_HOP, max_lag_s: float = 1.0) -> tuple[float, float]:
    """Lag (s) of envelope ``a`` relative to ``b`` within ±max_lag_s, and the peak correlation."""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    a = (a - a.mean()) / a.std()
    b = (b - b.mean()) / b.std()
    maxlag = int(max_lag_s / hop)
    corr = np.empty(2 * maxlag + 1)
    base_b = b[maxlag:n - maxlag]
    for j, lag in enumerate(range(-maxlag, maxlag + 1)):
        corr[j] = float(np.mean(a[maxlag + lag:n - maxlag + lag] * base_b))
    k = int(np.argmax(corr))
    off = _parabolic(corr[k - 1], corr[k], corr[k + 1]) if 0 < k < len(corr) - 1 else 0.0
    return (k - maxlag + off) * hop, float(corr[k])


def break_gaps(breaks: list[dict], delta_s: float, *, extra: list[dict] = (), tail=None) -> list[tuple[float, float]]:
    """Recorded LD blacks (``ld_black`` [start, end] in master time) on the output timeline."""
    out = [(float(b["ld_black"][0]) - delta_s, float(b["ld_black"][1]) - delta_s)
           for b in [*(breaks or []), *(extra or [])] if b.get("ld_black")]
    if tail:
        out.append((float(tail[0]) - delta_s, float(tail[1]) - delta_s))
    return sorted(out)


def sync_sample_points(gaps: list[tuple[float, float]], duration_s: float) -> list[float]:
    """Midpoints of content segments long enough for a 60 s window, away from act breaks (v38)."""
    segs, cursor = [], 0.0
    for g0, g1 in sorted(gaps):
        if g0 - cursor > 2 * SYNC_WIN + 10:
            segs.append((cursor, g0))
        cursor = g1
    if duration_s - cursor > 2 * SYNC_WIN + 10:
        segs.append((cursor, duration_s))
    pts = [(lo + hi) / 2 for lo, hi in segs if hi - lo >= SYNC_WIN + 10]
    if not pts:
        raise ArtifactError(f"no clear segment long enough for a {SYNC_WIN:.0f} s window")
    if len(pts) > SYNC_MAX_WINDOWS:
        step = len(pts) / SYNC_MAX_WINDOWS
        pts = [pts[int(i * step)] for i in range(SYNC_MAX_WINDOWS)]
    return sorted(p for p in pts if 0 <= p <= duration_s - SYNC_WIN)


@dataclass(frozen=True)
class LinearFit:
    constant: float     # in the y unit
    slope_ppm: float
    rms: float          # in the y unit
    n: int
    slope_stderr_ppm: float | None = None      # 1σ of the slope from the fit residuals (None below 3 points)


def linear_fit(x: list[float], y: list[float], *, y_in_ms: bool) -> LinearFit:
    """Least-squares line; rms uses ddof=2 as v38. ``y_in_ms``: y in ms (A/V) vs seconds (sync)."""
    xa, ya = np.asarray(x, float), np.asarray(y, float)
    slope, const = np.polyfit(xa, ya, 1)
    res = ya - (slope * xa + const)
    rms = float(res.std(ddof=2)) if len(ya) > 2 else 0.0
    ppm = slope * 1000.0 if y_in_ms else slope * 1e6
    se_ppm = None
    if len(ya) > 2:
        sxx = float(((xa - xa.mean()) ** 2).sum())
        if sxx > 0:
            se = math.sqrt(float((res ** 2).sum()) / (len(ya) - 2) / sxx)
            se_ppm = se * (1000.0 if y_in_ms else 1e6)
    return LinearFit(float(const), float(ppm), rms, len(ya), se_ppm)


#: a slope within this many ppm of its gate, or within two standard errors of it, is "marginal": it passes (or
#: fails) by less than the measurement can tell apart, so more windows are measured before the gate is decided
SLOPE_MARGIN_PPM = 6.0


def slope_verdict(slope_ppm: float, stderr_ppm: float | None, limit_ppm: float) -> dict:
    """How far inside (or outside) its limit a drift slope is, counting the measurement's own uncertainty.

    ``verdict``: ``pass`` (clearly inside), ``marginal`` (inside, but the limit is within 2σ or
    ``SLOPE_MARGIN_PPM``), ``fail`` (outside). ``ambiguous`` is True when a failing slope is within 2σ of the
    limit, i.e. it could be a pass that the measurement noise pushed over. The limit is never relaxed: the
    ambiguity is resolved by measuring more.
    """
    sigma = stderr_ppm if stderr_ppm is not None else 0.0
    mag = abs(slope_ppm)
    headroom = limit_ppm - mag
    if mag > limit_ppm:
        verdict = "fail"
    elif headroom < max(SLOPE_MARGIN_PPM, 2 * sigma):
        verdict = "marginal"
    else:
        verdict = "pass"
    return {"verdict": verdict, "headroom_ppm": headroom, "stderr_ppm": stderr_ppm,
            "ambiguous": verdict == "fail" and mag - 2 * sigma <= limit_ppm,
            "limit_ppm": limit_ppm}


def between_track_sync(points: list[float], grab: Callable[[str, float], np.ndarray]) -> dict:
    """Measure LD (a:0) vs 5.1 (a:1) lag at each point; ``grab(selector, t)`` returns mono PCM."""
    rows = []
    for t in points:
        a = envelope(grab("0:a:0", t), SYNC_SR, SYNC_HOP)
        b = envelope(grab("0:a:1", t), SYNC_SR, SYNC_HOP)
        lag, corr = track_lag(a, b)
        rows.append({"time_s": t, "lag_s": lag, "corr": corr})
    if len(rows) < 2:
        raise ArtifactError(f"only {len(rows)} sync window(s); at least 2 are needed for a drift fit")
    fit = linear_fit([r["time_s"] for r in rows], [r["lag_s"] for r in rows], y_in_ms=False)
    rms_ms = fit.rms * 1000
    ok = rms_ms <= SYNC_GATE_RMS_MS and abs(fit.slope_ppm) <= SYNC_GATE_SLOPE_PPM
    return {"rows": rows, "constant_ms": fit.constant * 1000, "slope_ppm": fit.slope_ppm, "rms_ms": rms_ms,
            "pass": ok, "slope_stderr_ppm": fit.slope_stderr_ppm,
            "slope": slope_verdict(fit.slope_ppm, fit.slope_stderr_ppm, SYNC_GATE_SLOPE_PPM)}


def more_sync_points(gaps: list[tuple[float, float]], duration_s: float, have: list[float], want: int = 7) -> list[float]:
    """Extra 60 s windows inside the content segments, none overlapping a window already measured.

    Used to settle a marginal drift slope: the slope's uncertainty shrinks with more windows spread across the
    programme, so the gate is decided on enough data instead of on seven points that happen to straddle the limit.
    """
    cursor, segs = 0.0, []
    for g0, g1 in sorted(gaps):
        segs.append((cursor, g0))
        cursor = g1
    segs.append((cursor, duration_s))
    cand: list[float] = []
    for lo, hi in segs:
        t = lo + SYNC_WIN / 2 + 5
        while t + SYNC_WIN / 2 + 5 <= hi:
            cand.append(t)
            t += SYNC_WIN + 10
    free = [c for c in cand if all(abs(c - h) >= SYNC_WIN for h in have) and 0 <= c <= duration_s - SYNC_WIN]
    if len(free) > want:
        step = len(free) / want
        free = [free[int(i * step)] for i in range(want)]
    return sorted(free)


# ---------------------------------------------------------------- absolute A/V
def corr_peak(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Normalised correlation of ``a`` against every aligned window of ``b`` (FFT, v38)."""
    a = (a - a.mean()) / a.std()
    b = (b - b.mean()) / (b.std() + 1e-12)
    n, m = len(a), len(b)
    nfft = 1 << (n + m - 2).bit_length()
    conv = np.fft.irfft(np.fft.rfft(b, nfft) * np.fft.rfft(a[::-1], nfft), nfft)
    dot = conv[n - 1:n - 1 + (m - n + 1)]
    cum = np.concatenate(([0.0], np.cumsum(b)))
    cum2 = np.concatenate(([0.0], np.cumsum(b * b)))
    s = cum[n:] - cum[:-n]
    e = cum2[n:] - cum2[:-n] - s * s / n
    return dot / np.sqrt((a * a).sum() * np.maximum(e, 1e-9))


def split_at_cuts(start_s: float, blacks_out: list[tuple[float, float]], window_s: float = AV_WIN,
                  min_sub_s: float = MIN_SUB_S) -> tuple[list[tuple[float, float, float]], list[tuple[float, float]]]:
    """Split a correlation window around recorded blacks → (kept (start, dur, anchor), dropped spans)."""
    if not math.isfinite(start_s) or not math.isfinite(window_s) or window_s <= 0:
        raise ArtifactError("A/V window start and duration must be finite, with positive duration")
    if not math.isfinite(min_sub_s) or min_sub_s <= 0:
        raise ArtifactError("minimum A/V sub-window duration must be positive")
    spans = [(start_s, start_s + window_s)]
    for black_start, black_end in sorted(blacks_out):
        if not (math.isfinite(black_start) and math.isfinite(black_end)) or black_end <= black_start:
            raise ArtifactError(f"invalid recorded black interval: {black_start!r}..{black_end!r}")
        nxt = []
        for s0, s1 in spans:
            if black_end <= s0 or black_start >= s1:
                nxt.append((s0, s1))
                continue
            if black_start > s0:
                nxt.append((s0, min(black_start, s1)))
            if black_end < s1:
                nxt.append((max(black_end, s0), s1))
        spans = nxt
    kept, dropped = [], []
    for s0, s1 in spans:
        if s1 - s0 < min_sub_s:
            dropped.append((s0, s1))
        else:
            kept.append((s0, s1 - s0, s0))
    return kept, dropped


def fit_windows(blacks_master: list[tuple[float, float]], end_s: float,
                tail: tuple[float, float] | None = None) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Content windows (master time) between fades, pulled back by EDGE_CLEAR_S; short ones dropped."""
    blacks = sorted([*blacks_master, *([tuple(tail)] if tail else [])])
    if not blacks:
        raise ArtifactError("manifest has no retime.breaks ld_black entries")
    if tail:
        end_s = min(end_s, float(tail[0]))
    wins, cursor = [], 0.0
    for s, e in blacks:
        if s - EDGE_CLEAR_S > cursor + 5:
            wins.append((cursor, s - EDGE_CLEAR_S))
        cursor = max(cursor, e + EDGE_CLEAR_S)
    if end_s - cursor > 5:
        wins.append((cursor, end_s))
    return ([w for w in wins if w[1] - w[0] >= MIN_SPAN_S], [w for w in wins if w[1] - w[0] < MIN_SPAN_S])


class OffsetModel:
    """Per-content-window linear fits of the DVD alignment path (frames vs master seconds)."""

    def __init__(self, align: np.ndarray, windows: list[tuple[float, float]]):
        t, o = align[:, 0] / FPS, align[:, 1].astype(float)
        self.sampled = (float(t.min()), float(t.max()))
        o_s = o.copy()
        for k in range(2, len(o_s) - 2):
            o_s[k] = np.median(o[k - 2:k + 3])
        self.fits = []
        for lo, hi in windows:
            m = (t >= lo) & (t <= hi)
            if m.sum() < 20:
                continue
            slope, icept = np.polyfit(t[m], o_s[m], 1)
            self.fits.append((lo, hi, float(slope), float(icept)))
        if not self.fits:
            raise ArtifactError("no content window had enough samples to fit an offset model")

    def __call__(self, m: float) -> float:
        lo, hi = self.sampled
        if not lo <= m <= hi:
            raise ArtifactError(f"requested master time {m:.3f}s is outside the sampled alignment range "
                                f"{lo:.3f}..{hi:.3f}s; measure the alignment over this anchor first")

        def dist(f):
            a, b, _, _ = f
            return 0.0 if a <= m <= b else min(abs(m - a), abs(m - b))

        best = min(self.fits, key=dist)
        return best[2] * m + best[3]


def absolute_av(*, ld_audio: np.ndarray, dvd_audio: np.ndarray, delta_s: float, offset: OffsetModel,
                blacks_out: list[tuple[float, float]], points=DEFAULT_POINTS) -> dict:
    """Measure A/V at each point (split around blacks); fit constant/slope/rms (ms)."""
    rows, uncovered, dropped_all = [], [], []
    for t in points:
        subs, dropped = split_at_cuts(t, blacks_out)
        dropped_all += dropped
        if not subs:
            uncovered.append(t)
        for s, slen, anchor in subs:
            master = delta_s + anchor
            off = offset(master)
            d_v = master + off / FPS
            k0 = d_v - anchor
            a = envelope(ld_audio[int(s * AV_SR):int(s * AV_SR) + int(slen * AV_SR)], AV_SR, AV_HOP, minimum=10)
            lo, hi = k0 - 6.0, k0 + 6.0
            seg_start = s + lo
            if seg_start < 0:
                raise ArtifactError(f"DVD search window starts before the DVD audio at t={s:.1f}s")
            b = envelope(dvd_audio[int(seg_start * AV_SR):int(seg_start * AV_SR) + int(((hi - lo) + slen) * AV_SR)],
                         AV_SR, AV_HOP, minimum=10)
            sc = corr_peak(a, b)
            i = int(np.argmax(sc))
            refine = _parabolic(sc[i - 1], sc[i], sc[i + 1]) if 0 < i < len(sc) - 1 else 0.0
            k = lo + (i + refine) * AV_HOP
            av = k - delta_s - off / FPS
            rows.append({"out_time_s": s, "master_s": master, "offset_frames": off, "expected_lag_s": k0,
                         "measured_lag_s": k, "corr": float(sc[i]), "av_ms": av * 1000, "window_duration_s": slen})
    if uncovered:
        raise ArtifactError("no ≥8 s content sub-window remains for A/V point(s) "
                            + ", ".join(f"{p:.1f}s" for p in uncovered))
    if len(rows) < 3:
        raise ArtifactError(f"only {len(rows)} content windows remain; at least 3 are needed for an A/V fit")
    fit = linear_fit([r["out_time_s"] for r in rows], [r["av_ms"] for r in rows], y_in_ms=True)
    gates = {"constant": abs(fit.constant) <= AV_GATE_CONST_MS, "slope": abs(fit.slope_ppm) <= AV_GATE_SLOPE_PPM,
             "rms": fit.rms <= AV_GATE_RMS_MS}
    return {"rows": rows, "constant_ms": fit.constant, "slope_ppm": fit.slope_ppm, "rms_ms": fit.rms,
            "gates": gates, "pass": all(gates.values()), "dropped_subwindows": dropped_all,
            "slope_stderr_ppm": fit.slope_stderr_ppm,
            "slope": slope_verdict(fit.slope_ppm, fit.slope_stderr_ppm, AV_GATE_SLOPE_PPM)}
