from __future__ import annotations

import numpy as np
import pytest

from fstk.core.errors import ArtifactError
from fstk.media.audio_origin import (
    ORIGIN_AGREEMENT_S,
    OUTPUT_FRAME_S,
    centre_pairs,
    fit_centre_referenced,
    origin_agreement,
)
from fstk.media.audio_timing import WINDOW


def test_tolerance_is_quarter_output_frame():
    assert pytest.approx(OUTPUT_FRAME_S / 4) == ORIGIN_AGREEMENT_S and pytest.approx(0.010427083, abs=1e-9) == ORIGIN_AGREEMENT_S


def test_centre_pairs_remove_start_bias():
    slope, offset = 1 + 864e-6, 0.37
    x = np.arange(100.0, 1500.0, 120.0)
    y_start = offset + slope * x + (slope - 1) * WINDOW / 2      # what start-referencing measures
    xc, yc = centre_pairs(x, y_start)
    fit = fit_centre_referenced(xc, yc)
    assert fit.intercept_s == pytest.approx(offset, abs=1e-9) and fit.rate_delta_ppm == pytest.approx(864, abs=1e-6)
    biased = fit_centre_referenced(x, y_start)
    assert (biased.intercept_s - offset) * 1000 == pytest.approx(21.6, abs=0.01)


def test_agreement_gate():
    ok = origin_agreement({"rate_fit_centre": 0.3700, "fade_midpoint": 0.3762, "duration_identity": 0.3731})
    assert ok.agree and ok.worst_pair == ("fade_midpoint", "rate_fit_centre")
    bad = origin_agreement({"rate_fit_start": 0.3916, "fade_midpoint": 0.3700})
    assert not bad.agree and "DISAGREE" in bad.describe() and "provisional" in bad.describe()
    with pytest.raises(ArtifactError, match="at least 2"):
        origin_agreement({"rate_fit_centre": 0.37})
    with pytest.raises(ArtifactError, match="finite"):
        origin_agreement({"a": 0.1, "b": float("nan")})
