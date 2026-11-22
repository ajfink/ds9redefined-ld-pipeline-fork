"""Audio quality measurements and the two small repairs (pure numpy).

Owner rules (2026-11-22): the delivered audio stays as captured. The only change this module can make is
filling a *short* digital gap or a single-sample-wide click by linear-prediction interpolation from the
signal on both sides; anything longer is reported, never invented. Dynamic range is never reduced —
``dynamic_range_preserved`` is the guard any future noise-reduction step must pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

CLICK_SIGMA = 12.0          # second-difference spike vs the local robust noise floor
CLICK_MAX_MS = 1.5          # wider disturbances are music/effects, not clicks
GAP_MIN_MS = 1.0            # a run of exact digital zeros this long mid-programme is a gap
GAP_REPAIR_MAX_MS = 10.0    # longer gaps are flagged only
LPC_ORDER = 32


@dataclass(frozen=True)
class Event:
    channel: int
    start: int          # sample index
    length: int         # samples
    kind: str           # "click" | "gap"
    amount: float       # click: peak/floor ratio; gap: length in ms

    def seconds(self, rate: int) -> float:
        return self.start / rate


# ------------------------------------------------------------------------------ detection
def find_clicks(x: np.ndarray, rate: int, channel: int = 0) -> list[Event]:
    """Impulsive disturbances: spikes of the second difference far above its running median-abs level."""
    x = x.astype(np.float64)
    d2 = np.abs(np.diff(x, 2))
    block = max(rate // 20, 64)
    n = (len(d2) // block) * block
    if n == 0:
        return []
    floor = np.median(d2[:n].reshape(-1, block), axis=1)
    floor = np.maximum(np.repeat(floor, block), 1e-9)
    floor = np.pad(floor, (0, len(d2) - n), mode="edge")
    thr = CLICK_SIGMA * np.maximum(floor, 1e-5 + 0.0)
    hot = d2 > thr
    out, i, limit = [], 0, max(int(rate * CLICK_MAX_MS / 1000), 2)
    idx = np.flatnonzero(hot)
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and idx[j + 1] - idx[j] <= 2:
            j += 1
        start, length = int(idx[i]), int(idx[j] - idx[i] + 3)
        if length <= limit:
            out.append(Event(channel, start, length, "click", float(d2[idx[i]:idx[j] + 1].max() / floor[idx[i]])))
        i = j + 1
    return out


def find_gaps(x: np.ndarray, rate: int, channel: int = 0, *, level_db: float = -70.0) -> list[Event]:
    """Runs of exact zeros (>= GAP_MIN_MS) that have real signal on both sides: dropped samples, not silence."""
    min_len = max(int(rate * GAP_MIN_MS / 1000), 2)
    zero = np.concatenate([[False], x == 0, [False]])
    edges = np.flatnonzero(np.diff(zero.astype(np.int8)))
    out = []
    thr = 10 ** (level_db / 20)
    for a, b in zip(edges[::2], edges[1::2], strict=False):
        if b - a < min_len:
            continue
        pre, post = x[max(0, a - 480):a], x[b:b + 480]
        if len(pre) < 32 or len(post) < 32:
            continue
        if np.sqrt((pre.astype(np.float64) ** 2).mean()) > thr and np.sqrt((post.astype(np.float64) ** 2).mean()) > thr:
            out.append(Event(channel, int(a), int(b - a), "gap", (b - a) * 1000.0 / rate))
    return out


def find_events(x: np.ndarray, rate: int, channel: int = 0) -> list[Event]:
    """Gaps plus clicks, where the step at a gap's own edges is not counted as a separate click."""
    gaps = find_gaps(x, rate, channel)
    clicks = [c for c in find_clicks(x, rate, channel)
              if not any(g.start - 4 <= c.start <= g.start + g.length + 4 for g in gaps)]
    return sorted(clicks + gaps, key=lambda e: e.start)


# ------------------------------------------------------------------------------ repair
def _lpc(seg: np.ndarray, order: int) -> np.ndarray:
    """Least-squares (covariance-method) predictor: stays accurate on tonal material where the
    autocorrelation method shrinks and then diverges."""
    n = len(seg)
    a = np.array([seg[order - k - 1:n - k - 1] for k in range(order)]).T
    coef, *_ = np.linalg.lstsq(a, seg[order:], rcond=None)
    return coef


