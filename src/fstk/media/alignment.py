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
3. **Automatic acceptance.** A path is accepted without a human only if every declared check
   passes:
   - no offset sits on a search edge;
   - the reverse (DVD→LD) alignment agrees within ±1 frame;
   - a dense frame-by-frame check confirms each segment's offset;
   - the segment count and lengths are plausible.
   Anything else is held for the alignment review with the reasons listed.

The state convention is v38's: ``offset = DVD index − master index`` on the 30000/1001 grid.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

FPS = 30000.0 / 1001.0
W, H = 48, 36
FEATURES = W * H
UNMATCHED = None  # offset value used for the unmatched state in paths/segments

#: Defaults. v38's search range, step and penalty, plus the unmatched-state constants
#: (provisional until calibrated on real episodes with tools/validate_v38_alignment.py).
DEFAULTS = {
    "min_offset": -3000, "max_offset": 500, "step": 15, "penalty": 0.006,
    "unmatched_score": 0.30, "unmatched_switch": 1.5, "flat_std": 2.0,
}

#: Automatic-acceptance thresholds (provisional; recorded with every decision).
ACCEPT = {
    "reverse_agreement_min": 0.95,     # fraction of matched samples agreeing within ±1 frame
    "dense_agreement_min": 0.90,       # per segment, textured frames whose best offset is within ±1
    "min_segment_s": 4.0,              # shorter matched segments are suspicious
    "max_segments": 16,
    "min_matched_fraction": 0.80,      # of the master's textured samples
    "max_interior_unmatched_s": 10.0,  # textured LD material with no DVD match between matched segments
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
class Segment:
    start_frame: int          # first sampled master frame of the run
    end_frame: int            # last sampled master frame of the run
    offset: int | None        # DVD − master frames; None = unmatched
    samples: int
    median_ncc: float | None
    dense_exact: float | None = None      # textured frames whose best offset == offset
    dense_within1: float | None = None    # ... within ±1
    dense_frames: int = 0
    kind: str = "matched"                 # matched | gap (no evidence between offsets) | unmatched

    @property
    def seconds(self) -> float:
        return (self.end_frame - self.start_frame) / FPS


@dataclass
class AlignmentResult:
    idxs: np.ndarray
    offsets: np.ndarray                    # per sample; out-of-range sentinel for unmatched
    matched: np.ndarray                    # bool per sample
    ncc: np.ndarray
    margin: np.ndarray
    flat: np.ndarray
    params: dict[str, Any]
    segments: list[Segment] = field(default_factory=list)
    reverse_agreement: float | None = None
    decision: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def origin_master_frame(self) -> int | None:
        hit = np.flatnonzero(self.matched & ~self.flat)
        return int(self.idxs[hit[0]]) if len(hit) else None

    def summary(self) -> dict[str, Any]:
        seg = [asdict(s) | {"seconds": round(s.seconds, 3)} for s in self.segments]
        matched = [s for s in self.segments if s.offset is not None]
        return {"params": self.params, "samples": len(self.idxs),
                "sampled_range": [int(self.idxs[0]), int(self.idxs[-1])],
                "matched_samples": int(self.matched.sum()), "flat_samples": int(self.flat.sum()),
                "origin_master_frame": self.origin_master_frame,
                "offset_range": ([min(s.offset for s in matched), max(s.offset for s in matched)]
                                 if matched else None),
                "segments": seg, "reverse_agreement": self.reverse_agreement,
                "decision": self.decision, "reasons": self.reasons, "acceptance_thresholds": ACCEPT}


def _segments(idxs, offsets, matched, ncc, flat, min_ncc) -> list[Segment]:
    """Runs of *evidenced* samples (matched, textured, NCC ≥ ``min_ncc``) with one offset.

    Samples without evidence (flat frames, low NCC) never start a segment of their own: inside
    a run of one offset they are bridged, between runs of different offsets they become a
    ``gap`` (offset None) whose exact edges the retime stage measures later. Samples the path
    put in the unmatched state form ``unmatched`` runs.
    """
    evid = matched & ~flat & (np.nan_to_num(ncc, nan=-1.0) >= min_ncc)
    out: list[Segment] = []
    r, n = 0, len(idxs)
    while r < n:
        if evid[r]:
            off, start, last = int(offsets[r]), r, r
            k = r + 1
            while k < n:
                if evid[k]:
                    if int(offsets[k]) != off:
                        break
                    last = k
                elif not matched[k]:
                    break
                k += 1
            sel = np.arange(start, last + 1)
            vals = ncc[sel][evid[sel]]
            out.append(Segment(int(idxs[start]), int(idxs[last]), off, int(evid[sel].sum()),
                               float(np.median(vals))))
            r = last + 1
        else:
            start = r
            kind_unmatched = not matched[r]
            while r < n and not evid[r] and (not matched[r]) == kind_unmatched:
                r += 1
            seg = Segment(int(idxs[start]), int(idxs[r - 1]), None, r - start, None)
            seg.kind = "unmatched" if kind_unmatched else "gap"
            out.append(seg)
    # merge consecutive no-offset runs
    merged: list[Segment] = []
    for seg in out:
        if merged and seg.offset is None and merged[-1].offset is None:
            prev = merged[-1]
            prev.end_frame, prev.samples = seg.end_frame, prev.samples + seg.samples
            prev.kind = "unmatched" if "unmatched" in (prev.kind, seg.kind) else "gap"
        else:
            merged.append(seg)
    return merged


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
            progress(0.6 * min(1.0, (c + chunk) / len(idxs)), "matching descriptors")
    flat = flat_rows(np.asarray(master[idxs]), p["flat_std"])
    em[flat] = 0.0                                       # no evidence either way on flat frames
    path = viterbi(em, penalty=p["penalty"], unmatched_score=p["unmatched_score"],
                   unmatched_switch=p["unmatched_switch"])
    states = len(offs)
    matched = path < states
    offsets = np.where(matched, offs[np.minimum(path, states - 1)], np.iinfo(np.int32).min)
    rows = np.arange(len(idxs))
    ncc = np.where(matched, em[rows, np.minimum(path, states - 1)], np.nan).astype(np.float32)
    margin = np.full(len(idxs), np.nan, np.float32)
    for r in np.flatnonzero(matched):
        s = int(path[r])
        alt = np.concatenate((em[r, :max(0, s - 2)], em[r, s + 3:]))
        if alt.size:
            margin[r] = ncc[r] - alt.max()
    res = AlignmentResult(idxs, offsets, matched, ncc, margin, flat, p)
    res.segments = _segments(idxs, offsets, matched, ncc, flat, p["unmatched_score"])
    return res


def dense_check(master: np.ndarray, dvd: np.ndarray, res: AlignmentResult, *, radius: int = 2) -> None:
    """Every textured master frame of each matched segment: is its best DVD offset the segment's?"""
    dvd_norm = normalize_rows(np.asarray(dvd))
    thr = res.params["flat_std"]
    for seg in res.segments:
        if seg.offset is None:
            continue
        frames = np.arange(seg.start_frame, seg.end_frame + 1, dtype=np.int64)
        frames = frames[~flat_rows(np.asarray(master[frames]), thr)]
        if not len(frames):
            continue
        q = normalize_rows(np.asarray(master[frames]))
        deltas = np.arange(-radius, radius + 1)
        best = np.full(len(frames), -np.inf)
        arg = np.zeros(len(frames), np.int64)
        for d in deltas:
            js = frames + seg.offset + d
            ok = (js >= 0) & (js < len(dvd_norm))
            s = np.full(len(frames), -np.inf)
            s[ok] = np.einsum("ij,ij->i", q[ok], dvd_norm[js[ok]])
            upd = s > best
            best[upd], arg[upd] = s[upd], d
        seg.dense_frames = len(frames)
        seg.dense_exact = float(np.mean(arg == 0))
        seg.dense_within1 = float(np.mean(np.abs(arg) <= 1))


def reverse_agreement(forward: AlignmentResult, reverse: AlignmentResult) -> float | None:
    """Fraction of textured reverse-matched samples that the forward path confirms within ±1 frame.

    ``reverse`` aligns the DVD (as "master") to the LD, so its offsets are master − DVD. A sample
    maps to master frame i; the forward offsets of the samples either side of i (matched ones
    only) must include −offset ± 1. Samples outside the forward path's matched range count as
    disagreements.
    """
    total = agree = 0
    n = len(forward.idxs)
    for j, off, m, flat in zip(reverse.idxs, reverse.offsets, reverse.matched, reverse.flat, strict=True):
        if not m or flat:
            continue
        total += 1
        i = int(j) + int(off)
        k = int(np.searchsorted(forward.idxs, i, side="right")) - 1
        near = [x for x in (k, k + 1) if 0 <= x < n and forward.matched[x]]
        if any(abs(int(forward.offsets[x]) + int(off)) <= 1 for x in near):
            agree += 1
    return agree / total if total else None


def decide(res: AlignmentResult) -> None:
    """Set ``res.decision`` to AUTO-ACCEPTED or NEEDS-REVIEW, listing every failed check."""
    p, reasons = res.params, []
    matched = [s for s in res.segments if s.offset is not None]
    if not matched:
        reasons.append("no matched segment")
    for s in matched:
        if s.offset in (p["min_offset"], p["max_offset"]):
            reasons.append(f"segment at {s.start_frame / FPS:.1f}s sits on the offset search edge ({s.offset})")
        k = matched.index(s)
        neighbours = [x.offset for x in matched[max(0, k - 1):k + 2] if x is not s]
        jitter = any(abs(s.offset - o) <= 1 for o in neighbours)   # telecine phase, not a break
        if (s.seconds < ACCEPT["min_segment_s"] and not jitter
                and s is not matched[0] and s is not matched[-1]):
            reasons.append(f"short matched segment {s.start_frame / FPS:.1f}–{s.end_frame / FPS:.1f}s "
                           f"(offset {s.offset:+d})")
        if s.dense_within1 is not None and s.dense_within1 < ACCEPT["dense_agreement_min"]:
            reasons.append(f"segment {s.start_frame / FPS:.1f}–{s.end_frame / FPS:.1f}s: only "
                           f"{s.dense_within1:.0%} of frames confirm offset {s.offset:+d} within ±1")
    if matched:
        lo, hi = matched[0].start_frame, matched[-1].end_frame
        for s in res.segments:
            if s.offset is None and lo < s.start_frame and s.end_frame < hi:
                inside = (res.idxs >= s.start_frame) & (res.idxs <= s.end_frame) & ~res.flat
                textured_s = float(inside.sum() * p["step"] / FPS)
                if textured_s > ACCEPT["max_interior_unmatched_s"]:
                    reasons.append(f"{textured_s:.1f}s of LD picture at {s.start_frame / FPS:.1f}–"
                                   f"{s.end_frame / FPS:.1f}s has no DVD match (edition difference?)")
    if len(matched) > ACCEPT["max_segments"]:
        reasons.append(f"{len(matched)} matched segments (> {ACCEPT['max_segments']})")
    textured = ~res.flat
    if textured.any():
        frac = float((res.matched & textured).sum() / textured.sum())
        if frac < ACCEPT["min_matched_fraction"]:
            reasons.append(f"only {frac:.0%} of textured master samples matched the DVD")
    if res.reverse_agreement is None:
        reasons.append("reverse alignment not run")
    elif res.reverse_agreement < ACCEPT["reverse_agreement_min"]:
        reasons.append(f"reverse alignment agrees on only {res.reverse_agreement:.1%} of samples")
    if len(reasons) > 12:
        reasons = [*reasons[:12], f"... and {len(reasons) - 12} more"]
    res.reasons = reasons
    res.decision = "AUTO-ACCEPTED" if not reasons else "NEEDS-REVIEW"


def measure(master: np.ndarray, dvd: np.ndarray, *, params: dict[str, Any] | None = None,
            progress=None) -> AlignmentResult:
    """Forward path + dense check + reverse check + decision."""
    fwd = align(master, dvd, params=params, progress=progress)
    if progress:
        progress(0.65, "confirming offsets frame by frame")
    dense_check(master, dvd, fwd)
    rp = {**fwd.params, "min_offset": -fwd.params["max_offset"], "max_offset": -fwd.params["min_offset"]}
    if progress:
        progress(0.75, "reverse alignment")
    rev = align(dvd, master, params=rp)
    fwd.reverse_agreement = reverse_agreement(fwd, rev)
    decide(fwd)
    return fwd


def path_array(res: AlignmentResult) -> np.ndarray:
    """v38 ``align_npy`` layout: rows (master_frame, dvd_minus_master).

    Only samples inside matched segments are written. Gaps (no evidence between two offsets)
    and unmatched material are left out, so no consumer interpolates across an unmeasured edge.
    """
    keep = np.zeros(len(res.idxs), bool)
    for s in res.segments:
        if s.offset is not None:
            keep |= (res.idxs >= s.start_frame) & (res.idxs <= s.end_frame) & res.matched & (res.offsets == s.offset)
    return np.column_stack((res.idxs[keep], res.offsets[keep])).astype(np.int64)


def offset_at(path: np.ndarray, master_frame: int) -> int:
    """Offset in force at ``master_frame`` (nearest sampled point at or before it; else the first)."""
    k = int(np.searchsorted(path[:, 0], master_frame, side="right")) - 1
    return int(path[max(k, 0), 1])
