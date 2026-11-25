"""Where a small audio edit is (S02E02, 2026-11-24: a 33 ms step placed ~8 s early; retimed audio 30 ms late there)."""
from __future__ import annotations

import numpy as np
import pytest

from fstk.media.retime_map import Line, locate_switch

HOP = 0.005


def envelopes(step_s: float, at_s: float, seconds: float = 200.0, seed: int = 3):
    """LD envelope, and the DVD one with ``step_s`` of extra material inserted at LD time ``at_s``."""
    rng = np.random.default_rng(seed)
    n = round(seconds / HOP)
    raw = rng.random(n + 50)
    ld = np.convolve(raw, np.ones(8) / 8, mode="same")[:n].astype(np.float32)   # speech-like texture
    k, extra = round(at_s / HOP), round(step_s / HOP)
    dvd = np.concatenate((ld[:k], rng.random(extra).astype(np.float32), ld[k:]))
    return ld, dvd


@pytest.mark.parametrize("step_ms", [33.0, -33.0, 66.0])
def test_a_one_frame_step_is_placed_where_it_is(step_ms):
    step = step_ms / 1000
    at = 100.0
    ld, dvd = envelopes(max(step, 0.0), at) if step > 0 else envelopes(0.0, at)
    if step < 0:                                          # LD has the extra material: remove it from the DVD
        k = round(at / HOP)
        dvd = np.concatenate((ld[:k], ld[k + round(-step / HOP):]))
    a = Line(40.0, 94.0, 0.0, 1.0, 20, 1.0)
    b = Line(106.0, 160.0, step, 1.0, 20, 1.0)
    w, info = locate_switch(a, b, ld, dvd, HOP)
    assert info["method"] == "lag" and info["clear"], info
    assert abs(w - at) <= 0.5, w
