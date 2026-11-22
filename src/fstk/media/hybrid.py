"""Hybrid cadence plan: which stretches are film (3:2 telecine → 23.976p) and which are video-origin.

Film is decimated 5→4 and is exact at 23.976p. Video-origin stretches (interlaced inserts, native 29.97
progressive, CGI/titles) have real motion in every field; forcing them through the same decimation drops
a frame in five and judders. For those stretches the lossless choice is to keep every field: deliver them at
59.94p (interlaced) or 29.97p (progressive) and let the container carry per-frame timestamps (Matroska
v2 timecodes). The programme duration is identical to the constant-rate cut, so audio sync is unaffected.

This module is the *plan* only (pure): classification from the TFM decisions, cycle-aligned segment
boundaries, the output timeline and the timecode file. Nothing here changes a delivered file.
"""
from __future__ import annotations

from dataclasses import dataclass

from fstk.media.cadence import TFM_MI

CYCLE = 5            # source frames per telecine cycle (4 output frames)
OUT_PER_CYCLE = 4
MIN_SEGMENT_CYCLES = 6            # 1 s: shorter stretches are not worth a rate change
NATIVE_30P_MAX_ACTIVITY = 0.12    # share of non-"c" matches in a window that still counts as having no telecine
NATIVE_30P_WINDOW = 60

#: Frame durations in 1/60000 s units so every rate is an integer: 24000/1001 → 2502.5/... use exact fractions below.
FILM = "film"
VIDEO_I = "video-interlaced"       # → 59.94p (two fields = two frames)
VIDEO_P = "video-progressive"      # → 29.97p (no decimation)
RATE = {FILM: (24000, 1001), VIDEO_P: (30000, 1001), VIDEO_I: (60000, 1001)}


@dataclass(frozen=True)
class Segment:
    kind: str
    first_cycle: int            # inclusive, in source telecine cycles
    end_cycle: int              # exclusive

    @property
    def source_frames(self) -> tuple[int, int]:
        return self.first_cycle * CYCLE, self.end_cycle * CYCLE

    @property
    def cfr_frames(self) -> int:
        return (self.end_cycle - self.first_cycle) * OUT_PER_CYCLE

    @property
    def frames(self) -> int:
        n = self.end_cycle - self.first_cycle
        return {FILM: n * 4, VIDEO_P: n * 5, VIDEO_I: n * 10}[self.kind]

    def as_dict(self) -> dict:
        return {"kind": self.kind, "first_cycle": self.first_cycle, "end_cycle": self.end_cycle,
                "source_frames": list(self.source_frames), "frames": self.frames, "cfr_frames": self.cfr_frames}


def classify_cycles(rows: list[dict]) -> list[str]:
    """One label per whole telecine cycle of the TFM log: film | video-interlaced | video-progressive."""
    mics = [int(r["mic"]) for r in rows]
    codes = [str(r["match_code"]).lower() for r in rows]
    n_cycles = len(rows) // CYCLE
    labels = []
    for c in range(n_cycles):
        a, b = c * CYCLE, (c + 1) * CYCLE
        if sum(m > TFM_MI for m in mics[a:b]) >= 3:            # combed even after field matching: real interlaced motion
            labels.append(VIDEO_I)
            continue
        lo, hi = max(0, a - NATIVE_30P_WINDOW // 2), min(len(rows), b + NATIVE_30P_WINDOW // 2)
        window = codes[lo:hi]
        activity = sum(code != "c" for code in window) / max(len(window), 1)
        labels.append(VIDEO_P if activity <= NATIVE_30P_MAX_ACTIVITY and len(window) >= NATIVE_30P_WINDOW // 2 else FILM)
    return labels


def plan_segments(labels: list[str], *, min_cycles: int = MIN_SEGMENT_CYCLES) -> list[Segment]:
    """Runs of labels → segments; a non-film run shorter than ``min_cycles`` stays film (not worth a rate change)."""
    runs: list[list] = []
    for i, lab in enumerate(labels):
        if runs and runs[-1][0] == lab:
            runs[-1][2] = i + 1
        else:
            runs.append([lab, i, i + 1])
    out: list[Segment] = []
    for lab, a, b in runs:
        if lab != FILM and b - a < min_cycles:
            lab = FILM
        if out and out[-1].kind == lab and out[-1].end_cycle == a:
            out[-1] = Segment(lab, out[-1].first_cycle, b)
        else:
            out.append(Segment(lab, a, b))
    return out


def summary(segments: list[Segment]) -> dict:
    video = [s for s in segments if s.kind != FILM]
    seconds = sum((s.end_cycle - s.first_cycle) * CYCLE * 1001 / 30000 for s in video)
    return {"segments": [s.as_dict() for s in segments], "video_segments": len(video),
            "video_seconds": round(seconds, 2), "film_cycles": sum(s.end_cycle - s.first_cycle for s in segments
                                                                   if s.kind == FILM),
            "recommend_hybrid": bool(video)}


def timecodes_v2(segments: list[Segment]) -> str:
    """Matroska timestamp file (v2), milliseconds with 6 decimals, one line per output frame."""
    lines, t_num = ["# timecode format v2"], 0       # t in units of 1/(24000*30000*...) → use exact Fraction instead
    from fractions import Fraction

    t = Fraction(0)
    for s in segments:
        num, den = RATE[s.kind]
        step = Fraction(den, num)
        for _ in range(s.frames):
            lines.append(f"{float(t * 1000):.6f}")
            t += step
    del t_num
    return "\n".join(lines) + "\n"


def total_duration_s(segments: list[Segment]) -> float:
    from fractions import Fraction

    return float(sum(Fraction(s.frames * RATE[s.kind][1], RATE[s.kind][0]) for s in segments))
