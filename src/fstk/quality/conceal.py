"""Dropout concealment from the *same LaserDisc's* neighbouring frames (never the DVD).

A fix copies the defect's rectangle from frame n-1 or n+1, shifted by a small integer motion
vector. Because the pixels are real captured grain, the result keeps the picture's grain character;
nothing is smoothed or synthesised. A fix is accepted only when (1) the surrounding ring matches the
source well (it is the same content, same place), (2) the source patch is itself defect-free, and
(3) the copied patch has the same grain strength as its ring. Anything else stays as captured and is
reported as residual.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from fstk.quality.measure import Dropout, _residual

SEARCH = 6          # +- pixels of motion searched (even steps only: 4:2:2 chroma stays aligned)
RING = 6
GRAIN_BAND = (0.7, 1.4)     # patch grain std / ring grain std must stay inside this


@dataclass(frozen=True)
class Fix:
    frame: int
    src: int            # -1 (previous frame) or +1 (next frame)
    dx: int
    dy: int
    x: int
    y: int
    w: int
    h: int
    cost: float

    def tsv(self) -> str:
        return "\t".join(str(v) for v in (self.frame, self.src, self.dx, self.dy, self.x, self.y, self.w, self.h,
                                          f"{self.cost:.3f}"))


def even_rect(d: Dropout, width: int, height: int, *, grow_x: int = 2, grow_y: int = 1) -> tuple[int, int, int, int]:
    x0, y0 = max(0, d.x - grow_x), max(0, d.y - grow_y)
    x1, y1 = min(width, d.x + d.w + grow_x), min(height, d.y + d.h + grow_y)
    x0, x1 = x0 - x0 % 2, x1 + x1 % 2
    return x0, y0, min(width, x1) - x0, y1 - y0


def _ring(shape, x, y, w, h, ring):
    m = np.zeros(shape, bool)
    m[max(0, y - ring):min(shape[0], y + h + ring), max(0, x - ring):min(shape[1], x + w + ring)] = True
    m[y:y + h, x:x + w] = False
    return m


def plan_fix(frame: int, cur: np.ndarray, sources: dict[int, tuple[np.ndarray, np.ndarray]], d: Dropout, *,
             max_cost: float, cur_mask: np.ndarray | None = None) -> tuple[Fix | None, str]:
    """Best fix for one defect, or (None, reason). ``sources``: {-1: (prev_luma, prev_mask), 1: (next_luma, next_mask)}."""
    h_img, w_img = cur.shape
    x, y, w, h = even_rect(d, w_img, h_img)
    ring = _ring(cur.shape, x, y, w, h, RING)
    if cur_mask is not None:
        ring &= ~cur_mask
    if ring.sum() < 40:
        return None, "no clean surround to compare"
    best, best_cost = None, 1e9
    for src, (img, msk) in sources.items():
        for dy in range(-SEARCH, SEARCH + 1, 2):
            for dx in range(-SEARCH, SEARCH + 1, 2):
                xs, ys = x + dx, y + dy
                if xs < 0 or ys < 0 or xs + w > w_img or ys + h > h_img:
                    continue
                if msk[ys:ys + h, xs:xs + w].any():
                    continue                                    # the source has its own defect there
                shifted = np.roll(np.roll(img, -dy, axis=0), -dx, axis=1)   # shifted[p] = img[p + (dy, dx)]
                cost = float(np.abs(shifted[ring] - cur[ring]).mean())
                if cost < best_cost:
                    best, best_cost = (src, dx, dy), cost
    if best is None:
        return None, "no defect-free source patch in range"
    if best_cost > max_cost:
        return None, f"best surround match {best_cost:.2f} exceeds {max_cost:.2f} (motion or scene change)"
    src, dx, dy = best
    img = sources[src][0]
    patch = img[y + dy:y + dy + h, x + dx:x + dx + w]
    pr, rr = _residual(patch)[2:-2, 2:-2], _residual(cur)[ring]
    if pr.size and rr.size and rr.std() > 1e-6:
        ratio = float(pr.std() / rr.std())
        if not GRAIN_BAND[0] <= ratio <= GRAIN_BAND[1]:
            return None, f"copied patch grain ratio {ratio:.2f} outside {GRAIN_BAND}"
    return Fix(frame, src, dx, dy, x, y, w, h, best_cost), "ok"


def expected_cost(grain_std: float, motion_allowance: float = 1.5) -> float:
    """Mean |difference| expected between two frames of the same static content: independent grain
    (0.8 * sigma * sqrt(2) for a Gaussian) plus an allowance for small motion."""
    return 0.8 * grain_std * 2 ** 0.5 + motion_allowance
