"""Lip-sync verdicts → audio timing correction (bisection over offset and drift)."""
from __future__ import annotations

import pytest

from fstk.media.av_correction import ContradictoryVerdicts, Correction, next_correction

CHUNKS = [120.0, 450.0, 800.0, 1150.0, 1500.0, 1850.0, 2200.0, 2550.0]   # chunk centres (s)


def reviewer(true: Correction, applied: Correction):
    """A viewer who notices audio ≥ 45 ms early or ≥ 125 ms late (ITU-R BT.1359)."""
    out = []
    for t in CHUNKS:
        err = true.delay_ms(t) - applied.delay_ms(t)      # > 0: audio still plays too early
        out.append({"t_s": t, "verdict": "early" if err >= 45 else "late" if err <= -125 else "ok"})
    return out


@pytest.mark.parametrize("true", [Correction(60, 0), Correction(-300, 0), Correction(900, 0),
                                  Correction(0, 120), Correction(-80, -200), Correction(30, 60)])
def test_converges_to_in_sync_everywhere(true):
    applied, state = Correction(), None
    for round_no in range(1, 13):  # noqa: B007 - used after the loop
        verdicts = reviewer(true, applied)
        if all(v["verdict"] == "ok" for v in verdicts):
            break
        applied, state = next_correction(state, verdicts)
    else:
        pytest.fail(f"no convergence for {true}: last applied {applied}")
    assert round_no <= 9, round_no
    assert all(-125 < true.delay_ms(t) - applied.delay_ms(t) < 45 for t in CHUNKS)


def test_first_step_is_moderate_and_in_the_right_direction():
    nxt, state = next_correction(None, [{"t_s": t, "verdict": "early"} for t in CHUNKS])
    assert 20 < nxt.offset_ms <= 80 and nxt.drift_ppm == 0
    nxt, _ = next_correction(None, [{"t_s": t, "verdict": "late"} for t in CHUNKS])
    assert -80 <= nxt.offset_ms < -20


def test_mixed_verdicts_bring_drift_into_play():
    verdicts = [{"t_s": t, "verdict": "early" if t > 1500 else "ok"} for t in CHUNKS]
    nxt, state = next_correction(None, verdicts)
    assert nxt.drift_ppm == 0                    # all non-ok marks agree → still an offset
    verdicts = [{"t_s": t, "verdict": "late" if t < 1000 else "early" if t > 1800 else "ok"} for t in CHUNKS]
    nxt, state = next_correction(None, verdicts)
    assert nxt.drift_ppm > 0 and state["drift_in_play"]


def test_contradictions_are_reported_not_averaged():
    _, state = next_correction(None, [{"t_s": 100.0, "verdict": "early"}])
    applied = Correction(**state["current"])
    # now the same chunk is "late" at a delay that is *less* than one already judged early
    state["current"] = {"offset_ms": applied.offset_ms - 200, "drift_ppm": 0.0}
    with pytest.raises(ContradictoryVerdicts):
        next_correction(state, [{"t_s": 100.0, "verdict": "late"}])


def test_all_ok_settles_without_changing():
    nxt, state = next_correction({"schema": "x", "rounds": [], "current": {"offset_ms": 40.0, "drift_ppm": 0.0},
                                  "step_ms": 80.0, "step_ppm": 200.0}, [{"t_s": 10.0, "verdict": "ok"}])
    assert nxt == Correction(40.0, 0.0) and state["settled"]
