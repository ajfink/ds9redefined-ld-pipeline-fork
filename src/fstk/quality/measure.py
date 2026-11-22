"""Pure-numpy picture measurements (levels, grain, chroma noise, dot crawl, dropouts).

All inputs are ``fstk.quality.frames.Planes`` on the 8-bit code scale. Grain is *measured*, never
modelled: the residual after a small Gaussian blur, taken only in flat regions so picture detail is
not counted as grain.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from fstk.quality.frames import Planes, upsample_chroma

GRAIN_SIGMA = 1.5
FLAT_GRADIENT = 1.2            # max |gradient| of the 5x5 box-smoothed luma for "flat"
MIN_FLAT_PIXELS = 2000


# ------------------------------------------------------------------------------ levels
def level_stats(ys: list[np.ndarray]) -> dict[str, float]:
    """Pooled luma percentiles (8-bit codes) over many frames plus clipping fractions."""
    pooled = np.concatenate([y.ravel()[::7] for y in ys])         # subsample: percentiles are stable
    q = np.percentile(pooled, [0.1, 1, 50, 99, 99.9])
    return {"p0_1": float(q[0]), "p1": float(q[1]), "p50": float(q[2]), "p99": float(q[3]), "p99_9": float(q[4]),
            "frac_below_16": float((pooled < 15.5).mean()), "frac_above_235": float((pooled > 235.5).mean()),
            "frac_floor": float((pooled <= 1.5).mean()), "frac_ceiling": float((pooled >= 254.5).mean())}


def compare_levels(ld: dict[str, float], ref: dict[str, float], *, black_tol: float = 3.0,
                   white_tol: float = 6.0, mid_tol: float = 6.0) -> list[dict]:
    """Findings where the delivered luma differs from the reference (DVD) beyond tolerance.

    Black point = p1, white point = p99. ``mid`` catches a gamma/contrast slip the end points miss.
    The reference is a *measurement target only*; nothing here changes pixels.
    """
    out = []
    for key, tol, label in (("p1", black_tol, "black point"), ("p99", white_tol, "white point"),
                            ("p50", mid_tol, "median luma")):
        d = ld[key] - ref[key]
        out.append({"check": label, "ld": round(ld[key], 2), "reference": round(ref[key], 2), "delta": round(d, 2),
                    "tolerance": tol, "status": "PASS" if abs(d) <= tol else "FLAG"})
    return out


def clipping_findings(stats: dict[str, float], *, floor_limit: float = 0.02, ceiling_limit: float = 0.01) -> list[dict]:
    """Absolute clipping: a lot of pixels pinned to code 0/255 means crushed blacks or blown whites."""
    return [{"check": "crushed blacks (codes <= 1)", "value": round(stats["frac_floor"], 4), "limit": floor_limit,
             "status": "PASS" if stats["frac_floor"] <= floor_limit else "FLAG"},
            {"check": "clipped whites (codes >= 255)", "value": round(stats["frac_ceiling"], 4), "limit": ceiling_limit,
             "status": "PASS" if stats["frac_ceiling"] <= ceiling_limit else "FLAG"}]


# ------------------------------------------------------------------------------ flat areas and grain
def flat_mask(y: np.ndarray, *, gradient: float = FLAT_GRADIENT) -> np.ndarray:
    sm = cv2.blur(y.astype(np.float32), (5, 5))
    gx = np.abs(cv2.Sobel(sm, cv2.CV_32F, 1, 0, ksize=3)) / 8.0
    gy = np.abs(cv2.Sobel(sm, cv2.CV_32F, 0, 1, ksize=3)) / 8.0
    mask = (gx + gy) < gradient
    mask[:4, :] = mask[-4:, :] = False
    mask[:, :4] = mask[:, -4:] = False
    return mask


def _residual(a: np.ndarray, sigma: float = GRAIN_SIGMA) -> np.ndarray:
    a = a.astype(np.float32)
    return a - cv2.GaussianBlur(a, (0, 0), sigma)


def grain_power(y: np.ndarray, mask: np.ndarray | None = None) -> dict[str, float] | None:
    """Luma grain: residual std (codes) in flat areas, and fine/coarse ratio as a grain-*structure* number.

    Returns None when the frame has too little flat area to say anything (the caller skips it).
    """
    mask = flat_mask(y) if mask is None else mask
    if int(mask.sum()) < MIN_FLAT_PIXELS:
        return None
    fine, coarse = _residual(y, 0.8)[mask], _residual(y, 2.5)[mask]
    r = _residual(y)[mask]
    return {"std": float(r.std()), "fine_over_coarse": float(fine.std() / max(coarse.std(), 1e-6)),
            "flat_pixels": int(mask.sum())}


def grain_retention(before: np.ndarray, after: np.ndarray) -> dict[str, float] | None:
    """How much grain a processing step kept: after/before residual std in the *before* frame's flat areas.

    1.0 = unchanged; < 1 = grain lost (smoothing); > 1 = noise added. None if undefined.
    """
    mask = flat_mask(before)
    if int(mask.sum()) < MIN_FLAT_PIXELS:
        return None
    b, a = _residual(before)[mask], _residual(after)[mask]
    if b.std() < 1e-6:
        return None
    return {"ratio": float(a.std() / b.std()), "before_std": float(b.std()), "after_std": float(a.std())}


# ------------------------------------------------------------------------------ chroma noise / dot crawl
def chroma_noise(p: Planes) -> dict[str, float] | None:
    """Chroma residual std (codes) in luma-flat areas, Cb and Cr, plus checkerboard (dot-crawl) energy."""
    mask = flat_mask(p.y)
    if int(mask.sum()) < MIN_FLAT_PIXELS:
        return None
    u, v = upsample_chroma(p)
    ru, rv = _residual(u)[mask], _residual(v)[mask]
    return {"cb_std": float(ru.std()), "cr_std": float(rv.std()),
            "dot_crawl_luma": checker_energy(p.y), "dot_crawl_chroma": max(checker_energy(u), checker_energy(v))}


def checker_energy(a: np.ndarray, block: int = 16) -> float:
    """RMS over blocks of the Nyquist-checkerboard component (the signature of composite dot crawl)."""
    h, w = (a.shape[0] // block) * block, (a.shape[1] // block) * block
    x = a[:h, :w].astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    sign = np.where(((yy + xx) & 1) == 0, 1.0, -1.0).astype(np.float32)
    prod = (x - cv2.blur(x, (3, 3))) * sign
    blocks = prod.reshape(h // block, block, w // block, block).mean(axis=(1, 3))
    return float(np.sqrt((blocks ** 2).mean()))


def cross_colour_ratio(p: Planes) -> float | None:
    """Chroma activity where luma has fine detail vs where it is flat; >> 1 means luma detail leaks into colour."""
    mask = flat_mask(p.y)
    detail = ~mask
    detail[:4, :] = detail[-4:, :] = False
    detail[:, :4] = detail[:, -4:] = False
    if int(mask.sum()) < MIN_FLAT_PIXELS or int(detail.sum()) < MIN_FLAT_PIXELS:
        return None
    u, v = upsample_chroma(p)
    act = np.abs(_residual(u)) + np.abs(_residual(v))
    base = max(float(act[mask].mean()), 1e-3)
    return float(act[detail].mean() / base)


# ------------------------------------------------------------------------------ dropouts
@dataclass
class Dropout:
    x: int
    y: int
    w: int
    h: int
    area: int
    peak: float


def dropout_mask(prev: np.ndarray, cur: np.ndarray, nxt: np.ndarray, *, threshold: float = 28.0,
                 static: float = 10.0, min_run: int = 5) -> np.ndarray:
    """Pixels of ``cur`` that disagree with both neighbours while those agree with each other.

    LaserDisc dropouts are short horizontal streaks, so a horizontal close (>= ``min_run`` wide) keeps
    them and drops isolated grain peaks. Moving areas (neighbours disagree) are never flagged: a
    real detail change is not a defect.
    """
    p, c, n = (a.astype(np.float32) for a in (prev, cur, nxt))
    med = np.median(np.stack([p, c, n]), axis=0)
    candidate = (np.abs(c - med) > threshold) & (np.abs(p - n) < static)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (min_run, 1))
    closed = cv2.morphologyEx(candidate.astype(np.uint8), cv2.MORPH_CLOSE, k)
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, k)
    return opened.astype(bool)


def dropouts(prev: np.ndarray, cur: np.ndarray, nxt: np.ndarray, **kw) -> list[Dropout]:
    mask = dropout_mask(prev, cur, nxt, **kw)
    if not mask.any():
        return []
    # join streaks a few lines apart into one defect, then box them
    grown = cv2.dilate(mask.astype(np.uint8), np.ones((5, 9), np.uint8))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        inside = mask[y:y + h, x:x + w]
        if not inside.any():
            continue
        diff = np.abs(cur[y:y + h, x:x + w].astype(np.float32) - prev[y:y + h, x:x + w].astype(np.float32))
        out.append(Dropout(int(x), int(y), int(w), int(h), int(inside.sum()), float(diff[inside].max())))
    return out