def _predict(history: np.ndarray, coef: np.ndarray, n: int) -> np.ndarray:
    buf = list(history[-len(coef):])
    out = []
    for _ in range(n):
        v = float(np.dot(coef, buf[::-1][:len(coef)]))
        out.append(v)
        buf.append(v)
    return np.asarray(out)


def lpc_fill(x: np.ndarray, start: int, length: int, order: int = LPC_ORDER) -> np.ndarray:
    """New samples for x[start:start+length]: forward prediction from the left, backward from the right,
    raised-cosine crossfaded. Needs ``order*8`` samples of context on both sides (else None-equivalent: raises)."""
    ctx = order * 16
    if start < ctx or start + length + ctx > len(x):
        raise ValueError("not enough context around the gap")
    left, right = x[start - ctx:start].astype(np.float64), x[start + length:start + length + ctx][::-1].astype(np.float64)
    fwd = _predict(left, _lpc(left, order), length)
    bwd = _predict(right, _lpc(right, order), length)[::-1]
    w = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, length))
    return fwd * (1 - w) + bwd * w


def repair(x: np.ndarray, events: list[Event], rate: int, *, max_gap_ms: float = GAP_REPAIR_MAX_MS):
    """Returns (repaired copy, repaired events, left-alone events with reasons). Never touches anything else."""
    y = x.copy()
    done, left = [], []
    for e in events:
        if e.kind == "gap" and e.amount > max_gap_ms:
            left.append((e, f"gap {e.amount:.1f} ms is longer than {max_gap_ms:g} ms: flagged, not invented"))
            continue
        try:
            fill = lpc_fill(y, e.start, e.length)
            if e.kind == "click":
                ok, nrmse = _continues(y, e, fill)
                if not ok:
                    left.append((e, f"looks like programme material (signal does not continue the prediction, "
                                    f"nrmse {nrmse:.2f}): left as captured"))
                    continue
            y[e.start:e.start + e.length] = fill.astype(y.dtype)
            done.append(e)
        except ValueError as exc:
            left.append((e, str(exc)))
    return y, done, left


def _continues(x: np.ndarray, e: Event, fill: np.ndarray, *, look: int = 48, limit: float = 0.35) -> tuple[bool, float]:
    """A real click is a blip on a signal that carries on as predicted; a drum hit or consonant is not.

    Predict ``look`` samples past the event from the filled signal and compare with what was captured."""
    order = LPC_ORDER
    hist = np.concatenate([x[e.start - order * 16:e.start].astype(np.float64), fill])
    ahead = _predict(hist, _lpc(hist[-order * 16:], order), look)
    actual = x[e.start + e.length:e.start + e.length + look].astype(np.float64)
    if len(actual) < look:
        return False, 1.0
    scale = max(float(np.sqrt((actual ** 2).mean())), 1e-6)
    nrmse = float(np.sqrt(((ahead - actual) ** 2).mean()) / scale)
    return nrmse <= limit, nrmse


# ------------------------------------------------------------------------------ channel / phase
def channel_findings(ch: np.ndarray, labels: list[str]) -> list[dict]:
    """``ch``: (samples, channels). Silent/dead channel, polarity, near-mono and swap hints (stereo)."""
    rows = []
    rms = np.sqrt((ch.astype(np.float64) ** 2).mean(axis=0))
    db = 20 * np.log10(np.maximum(rms, 1e-9))
    for i, lab in enumerate(labels):
        rows.append({"check": f"{lab} level", "value_dbfs": round(float(db[i]), 1),
                     "status": "FLAG" if db[i] < -80 and lab != "LFE" else "PASS"})
    if ch.shape[1] >= 2:
        a, b = ch[:, 0].astype(np.float64), ch[:, 1].astype(np.float64)
        denom = max(float(np.sqrt((a * a).mean() * (b * b).mean())), 1e-12)
        corr = float((a * b).mean() / denom)
        rows.append({"check": f"{labels[0]}/{labels[1]} correlation", "value": round(corr, 3),
                     "status": "FLAG" if corr < -0.3 or corr > 0.999 else "PASS",
                     "note": "inverted polarity" if corr < -0.3 else ("identical channels (mono?)" if corr > 0.999 else "")})
    return rows


def swap_hint(ld: np.ndarray, ref: np.ndarray) -> dict:
    """LD stereo vs the DVD front pair (same length, mono-summed per side): swapped when cross > straight."""
    def c(a, b):
        a, b = a.astype(np.float64), b.astype(np.float64)
        return float((a * b).mean() / max(np.sqrt((a * a).mean() * (b * b).mean()), 1e-12))
    straight = (c(ld[:, 0], ref[:, 0]) + c(ld[:, 1], ref[:, 1])) / 2
    cross = (c(ld[:, 0], ref[:, 1]) + c(ld[:, 1], ref[:, 0])) / 2
    return {"straight": round(straight, 3), "cross": round(cross, 3), "status": "FLAG" if cross > straight + 0.1 else "PASS"}


