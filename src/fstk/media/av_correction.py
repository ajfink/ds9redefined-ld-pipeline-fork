"""Turn a reviewer's per-chunk lip-sync verdicts into the next audio timing correction.

Owner decision 2026-11-21: the only human review is an A/V check over several 15-second chunks
of the delivered file. If a chunk sounds wrong, the reviewer marks it *audio too early* or
*audio too late* and asks for a rerun, and the app corrects the audio by itself.

Model: the delivered audio needs an extra delay

    D(t) = offset_ms + drift_ppm · 1e-3 · t      (ms, t = programme seconds)

where a positive value plays the audio later. Each verdict is a constraint on the **true** D at
the chunk's centre time, relative to the delay ``D_a(t)`` already applied when it was judged:

* ``early`` (audio leads picture)  → D(t) ≥ D_a(t) + NOTICE_MS
* ``late``  (audio lags picture)   → D(t) ≤ D_a(t) − NOTICE_MS
* ``ok``                           → D_a(t) − OK_LATE_MS ≤ D(t) ≤ D_a(t) + OK_EARLY_MS

The ``ok`` window is asymmetric (ITU-R BT.1359 detectability: audio ~45 ms early, ~125 ms late).
Verdicts from every earlier round are kept, so the feasible region only shrinks. The next
correction is the centre of the feasible region inside a trust box around the last applied
correction. The box doubles each round that still pushes against it, which brackets the true
value quickly and then bisects. Drift stays at zero until a round's verdicts disagree across the
episode (for example early at the start and late at the end), or until no offset alone fits
the marks of all rounds. Contradictory rounds are reported rather than averaged away.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

NOTICE_MS = 20.0          # a reported early/late error is at least this large
OK_EARLY_MS = 45.0        # undetectable audio lead
OK_LATE_MS = 125.0        # undetectable audio lag
OFFSET_LIMIT_MS = 2000.0
DRIFT_LIMIT_PPM = 3000.0
FIRST_STEP_MS = 80.0      # trust box half-width for the first offset correction
FIRST_STEP_PPM = 200.0    # ... and for drift, once drift is in play
VERDICTS = ("ok", "early", "late")
SCHEMA = "fstk-av-correction-v1"


@dataclass(frozen=True)
class Correction:
    offset_ms: float = 0.0
    drift_ppm: float = 0.0

    def delay_ms(self, t_s: float) -> float:
        return self.offset_ms + self.drift_ppm * 1e-3 * t_s


class ContradictoryVerdicts(ValueError):
    """No offset/drift satisfies every recorded verdict."""


def _grid(drift: bool) -> tuple[np.ndarray, np.ndarray]:
    o = np.arange(-OFFSET_LIMIT_MS, OFFSET_LIMIT_MS + 0.5, 1.0)
    k = np.arange(-DRIFT_LIMIT_PPM, DRIFT_LIMIT_PPM + 0.5, 5.0) if drift else np.array([0.0])
    return np.meshgrid(o, k, indexing="xy")


def _feasible(rounds: list[dict], OFF: np.ndarray, DRIFT: np.ndarray) -> np.ndarray:
    ok = np.ones(OFF.shape, bool)
    for r in rounds:
        applied = Correction(**r["applied"])
        for v in r["verdicts"]:
            t, verdict = float(v["t_s"]), v["verdict"]
            d = OFF + DRIFT * 1e-3 * t
            da = applied.delay_ms(t)
            if verdict == "early":
                ok &= d >= da + NOTICE_MS
            elif verdict == "late":
                ok &= d <= da - NOTICE_MS
            else:
                ok &= (d >= da - OK_LATE_MS) & (d <= da + OK_EARLY_MS)
    return ok


def _mixed(rounds: list[dict]) -> bool:
    return any({v["verdict"] for v in r["verdicts"]} >= {"early", "late"} for r in rounds)


def next_correction(state: dict[str, Any] | None, verdicts: list[dict[str, Any]]) -> tuple[Correction, dict]:
    """Add one review round (verdicts on the current correction); return the next correction + state.

    ``verdicts``: ``[{"t_s": chunk centre seconds, "verdict": "ok"|"early"|"late"}, ...]``.
    ``state`` is what this function returned last time (or None before the first rerun).
    """
    if not verdicts:
        raise ValueError("no verdicts")
    for v in verdicts:
        if v.get("verdict") not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}: {v!r}")
    state = dict(state or {"schema": SCHEMA, "rounds": [], "current": {"offset_ms": 0.0, "drift_ppm": 0.0},
                           "step_ms": FIRST_STEP_MS, "step_ppm": FIRST_STEP_PPM})
    current = Correction(**state["current"])
    rounds = [*state["rounds"], {"applied": {"offset_ms": current.offset_ms, "drift_ppm": current.drift_ppm},
                                 "verdicts": [{"t_s": float(v["t_s"]), "verdict": v["verdict"]} for v in verdicts]}]
    if all(v["verdict"] == "ok" for v in verdicts):
        return current, {**state, "rounds": rounds, "settled": True}
    drift = bool(state.get("drift_in_play")) or _mixed(rounds)
    OFF, DRIFT = _grid(drift)
    feasible = _feasible(rounds, OFF, DRIFT)
    if not feasible.any() and not drift:          # an offset alone cannot explain the marks: try drift
        drift = True
        OFF, DRIFT = _grid(True)
        feasible = _feasible(rounds, OFF, DRIFT)
    if not feasible.any():
        raise ContradictoryVerdicts("the marks in this round contradict earlier rounds; no single offset/drift "
                                    "fits them all. Re-check the chunks, or reset the corrections.")
    step_ms, step_ppm = float(state["step_ms"]), float(state["step_ppm"])
    for _ in range(12):
        box = (np.abs(OFF - current.offset_ms) <= step_ms) & (np.abs(DRIFT - current.drift_ppm) <= step_ppm)
        inside = feasible & box
        if inside.any():
            # if the feasible region continues past the box edge, the next round widens the box
            touches = (feasible & ~box).any()
            break
        step_ms, step_ppm = step_ms * 2, step_ppm * 2
    else:
        inside, touches = feasible, False
    nxt = Correction(round(float(OFF[inside].mean()), 1), round(float(DRIFT[inside].mean()), 1))
    new_state = {**state, "rounds": rounds, "current": {"offset_ms": nxt.offset_ms, "drift_ppm": nxt.drift_ppm},
                 "step_ms": step_ms * 2 if touches else max(step_ms / 2, 5.0),
                 "step_ppm": (step_ppm * 2 if touches else max(step_ppm / 2, 10.0)) if drift else step_ppm,
                 "settled": False, "drift_in_play": drift,
                 "feasible_offset_ms": [float(OFF[feasible].min()), float(OFF[feasible].max())],
                 "feasible_drift_ppm": [float(DRIFT[feasible].min()), float(DRIFT[feasible].max())]}
    return nxt, new_state
