"""A/V origin convention and cross-method agreement (owner decision 2026-11-21, finding 0001).

Convention
    Windowed correlations are referenced at the **window centre**. A start-referenced pair
    carries an origin bias of ≈ (rate − 1) × W/2 (+21.6 ms at 864 ppm for W = 50 s); the
    slope is unaffected. ``centre_pairs`` converts start-referenced measurements.

Agreement gate
    No single method defines the A/V origin. Independent estimates — the centre-referenced
    rate-fit intercept, the fade-midpoint fit intercept, and (no-DVD) the duration/PAD identity —
    must agree within ``ORIGIN_AGREEMENT_S`` before an origin may be committed. When they do not,
    the result says so and the review clips decide; nothing picks one silently.

``ORIGIN_AGREEMENT_S`` is ¼ of an output frame (≈10.4 ms), the owner-approved starting value.
It is **provisional until calibrated** on S01E20's saved window data and the E07 measurements;
change it only here, with the calibration evidence recorded in docs/findings/0001.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np

from fstk.core.errors import ArtifactError

from .audio_timing import WINDOW, robust_line_fit

OUTPUT_FRAME_S = 1001.0 / 24000.0
ORIGIN_AGREEMENT_S = OUTPUT_FRAME_S / 4.0
ORIGIN_AGREEMENT_STATUS = "provisional: calibrate on S01E20 saved windows and E07 before locking"
CONVENTION = "window-centre"


def centre_pairs(x_start: np.ndarray, y_start: np.ndarray, *, window_s: float = WINDOW,
                 y_map_slope: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Convert start-referenced pairs to centre-referenced pairs.

    ``y_map_slope`` is d(y)/d(matched-reference time) — e.g. the picture map's master-per-DVD
    slope when y is a master-equivalent time. When the exact centre of the matched window can be
    mapped directly (preferred), do that instead and pass the result as ``y`` with slope 1.
    """
    x = np.asarray(x_start, dtype=np.float64)
    y = np.asarray(y_start, dtype=np.float64)
    if x.shape != y.shape or x.ndim != 1:
        raise ArtifactError("start pairs must be two equal-length 1-D arrays")
    return x + window_s / 2.0, y + y_map_slope * window_s / 2.0


@dataclass(frozen=True)
class OriginFit:
    slope: float
    intercept_s: float
    residual_mad_s: float
    loo_slope_range: tuple[float, float]
    convention: str = CONVENTION

    @property
    def rate_delta_ppm(self) -> float:
        return (self.slope - 1.0) * 1e6


def fit_centre_referenced(x_centre: np.ndarray, y_centre: np.ndarray, use: np.ndarray | None = None) -> OriginFit:
    x = np.asarray(x_centre, dtype=np.float64)
    y = np.asarray(y_centre, dtype=np.float64)
    mask = np.ones_like(x, dtype=bool) if use is None else np.asarray(use, dtype=bool)
    if int(mask.sum()) < 4:
        raise ArtifactError(f"only {int(mask.sum())} usable windows; need >= 4")
    slope, intercept, mad, loo = robust_line_fit(x[mask], y[mask])
    return OriginFit(float(slope), float(intercept), float(mad), (float(loo[0]), float(loo[1])))


@dataclass(frozen=True)
class Agreement:
    agree: bool
    tolerance_s: float
    estimates: dict[str, float]
    spread_s: float
    worst_pair: tuple[str, str] | None
    status: str = ORIGIN_AGREEMENT_STATUS

    def describe(self) -> str:
        parts = ", ".join(f"{k}={v * 1000:.2f} ms" for k, v in self.estimates.items())
        verdict = "AGREE" if self.agree else "DISAGREE — review the A/V samples; nothing is chosen automatically"
        return (f"origin estimates {parts}; spread {self.spread_s * 1000:.2f} ms vs tolerance "
                f"{self.tolerance_s * 1000:.2f} ms ({self.status}) → {verdict}")


def origin_agreement(estimates: dict[str, float], *, tolerance_s: float = ORIGIN_AGREEMENT_S,
                     minimum_methods: int = 2) -> Agreement:
    """Compare independent origin estimates (seconds, same reference). Fails closed."""
    clean = {k: float(v) for k, v in estimates.items() if v is not None}
    if any(not np.isfinite(v) for v in clean.values()):
        raise ArtifactError("origin estimates must be finite")
    if len(clean) < minimum_methods:
        raise ArtifactError(f"need at least {minimum_methods} independent origin estimates, got {sorted(clean)}")
    worst, spread = None, 0.0
    for a, b in combinations(sorted(clean), 2):
        d = abs(clean[a] - clean[b])
        if d > spread:
            worst, spread = (a, b), d
    return Agreement(spread <= tolerance_s, tolerance_s, clean, spread, worst)