def lfe_findings(lfe: np.ndarray, rate: int, *, cutoff_hz: float = 200.0) -> dict:
    spec = np.abs(np.fft.rfft(lfe[: rate * 30].astype(np.float64))) ** 2
    freqs = np.fft.rfftfreq(min(len(lfe), rate * 30), 1 / rate)
    total = spec.sum() or 1e-12
    above = float(spec[freqs > cutoff_hz].sum() / total)
    return {"check": "LFE band-limited", "fraction_above_200hz": round(above, 4), "status": "PASS" if above < 0.2 else "FLAG"}


# ------------------------------------------------------------------------------ loudness and range guard
_LUFS = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_LRA = re.compile(r"^\s*LRA:\s*([\d.]+)\s*LU", re.M)
_TP = re.compile(r"^\s*Peak:\s*(-?[\d.]+)\s*dBFS", re.M)


def parse_ebur128(text: str) -> dict[str, float] | None:
    """Final summary block of ``ffmpeg -af ebur128=peak=true``."""
    tail = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    i, lra, tp = _LUFS.search(tail), _LRA.search(tail), _TP.search(tail)
    if not (i and lra and tp):
        return None
    return {"integrated_lufs": float(i.group(1)), "lra_lu": float(lra.group(1)), "true_peak_dbfs": float(tp.group(1))}


def dynamic_range_preserved(before: dict[str, float], after: dict[str, float], *, tol_lu: float = 0.5) -> list[dict]:
    """A noise-reduction (or any) step must not shrink loudness range or peak-to-loudness ratio beyond ``tol_lu``."""
    plr_b = before["true_peak_dbfs"] - before["integrated_lufs"]
    plr_a = after["true_peak_dbfs"] - after["integrated_lufs"]
    return [{"check": "loudness range", "before": before["lra_lu"], "after": after["lra_lu"],
             "status": "PASS" if after["lra_lu"] >= before["lra_lu"] - tol_lu else "FAIL"},
            {"check": "peak-to-loudness ratio", "before": round(plr_b, 2), "after": round(plr_a, 2),
             "status": "PASS" if plr_a >= plr_b - tol_lu else "FAIL"}]


# ------------------------------------------------------------------------------ subtitles vs speech
def speech_envelope(mono: np.ndarray, rate: int, hop_s: float = 0.05) -> np.ndarray:
    """Speech-band (300-3400 Hz) energy per hop, in dB."""
    n = len(mono)
    spec = np.fft.rfft(mono.astype(np.float64))
    f = np.fft.rfftfreq(n, 1 / rate)
    spec[(f < 300) | (f > 3400)] = 0
    band = np.fft.irfft(spec, n)
    hop = int(rate * hop_s)
    m = (n // hop) * hop
    return 10 * np.log10(np.maximum((band[:m].reshape(-1, hop) ** 2).mean(axis=1), 1e-12))


def subtitle_offset(cues: list[tuple[float, float]], env: np.ndarray, hop_s: float = 0.05,
                    search_s: float = 2.0, step_s: float = 0.05) -> dict:
    """Best global shift of the cues against the speech envelope (inside-cue level minus outside-cue level)."""
    best = None
    shifts = np.arange(-search_s, search_s + 1e-9, step_s)
    scores = []
    for sh in shifts:
        inside, outside = [], []
        for a, b in cues:
            i0, i1 = int((a + sh) / hop_s), int((b + sh) / hop_s)
            if i0 < 0 or i1 >= len(env) or i1 <= i0:
                continue
            inside.append(env[i0:i1].mean())
            o0, o1 = max(0, i0 - int(1.0 / hop_s)), i0
            if o1 > o0:
                outside.append(env[o0:o1].mean())
        scores.append(np.mean(inside) - np.mean(outside) if inside and outside else -1e9)
    k = int(np.argmax(scores))
    best, zero = float(shifts[k]), scores[int(np.argmin(np.abs(shifts)))]
    return {"best_shift_s": round(best, 3), "score_db_best": round(float(scores[k]), 2), "score_db_zero": round(float(zero), 2),
            "cues_used": len(cues), "status": "PASS" if abs(best) <= 0.25 else "FLAG"}
