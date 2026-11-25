"""DVD ↔ LD picture alignment on 48×36 luma descriptors (pure numpy; no I/O).

Port of v38 ``measure_dvd_alignment.py`` with three changes made for quality and automation
(owner decision 2026-11-21: automate DVD picture alignment as far as possible):

1. **An explicit "unmatched" state.** v38 started sampling 100 s in so the LD-only front matter
   could not drag the path, which left the episode's first 100 s, where ``video.delta_s`` and
   the head trim are anchored, outside the measured range. Here every master frame is sampled,
   and a constant-score *unmatched* state absorbs content with no DVD counterpart. The first
   matched sample is the measured DVD origin.
2. **Exact linear-time Viterbi.** The |Δoffset| transition penalty is solved with the
   distance-transform identity, max_j(prev_j − λ|i−j|), using prefix maxima instead of a
   states×states matrix. The result is identical to v38's for the same inputs, and the run
   time is linear in the number of states.
3. **An act model and automatic acceptance.** On real episodes the offset is not constant between
   act breaks: the two transfers run at slightly different speeds (S01E20: −990 ppm, i.e.
   1000:1001), so the integer path climbs in 1-frame steps, and telecine duplicates make
   neighbouring offsets tie. The path is therefore modelled as straight lines per act, fitted on
   sub-frame (parabolic) offsets, with the breaks between them refined by residuals. A path is
   accepted without a human only if the act lines explain the picture frame by frame (every
   textured master frame's best DVD match is within tolerance of the line's), the reverse
   (DVD→LD) path lands on the same lines, the slopes agree, and nothing on the LD between acts
   lacks a DVD match. Anything else is held for the alignment review, with the reasons listed.

The state convention is v38's: ``offset = DVD index − master index`` on the 30000/1001 grid.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

FPS = 30000.0 / 1001.0
W, H = 48, 36
FEATURES = W * H

#: Defaults. v38's search range, step and penalty, plus the unmatched-state constants
#: (provisional until calibrated on real episodes with tools/validate_v38_alignment.py).
DEFAULTS = {
    "min_offset": -3000, "max_offset": 500, "step": 15, "penalty": 0.006,
    "unmatched_score": 0.30, "unmatched_switch": 1.5, "flat_std": 2.0,
}

#: Act model: breaks are offset jumps of at least ``break_frames`` that persist for
#: ``break_window_s`` on both sides (robust medians after removing the common slope). Within an
#: act the offset is a continuous piecewise-linear curve with knots every ``knot_s`` (speed
#: wander), fitted robustly with a second-difference penalty of weight ``smoothness``.
MODEL = {"break_frames": 2.5, "break_window_s": 20.0, "knot_s": 30.0, "smoothness": 4.0,
         "consistency_radius": 8, "consistency_tolerance": 0.02,
         # An "act" at the very start or end whose samples scatter this far (RMS, frames) from its own line is not
         # DVD picture: it is LD-only material (warning cards, "Feature Presentation", logos) whose loose matches
         # landed on random DVD frames. S02E02 (Flex 2026-11-24): 4.0–40.0 s, RMS 52 frames, offsets +566 → +54,
         # against real acts at RMS 0.3–1.0. Only short edge acts qualify; anything longer goes to review.
         "edge_act_max_rms": 5.0, "edge_act_max_s": 180.0}

#: Programme opening (owner suggestion 2026-11-24): the DVD's first seconds of picture are searched for, as one
#: sequence, near the start of the LD. Where they are found clearly is the programme start; everything on the LD
#: before it is LD-only (warning cards, "Feature Presentation", logos — whatever they look like). A DVD that opens
#: with its own logo is handled by trying the next stretch of picture after a black gap.
OPENING = {"lengths": (12.0, 20.0, 30.0), "stride": 3, "search_s": 900.0, "dvd_search_s": 90.0, "tries": 4,
           "min_ncc": 0.80, "min_margin": 0.15, "exclude_frames": 45, "min_textured": 0.5,
           "sharp_shift": 15, "max_self_ncc": 0.95}
#: ``lengths``: the opening run is the shortest of these whose pictures change enough to pin it down — the run
#: compared with itself half a second (``sharp_shift`` frames) later scores at most ``max_self_ncc``. Measured on
#: 23 DS9 DVD openings: 22 pass at 12 s (worst 0.91); S01E04 opens on a near-still shot (0.99 at 12 s, 0.96 at 20 s,
#: 0.90 at 30 s) and needs 30 s. The runner-up is the best score outside the peak's own slope (the run sliding off
#: itself still matches well when its first shot is long) and at least ``exclude_frames`` away.

#: Automatic-acceptance thresholds. Calibrated on S01E20, whose v38 alignment the owner approved
#: visually: overall 0.993, worst act 0.99 after break refinement, longest inconsistent span 2.4 s.
ACCEPT = {
    "consistency_min": 0.98,           # textured frames the act lines explain (NCC within tolerance of best)
    "act_consistency_min": 0.95,       # per act of at least 20 s
    "max_inconsistent_span_s": 3.0,    # longer runs of frames the lines cannot explain
    "reverse_agreement_min": 0.95,     # reverse-path samples within ±1.5 frames of the act lines
    "slope_spread_ppm": 2500.0,        # acts ≥ 60 s vs the common slope
    "min_matched_fraction": 0.80,      # of the master's textured samples
    "max_interior_unmatched_s": 10.0,  # textured LD picture with no DVD match between acts
    "max_acts": 16,
}


def normalize_rows(values: np.ndarray) -> np.ndarray:
    """Centre and L2-normalise rows; zero-variance rows stay zero (v38 ``numeric.normalize_rows``)."""
    rows = values.astype(np.float32, copy=True)
    rows -= rows.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return rows / norms


def flat_rows(values: np.ndarray, threshold: float) -> np.ndarray:
    """Rows with almost no picture detail (black, fades, title cards on flat fields)."""
    return values.astype(np.float32).std(axis=1) < threshold


def emissions(master: np.ndarray, dvd_norm: np.ndarray, idxs: np.ndarray, offs: np.ndarray) -> np.ndarray:
    """NCC of each sampled master frame against every candidate DVD frame (−1 where out of range)."""
    out = np.full((len(idxs), len(offs)), -1.0, np.float32)
    mq = normalize_rows(np.asarray(master[idxs]))
    for row, i in enumerate(idxs):
        js = i + offs
        valid = (js >= 0) & (js < len(dvd_norm))
        out[row, valid] = dvd_norm[js[valid]] @ mq[row]
    return out


def _l1_max(prev: np.ndarray, lam: float) -> tuple[np.ndarray, np.ndarray]:
    """For every i: max_j (prev[j] − lam·|i−j|) and its argmax (ties → nearest-lower index)."""
    n = len(prev)
    k = np.arange(n, dtype=np.float64)
    p = prev.astype(np.float64)
    g = p + lam * k                                    # forward: j ≤ i
    cg = np.maximum.accumulate(g)
    fi = np.maximum.accumulate(np.where(g >= cg, np.arange(n), 0))
    fwd = cg - lam * k
    h = (p - lam * k)[::-1]                            # backward: j ≥ i
    ch = np.maximum.accumulate(h)
    bi_rev = np.maximum.accumulate(np.where(h >= ch, np.arange(n), 0))
    bwd = (ch + lam * k[::-1])[::-1]
    bi = (n - 1 - bi_rev)[::-1]
    use_b = bwd > fwd
    return np.where(use_b, bwd, fwd), np.where(use_b, bi, fi)


def viterbi(em: np.ndarray, *, penalty: float, unmatched_score: float | None,
            unmatched_switch: float) -> np.ndarray:
    """Best state per sample. States 0..S−1 are offsets; S is "unmatched" when enabled."""
    rows, states = em.shape
    use_null = unmatched_score is not None
    back = np.zeros((rows, states), np.int32)
    back_null = np.zeros(rows, np.int32)                # predecessor of the null state
    score = em[0].astype(np.float64).copy()
    null = float(unmatched_score) if use_null else -math.inf
    for r in range(1, rows):
        best, arg = _l1_max(score, penalty)
        if use_null:
            from_null = null - unmatched_switch
            take = from_null > best
            best = np.where(take, from_null, best)
            arg = np.where(take, states, arg)
            j = int(np.argmax(score))
            stay, leave = null, float(score[j]) - unmatched_switch
            back_null[r] = states if stay >= leave else j
            null = max(stay, leave) + float(unmatched_score)
        back[r] = arg
        score = best + em[r]
    path = np.empty(rows, np.int32)
    last = int(np.argmax(score))
    path[-1] = states if (use_null and null > score[last]) else last
    for r in range(rows - 1, 0, -1):
        cur = path[r]
        path[r - 1] = back_null[r] if cur == states else back[r, cur]
    return path


@dataclass
class Act:
    """One act: a continuous piecewise-linear offset curve (knots every ``knot_s``)."""
    start_frame: int          # first sampled master frame of the act
    end_frame: int            # last sampled master frame of the act
    knots: list[float]        # master frames
    values: list[float]       # offset (DVD − master frames) at each knot
    samples: int
    residual_rms: float
    consistency: float | None = None
    consistency_frames: int = 0
    inconsistent_spans: list[list[float]] = field(default_factory=list)   # [start_s, end_s]

    @property
    def seconds(self) -> float:
        return (self.end_frame - self.start_frame) / FPS

    @property
    def slope(self) -> float:
        """Mean d(offset)/d(master frame) across the act."""
        span = self.knots[-1] - self.knots[0]
        return (self.values[-1] - self.values[0]) / span if span > 0 else 0.0

    def offset(self, frame):
        return np.interp(frame, self.knots, self.values)


@dataclass
class AlignmentResult:
    idxs: np.ndarray
    offsets: np.ndarray                    # integer path per sample; sentinel where unmatched
    sub: np.ndarray                        # sub-frame offset per sample (nan where unknown)
    matched: np.ndarray                    # bool per sample
    ncc: np.ndarray
    margin: np.ndarray
    flat: np.ndarray
    params: dict[str, Any]
    acts: list[Act] = field(default_factory=list)
    breaks: list[dict[str, float]] = field(default_factory=list)
    common_slope: float | None = None
    consistency: float | None = None
    reverse_agreement: float | None = None
    decision: str = ""
    reasons: list[str] = field(default_factory=list)
    ld_only: list[dict[str, Any]] = field(default_factory=list)   # edge material set aside as having no DVD match
    opening: dict[str, Any] = field(default_factory=dict)         # where the DVD's opening pictures are on the LD

    @property
    def evidenced(self) -> np.ndarray:
        return (self.matched & ~self.flat & np.isfinite(self.sub)
                & (np.nan_to_num(self.ncc, nan=-1.0) >= self.params["unmatched_score"]))

    @property
    def origin_master_frame(self) -> int | None:
        if self.opening.get("found"):
            return int(self.opening["master_frame"])   # frame-exact, positively identified
        hit = np.flatnonzero(self.evidenced)
        return int(self.idxs[hit[0]]) if len(hit) else None

    def act_for(self, frame: float) -> Act | None:
        for a in self.acts:
            if a.start_frame <= frame <= a.end_frame:
                return a
        return None

    def summary(self) -> dict[str, Any]:
        acts = [asdict(a) | {"knots": [round(k, 1) for k in a.knots], "values": [round(v, 3) for v in a.values],
                             "seconds": round(a.seconds, 3), "slope_ppm": round(a.slope * 1e6, 1)}
                for a in self.acts]
        return {"params": self.params, "model": MODEL, "samples": len(self.idxs),
                "sampled_range": [int(self.idxs[0]), int(self.idxs[-1])],
                "matched_samples": int(self.matched.sum()), "evidenced_samples": int(self.evidenced.sum()),
                "flat_samples": int(self.flat.sum()), "origin_master_frame": self.origin_master_frame,
                "acts": acts, "breaks": self.breaks,
                "common_slope_ppm": None if self.common_slope is None else round(self.common_slope * 1e6, 1),
                "consistency": self.consistency, "reverse_agreement": self.reverse_agreement,
                "decision": self.decision, "reasons": self.reasons, "acceptance_thresholds": ACCEPT,
                "ld_only_edges": self.ld_only, "opening": self.opening}


def _subframe(em: np.ndarray, path: np.ndarray, offs: np.ndarray) -> np.ndarray:
    """Parabolic peak of the NCC around each matched state (±0.5 frame)."""
    states = len(offs)
    out = np.full(len(path), np.nan)
    for r, s in enumerate(path):
        if s >= states:
            continue
        if 0 < s < states - 1:
            a, b, c = float(em[r, s - 1]), float(em[r, s]), float(em[r, s + 1])
            den = a - 2 * b + c
            d = 0.5 * (a - c) / den if den < 0 else 0.0
            out[r] = offs[s] + float(np.clip(d, -0.5, 0.5))
        else:
            out[r] = float(offs[s])
    return out


def align(master: np.ndarray, dvd: np.ndarray, *, params: dict[str, Any] | None = None,
          progress=None) -> AlignmentResult:
    """Candidate path from master (LD) to DVD descriptors."""
    p = {**DEFAULTS, **(params or {})}
    if p["max_offset"] <= p["min_offset"] or p["step"] <= 0 or p["penalty"] < 0:
        raise ValueError("invalid offset range, step or penalty")
    for name, a in (("master", master), ("dvd", dvd)):
        if a.ndim != 2 or a.shape[1] != FEATURES or a.shape[0] < 1000:
            raise ValueError(f"invalid {name} descriptor shape {a.shape}; expected N×{FEATURES}, N ≥ 1000")
    offs = np.arange(p["min_offset"], p["max_offset"] + 1, dtype=np.int32)
    idxs = np.arange(0, len(master), p["step"], dtype=np.int32)
    dvd_norm = normalize_rows(np.asarray(dvd))
    em = np.empty((len(idxs), len(offs)), np.float32)
    chunk = 512
    for c in range(0, len(idxs), chunk):
        em[c:c + chunk] = emissions(master, dvd_norm, idxs[c:c + chunk], offs)
        if progress:
            progress(0.5 * min(1.0, (c + chunk) / len(idxs)), "matching descriptors")
    flat = flat_rows(np.asarray(master[idxs]), p["flat_std"])
    em[flat] = 0.0                                       # no evidence either way on flat frames
    path = viterbi(em, penalty=p["penalty"], unmatched_score=p["unmatched_score"],
                   unmatched_switch=p["unmatched_switch"])
    states = len(offs)
    matched = path < states
    st = np.minimum(path, states - 1)
    offsets = np.where(matched, offs[st], np.iinfo(np.int32).min)
    rows = np.arange(len(idxs))
    ncc = np.where(matched, em[rows, st], np.nan).astype(np.float32)
    margin = np.full(len(idxs), np.nan, np.float32)
    for r in np.flatnonzero(matched):
        s = int(path[r])
        alt = np.concatenate((em[r, :max(0, s - 2)], em[r, s + 3:]))
        if alt.size:
            margin[r] = ncc[r] - alt.max()
    sub = np.where(matched, _subframe(em, path, offs), np.nan)
    return AlignmentResult(idxs, offsets, sub, matched, ncc, margin, flat, p)


# ------------------------------------------------------------------------------ act model
def _robust_line(x: np.ndarray, y: np.ndarray, *, slope: float | None = None) -> tuple[float, float, np.ndarray]:
    """Huber-weighted least squares (optionally with a fixed slope) → slope, intercept, residuals."""
    w = np.ones_like(x)
    if len(x) == 1:
        sl = slope if slope is not None else 0.0
        return sl, float(y[0] - sl * x[0]), np.zeros(1)
    for _ in range(20):
        if slope is None:
            A = np.vstack([x, np.ones_like(x)]).T
            sl, ic = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0]
        else:
            sl, ic = slope, float(np.sum(w * (y - slope * x)) / np.sum(w))
        res = y - (sl * x + ic)
        scale = 1.4826 * float(np.median(np.abs(res))) + 1e-3
        w = 1.0 / np.maximum(1.0, np.abs(res) / (2 * scale))
    return float(sl), float(ic), res


def _common_slope(x: np.ndarray, y: np.ndarray, bounds: list[int]) -> float:
    """One slope shared by every act, each act with its own intercept (robust)."""
    n_acts = len(bounds) - 1
    A = np.zeros((len(x), n_acts + 1))
    A[:, 0] = x
    for i, (a, b) in enumerate(itertools.pairwise(bounds)):
        A[a:b, i + 1] = 1
    w = np.ones(len(x))
    sol = np.zeros(n_acts + 1)
    for _ in range(20):
        sol = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0]
        res = y - A @ sol
        scale = 1.4826 * float(np.median(np.abs(res))) + 1e-3
        w = 1.0 / np.maximum(1.0, np.abs(res) / (2 * scale))
    return float(sol[0])


def _find_breaks(x: np.ndarray, y: np.ndarray, slope: float) -> list[int]:
    """Indices k where the detrended offset jumps by ≥ break_frames, persisting on both sides."""
    win = MODEL["break_window_s"] * FPS
    yd = y - slope * x
    jump = np.zeros(len(x))
    for k in range(1, len(x)):
        before = yd[(x >= x[k] - win) & (x < x[k])]
        after = yd[(x >= x[k]) & (x < x[k] + win)]
        if len(before) >= 5 and len(after) >= 5:
            jump[k] = float(np.median(after) - np.median(before))
    out: list[int] = []
    for k in np.flatnonzero(np.abs(jump) >= MODEL["break_frames"]):
        if out and x[k] - x[out[-1]] < win:
            if abs(jump[k]) > abs(jump[out[-1]]):
                out[-1] = int(k)
        else:
            out.append(int(k))
    return out


def _refine(x: np.ndarray, y: np.ndarray, bounds: list[int]) -> list[int]:
    """Move each break to the split that best separates the two neighbouring act lines."""
    win = MODEL["break_window_s"] * FPS
    out = list(bounds)
    for i in range(1, len(out) - 1):
        a, k, b = out[i - 1], out[i], out[i + 1]
        lo = max(a + 1, int(np.searchsorted(x, x[k] - win)))
        hi = min(b - 1, int(np.searchsorted(x, x[k] + win)))
        if hi <= lo:
            continue
        s1, i1, _ = _robust_line(x[a:lo], y[a:lo]) if lo - a >= 2 else _robust_line(x[a:k], y[a:k])
        s2, i2, _ = _robust_line(x[hi:b], y[hi:b]) if b - hi >= 2 else _robust_line(x[k:b], y[k:b])
        r1 = np.abs(y[lo:hi] - (s1 * x[lo:hi] + i1))
        r2 = np.abs(y[lo:hi] - (s2 * x[lo:hi] + i2))
        cost = [np.sum(r1[:j]) + np.sum(r2[j:]) for j in range(hi - lo + 1)]
        out[i] = lo + int(np.argmin(cost))
    return out


def _robust_curve(x: np.ndarray, y: np.ndarray) -> tuple[list[float], list[float], np.ndarray]:
    """Continuous piecewise-linear fit (hat basis, Huber IRLS, second-difference penalty)."""
    k = MODEL["knot_s"] * FPS
    n = max(2, int(np.ceil((x[-1] - x[0]) / k)) + 1)
    knots = np.linspace(x[0], x[-1], n) if x[-1] > x[0] else np.array([x[0], x[0] + 1.0])
    A = np.zeros((len(x), n))
    pos = np.clip(np.searchsorted(knots, x, side="right") - 1, 0, n - 2)
    t = (x - knots[pos]) / (knots[pos + 1] - knots[pos])
    A[np.arange(len(x)), pos] = 1 - t
    A[np.arange(len(x)), pos + 1] = t
    D = np.zeros((max(n - 2, 0), n))
    for i in range(n - 2):
        D[i, i:i + 3] = (1.0, -2.0, 1.0)
    lam = MODEL["smoothness"]
    w = np.ones(len(x))
    vals = np.zeros(n)
    for _ in range(20):
        lhs = (A * w[:, None]).T @ A + lam * D.T @ D
        vals = np.linalg.solve(lhs + 1e-9 * np.eye(n), (A * w[:, None]).T @ y)
        res = y - A @ vals
        scale = 1.4826 * float(np.median(np.abs(res))) + 1e-3
        w = 1.0 / np.maximum(1.0, np.abs(res) / (2 * scale))
    return knots.tolist(), vals.tolist(), y - A @ vals


def fit_acts(res: AlignmentResult) -> None:
    """Fill ``res.acts``, ``res.breaks`` and ``res.common_slope`` from the evidenced samples."""
    e = np.flatnonzero(res.evidenced)
    res.acts, res.breaks = [], []
    if len(e) < 2:
        return
    x, y = res.idxs[e].astype(float), res.sub[e]
    bounds = [0, len(e)]
    slope = _common_slope(x, y, bounds)
    for _ in range(2):                                   # breaks need the slope; the slope needs the acts
        bounds = [0, *_find_breaks(x, y, slope), len(e)]
        slope = _common_slope(x, y, bounds)
    bounds = _refine(x, y, bounds)
    bounds = [b for i, b in enumerate(bounds) if i == 0 or b > bounds[i - 1]]
    res.common_slope = _common_slope(x, y, bounds)
    for a, b in itertools.pairwise(bounds):
        knots, vals, r = _robust_curve(x[a:b], y[a:b])
        res.acts.append(Act(int(x[a]), int(x[b - 1]), knots, vals, b - a, float(np.sqrt(np.mean(r ** 2)))))
    for prev, nxt in zip(res.acts[:-1], res.acts[1:], strict=True):
        mid = (prev.end_frame + nxt.start_frame) / 2
        jump = (nxt.values[0] + nxt.slope * (mid - nxt.knots[0])) - (prev.values[-1] + prev.slope * (mid - prev.knots[-1]))
        res.breaks.append({"after_frame": prev.end_frame, "before_frame": nxt.start_frame,
                           "after_s": round(prev.end_frame / FPS, 3), "before_s": round(nxt.start_frame / FPS, 3),
                           "jump_frames": round(float(jump), 2)})


def _run_score(mnorm: np.ndarray, dvd: np.ndarray, cols: np.ndarray, d0: int, length: int) -> np.ndarray:
    """Mean NCC of the DVD frames ``cols`` (a run starting at ``d0``) placed at every start position on ``mnorm``."""
    dnorm = normalize_rows(np.asarray(dvd[cols]))
    corr = mnorm @ dnorm.T
    npos = len(mnorm) - length
    score = np.zeros(npos, np.float64)
    for j, c in enumerate(cols - d0):
        score += corr[c:c + npos, j]
    return score / len(cols)


def opening_length(dvd: np.ndarray, d0: int, dflat: np.ndarray, o: dict[str, Any] = OPENING) -> tuple[int, float]:
    """Frames in the opening run starting at ``d0``, and its self-score half a second on (see ``OPENING``)."""
    shift = o["sharp_shift"]
    best: tuple[int, float] = (round(o["lengths"][0] * FPS), 1.0)
    for secs in o["lengths"]:
        length = round(secs * FPS)
        if d0 + length + shift > len(dvd):
            break
        cols = d0 + np.arange(0, length, o["stride"])
        cols = cols[~dflat[cols]]
        if len(cols) < 3:
            break
        a = normalize_rows(np.asarray(dvd[cols]))
        b = normalize_rows(np.asarray(dvd[cols + shift]))
        self_ncc = float((a * b).sum(axis=1).mean())
        best = (length, self_ncc)
        if self_ncc <= o["max_self_ncc"]:
            break
    return best


def outside_peak(score: np.ndarray, best: int, exclude: int) -> np.ndarray:
    lo = best
    while lo > 0 and score[lo - 1] <= score[lo]:
        lo -= 1
    hi = best
    while hi < len(score) - 1 and score[hi + 1] <= score[hi]:
        hi += 1
    lo, hi = min(lo, best - exclude), max(hi, best + exclude)
    return np.concatenate((score[:max(0, lo)], score[hi + 1:]))


def find_opening(master: np.ndarray, dvd: np.ndarray, *, flat_std: float = DEFAULTS["flat_std"],
                 search_s: float | None = None, seconds: float | None = None) -> dict[str, Any]:
    """Where the DVD's opening pictures are on the LD (see ``OPENING``): a whole run of textured DVD frames is slid
    along the start of the LD and must match there clearly, not just frame by frame somewhere. ``dvd`` may be just
    the DVD's first seconds (an opening pack, ``fstk.media.opening_pack``); ``seconds`` fixes the run's length
    (a pack records the length chosen when it was made)."""
    o = OPENING if search_s is None else {**OPENING, "search_s": search_s}
    longest = round((seconds or max(o["lengths"])) * FPS)
    dvd_lim = min(len(dvd), int(o["dvd_search_s"] * FPS) + longest + o["sharp_shift"])
    dflat = flat_rows(np.asarray(dvd[:dvd_lim]), flat_std)
    shortest = round((seconds or min(o["lengths"])) * FPS)
    span = min(len(master), int(o["search_s"] * FPS) + longest)
    if span <= shortest or dvd_lim <= shortest:
        return {"found": False, "why": "too short to search"}
    mnorm = normalize_rows(np.asarray(master[:span]))
    tried: list[dict[str, Any]] = []
    start = 0
    for _ in range(o["tries"]):
        textured = np.flatnonzero(~dflat[start:max(start, dvd_lim - shortest)])
        if not len(textured):
            break
        d0 = start + int(textured[0])
        if seconds is not None:
            length, self_ncc = round(seconds * FPS), None
        else:
            length, self_ncc = opening_length(dvd[:dvd_lim], d0, dflat, o)
        length = min(length, dvd_lim - d0)
        if span <= length:
            break
        cols = d0 + np.arange(0, length, o["stride"])
        keep = ~dflat[cols]
        if keep.mean() < o["min_textured"]:
            start = d0 + length
            continue
        cols = cols[keep]
        score = _run_score(mnorm, dvd, cols, d0, length)
        best = int(np.argmax(score))
        others = outside_peak(score, best, o["exclude_frames"])
        runner = float(others.max()) if others.size else -1.0
        attempt = {"dvd_frame": d0, "master_frame": best, "ncc": round(float(score[best]), 3),
                   "runner_up_ncc": round(runner, 3), "frames_compared": len(cols),
                   "seconds": round(length / FPS, 1)}
        if self_ncc is not None:
            attempt["self_ncc_half_s"] = round(self_ncc, 3)
        tried.append(attempt)
        if score[best] >= o["min_ncc"] and score[best] - runner >= o["min_margin"]:
            return {"found": True, **attempt, "offset": d0 - best, "tried": tried}
        # not found: maybe a DVD-only logo or card; try the next stretch of picture after a black gap
        gap = np.flatnonzero(dflat[d0:dvd_lim])
        if not len(gap):
            break
        start = d0 + int(gap[0])
    return {"found": False, "tried": tried,
            "why": "the DVD's opening pictures were not found clearly near the start of the LD"}


def apply_opening(res: AlignmentResult, opening: dict[str, Any]) -> None:
    """Everything on the LD before the identified opening is LD-only: its samples become unmatched."""
    res.opening = opening
    if not opening.get("found"):
        return
    cut = int(opening["master_frame"]) - res.params["step"] // 2
    sel = res.idxs < cut
    if (res.matched & sel).any():
        res.ld_only.append({"where": "start", "start_s": round(float(res.idxs[sel][0]) / FPS, 2),
                            "end_s": round(cut / FPS, 2), "samples": int((res.matched & sel).sum()),
                            "why": "before the DVD's opening pictures: LD-only material (warnings, titles, logos)"})
    res.matched[sel] = False
    res.offsets[sel] = np.iinfo(np.int32).min
    res.sub[sel] = np.nan
    res.ncc[sel] = np.nan
    fit_acts(res)


def drop_unmatched_edges(res: AlignmentResult) -> None:
    """Set aside a first or last act that is not DVD picture (see ``MODEL["edge_act_max_rms"]``): its samples
    become unmatched, the acts are fitted again, and the programme origin moves to the first real act."""
    sentinel = np.iinfo(np.int32).min
    while res.acts:
        hit = next(((a, w) for a, w in ((res.acts[0], "start"), (res.acts[-1], "end"))
                    if a.residual_rms > MODEL["edge_act_max_rms"] and a.seconds <= MODEL["edge_act_max_s"]), None)
        if hit is None:
            return
        act, where = hit
        sel = (res.idxs >= act.start_frame) & (res.idxs <= act.end_frame)
        res.matched[sel] = False
        res.offsets[sel] = sentinel
        res.sub[sel] = np.nan
        res.ncc[sel] = np.nan
        res.ld_only.append({"where": where, "start_s": round(act.start_frame / FPS, 2),
                            "end_s": round(act.end_frame / FPS, 2), "residual_rms_frames": round(act.residual_rms, 1),
                            "samples": int(sel.sum()),
                            "why": "its matches scatter across the DVD instead of following one line: LD-only "
                                   "material (warnings, titles, logos), not programme"})
        fit_acts(res)


def consistency_check(master: np.ndarray, dvd: np.ndarray, res: AlignmentResult) -> None:
    """Every textured master frame of every act: does the act line pick (±1) its best DVD match?

    "Best" is searched ±``consistency_radius`` frames around the line; a frame is consistent
    when the line's NCC is within ``consistency_tolerance`` of that best. Static shots and
    telecine duplicates tie, which is exactly what the tolerance allows.
    """
    dvd_norm = normalize_rows(np.asarray(dvd))
    R, tol, thr = MODEL["consistency_radius"], MODEL["consistency_tolerance"], res.params["flat_std"]
    total = good = 0
    for act in res.acts:
        frames = np.arange(act.start_frame, act.end_frame + 1, dtype=np.int64)
        raw = np.asarray(master[frames]).astype(np.float32)
        tex = raw.std(axis=1) >= thr
        frames = frames[tex]
        if not len(frames):
            continue
        q = normalize_rows(raw[tex])
        base = np.round(act.offset(frames)).astype(np.int64)
        score = np.full((len(frames), 2 * R + 1), -2.0, np.float32)
        for d in range(-R, R + 1):
            js = frames + base + d
            ok = (js >= 0) & (js < len(dvd_norm))
            score[ok, d + R] = np.einsum("ij,ij->i", q[ok], dvd_norm[js[ok]])
        cons = score[:, R - 1:R + 2].max(axis=1) >= score.max(axis=1) - tol
        act.consistency, act.consistency_frames = float(cons.mean()), len(frames)
        bad = frames[~cons]
        spans: list[list[float]] = []
        if len(bad):
            start = prev = int(bad[0])
            for f in [*bad[1:].tolist(), None]:
                if f is None or f - prev > 15:
                    spans.append([round(start / FPS, 2), round(prev / FPS, 2)])
                    if f is not None:
                        start = f
                if f is not None:
                    prev = f
        act.inconsistent_spans = [s for s in spans if s[1] - s[0] >= 0.5]
        total += len(frames)
        good += int(cons.sum())
    res.consistency = good / total if total else None


def reverse_agreement(forward: AlignmentResult, reverse: AlignmentResult) -> float | None:
    """Fraction of textured reverse-path samples that land within ±1.5 frames of a forward act line.

    ``reverse`` aligns the DVD (as "master") to the LD, so its offsets are master − DVD.
    """
    total = agree = 0
    ev = reverse.evidenced
    for j, off, ok in zip(reverse.idxs, reverse.offsets, ev, strict=True):
        if not ok:
            continue
        total += 1
        i = int(j) + int(off)
        act = forward.act_for(i)
        if act is not None and abs(act.offset(i) + int(off)) <= 1.5:
            agree += 1
    return agree / total if total else None


def decide(res: AlignmentResult) -> None:
    """Set ``res.decision`` to AUTO-ACCEPTED or NEEDS-REVIEW, listing every failed check."""
    p, reasons = res.params, []
    acts = res.acts
    if not acts:
        reasons.append("no DVD picture matched")
    op = res.opening
    if not op.get("found"):
        reasons.append(op.get("why") or "the DVD's opening pictures were not looked for")
    elif acts:
        first = acts[0]
        line = first.offset(float(op["master_frame"])) if first.start_frame <= op["master_frame"] + p["step"] else None
        if line is None or abs(line - op["offset"]) > 2.0:
            reasons.append(f"the DVD's opening is at LD {op['master_frame'] / FPS:.1f}s, but the first act does not "
                           "start there with the same offset")
    ev = res.evidenced
    edge = ev & ((res.offsets == p["min_offset"]) | (res.offsets == p["max_offset"]))
    if edge.any():
        reasons.append(f"{int(edge.sum())} sample(s) sit on the offset search edge")
    if len(acts) > ACCEPT["max_acts"]:
        reasons.append(f"{len(acts)} acts (> {ACCEPT['max_acts']})")
    if res.consistency is not None and res.consistency < ACCEPT["consistency_min"]:
        reasons.append(f"act lines explain only {res.consistency:.1%} of textured frames")
    for a in acts:
        where = f"act {a.start_frame / FPS:.1f}–{a.end_frame / FPS:.1f}s"
        if a.seconds >= 20 and a.consistency is not None and a.consistency < ACCEPT["act_consistency_min"]:
            reasons.append(f"{where}: line explains only {a.consistency:.1%} of frames")
        for s0, s1 in a.inconsistent_spans:
            if s1 - s0 >= ACCEPT["max_inconsistent_span_s"]:
                reasons.append(f"{where}: picture at {s0:.1f}–{s1:.1f}s does not follow the act line")
        if res.common_slope is not None and a.seconds >= 60 and \
                abs(a.slope - res.common_slope) * 1e6 > ACCEPT["slope_spread_ppm"]:
            reasons.append(f"{where}: slope {a.slope * 1e6:+.0f} ppm vs common {res.common_slope * 1e6:+.0f} ppm")
    if acts:
        lo, hi = acts[0].start_frame, acts[-1].end_frame
        unmatched = ~res.matched & ~res.flat & (res.idxs > lo) & (res.idxs < hi)
        run = longest = 0
        for u in unmatched:
            run = run + 1 if u else 0
            longest = max(longest, run)
        if longest * p["step"] / FPS > ACCEPT["max_interior_unmatched_s"]:
            reasons.append(f"{longest * p['step'] / FPS:.1f}s of LD picture between acts has no DVD match "
                           "(edition difference?)")
    textured = ~res.flat
    if textured.any():
        frac = float((res.matched & textured).sum() / textured.sum())
        if frac < ACCEPT["min_matched_fraction"]:
            reasons.append(f"only {frac:.0%} of textured master samples matched the DVD")
    if res.reverse_agreement is None:
        reasons.append("reverse alignment found nothing to compare")
    elif res.reverse_agreement < ACCEPT["reverse_agreement_min"]:
        reasons.append(f"reverse alignment lands on the act lines for only {res.reverse_agreement:.1%} of samples")
    if len(reasons) > 12:
        reasons = [*reasons[:12], f"... and {len(reasons) - 12} more"]
    res.reasons = reasons
    res.decision = "AUTO-ACCEPTED" if not reasons else "NEEDS-REVIEW"


def measure(master: np.ndarray, dvd: np.ndarray, *, params: dict[str, Any] | None = None,
            progress=None) -> AlignmentResult:
    """Forward path → act model → frame-by-frame consistency → reverse check → decision."""
    fwd = align(master, dvd, params=params, progress=progress)
    fit_acts(fwd)
    if progress:
        progress(0.55, "finding the DVD's opening pictures on the LD")
    apply_opening(fwd, find_opening(master, dvd, flat_std=fwd.params["flat_std"]))
    drop_unmatched_edges(fwd)
    if progress:
        progress(0.6, "checking every frame against the act lines")
    consistency_check(master, dvd, fwd)
    rp = {**fwd.params, "min_offset": -fwd.params["max_offset"], "max_offset": -fwd.params["min_offset"]}
    if progress:
        progress(0.75, "reverse alignment")
    rev = align(dvd, master, params=rp)
    fwd.reverse_agreement = reverse_agreement(fwd, rev)
    decide(fwd)
    return fwd


def path_array(res: AlignmentResult) -> np.ndarray:
    """v38 ``align_npy`` layout: rows (master_frame, dvd_minus_master), from the act lines.

    One row per sampled master frame inside an act, with the act line rounded to a whole frame.
    Gaps between acts and unmatched material are left out, so nothing interpolates across an
    unmeasured break.
    """
    rows = []
    for a in res.acts:
        sel = res.idxs[(res.idxs >= a.start_frame) & (res.idxs <= a.end_frame)]
        rows.append(np.column_stack((sel, np.round(a.offset(sel.astype(float))))))
    if not rows:
        return np.zeros((0, 2), np.int64)
    return np.vstack(rows).astype(np.int64)


def offset_at(path: np.ndarray, master_frame: int) -> int:
    """Offset in force at ``master_frame`` (nearest sampled point at or before it; else the first)."""
    k = int(np.searchsorted(path[:, 0], master_frame, side="right")) - 1
    return int(path[max(k, 0), 1])
