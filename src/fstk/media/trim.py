"""Head/tail trims and ``video.delta_s`` from the DVD alignment and the exact IVTC frame map.

v38 had these entered by a person after a visual review (``record_video_trim_review.py``) and
``video.delta_s`` set by hand. Owner decision 2026-11-21: automate. The rules:

* **Head.** The programme starts at the DVD programme's first picture, which the alignment
  measures (``origin``: the first textured master sample matched to DVD picture, at 15-frame
  resolution). Walking back over at most one sampling step of textured frames gives the first
  frame of the fade-up. ``HEAD_LEAD`` master frames of black before it are kept, if they are
  black, so the output opens on black. ``head_trim`` is the first decimated output frame whose
  source frame is at or after that point, and ``video.delta_s`` is that source frame's time,
  exact via the IVTC probe. Cross-check: across 12 v38 episodes ``delta_s`` was 49.5–52.4 s and
  is the master frame of the first programme frame (S01E15: 1515, the same frame fstk measures
  as S01E20's origin).
* **Tail.** v38 never trimmed the tail (``tail_trim`` = 0 in all 12 episodes). Here the tail is
  cut only if the LD carries ≥ 5 s of textured picture after the last DVD-matched act that the
  DVD does not have (e.g. a trailer). Otherwise it runs to the end of the master.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

FPS = 30000.0 / 1001.0
HEAD_LEAD = 2              # master frames of black kept before the fade-up
STEP_GUARD = 15            # the alignment samples every 15 frames
TAIL_MIN_S = 5.0


@dataclass
class TrimDecision:
    head_trim: int
    tail_trim: int
    frames: int
    delta_s: float
    head_master_frame: int
    fade_up_master_frame: int
    tail_last_master_frame: int
    full_frames: int
    notes: list[str]

    def summary(self) -> dict:
        return asdict(self)


def fade_up_frame(flat: np.ndarray, origin: int) -> int:
    """First textured frame of the run that reaches ``origin`` (looking back ≤ STEP_GUARD frames)."""
    f = origin
    while f > 0 and origin - f < STEP_GUARD and not flat[f - 1]:
        f -= 1
    return f


def tail_ld_only_start(samples: list[dict], last_act_end: int, step: int) -> int | None:
    """First master frame of ≥ TAIL_MIN_S textured, DVD-unmatched picture after the last act."""
    need = int(np.ceil(TAIL_MIN_S * FPS / step))
    run: list[int] = []
    for s in samples:
        f = int(s["master_frame"])
        if f <= last_act_end:
            continue
        if s["state"] == "unmatched" and not int(s["flat"]):
            run.append(f)
            if len(run) >= need:
                return run[0]
        else:
            run = []
    return None


def decide(probe: dict, *, flat: np.ndarray, origin: int, tail_cut: int | None) -> TrimDecision:
    """``probe``: the IVTC probe JSON (``frames``, ``map`` output frame → master frame)."""
    mapping = {int(k): int(v) for k, v in probe["map"].items()}
    n = int(probe["frames"])
    notes = []
    fade = fade_up_frame(flat, origin)
    head_m = fade
    lead = flat[max(fade - HEAD_LEAD, 0):fade]
    if fade >= HEAD_LEAD and len(lead) == HEAD_LEAD and lead.all():
        head_m = fade - HEAD_LEAD
    else:
        notes.append("no black before the fade-up: output starts on the fade-up frame")
    head_candidates = sorted(k for k, m in mapping.items() if m >= head_m)
    if not head_candidates:
        raise ValueError(f"the IVTC probe does not reach master frame {head_m}; probe more head frames")
    head = head_candidates[0]
    if head > 0 and head - 1 not in mapping:
        raise ValueError("the head probe window does not cover the head trim point")
    if tail_cut is None:
        tail = 0
        last_m = mapping.get(n - 1, -1)
    else:
        keep = [k for k, m in mapping.items() if m < tail_cut and k > head]
        if not keep or max(keep) + 1 not in mapping:
            raise ValueError(f"the tail probe window does not cover the LD-only tail at master frame {tail_cut}")
        last = max(keep)
        tail = n - 1 - last
        last_m = mapping[last]
        notes.append(f"LD-only picture from master frame {tail_cut}: tail trimmed")
    frames = n - head - tail
    if frames <= 0:
        raise ValueError("trims leave no frames")
    return TrimDecision(head_trim=head, tail_trim=tail, frames=frames, delta_s=mapping[head] / FPS,
                        head_master_frame=mapping[head], fade_up_master_frame=fade, tail_last_master_frame=last_m,
                        full_frames=n, notes=notes)
