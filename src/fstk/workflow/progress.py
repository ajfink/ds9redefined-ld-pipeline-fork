"""Run progress: per-step percent, overall progress through the whole plan, and honest time estimates.

``ProgressTracker`` is fed the engine's events (and the job's saved state, so a resumed run starts where it
left off) and answers: which step is running, how far along it is, how far through the whole plan, how long it
has taken, and, once there is evidence, how long it should still take. No Qt, so it is tested on its own.

Overall progress weights each step by how long it normally takes (not 1/N per step: a 2-second step and the
multi-hour encode are not the same fraction of an episode). Weights start as rough defaults and are replaced by
the median of timings recorded on this machine (``Timings``, stored under the data root) as soon as any exist.
An estimate is shown only with a basis: this step's own rate, or recorded history. Never an invented clock time.
"""
from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Rough seconds per stage for one ~45-minute episode on a modern machine; only used until real timings exist.
DEFAULT_SECONDS: dict[str, float] = {
    "archive-download": 3600, "capture-inventory": 600, "efm-decode": 1800, "efm-select": 60,
    "stack-reference": 900, "stack-render": 14400, "video-trim": 120, "cadence-evidence": 1800,
    "dvd-probe": 60, "dvd-alignment": 1800, "dvd-free-audio": 300, "dvd-ld-audio": 600, "dvd-retime": 600,
    "av-correction": 120, "video-quality": 900, "audio-quality": 300,
    "stretch-preview": 120, "frame-fixes": 120, "fix-preview": 60, "encode-final": 21600, "remux": 300,
    "qc-report": 900, "episode-report": 300,
}
FALLBACK_SECONDS = 120.0
KEEP_SAMPLES = 9
MIN_FRACTION_FOR_ETA = 0.03
MIN_SECONDS_FOR_ETA = 15.0

#: The words a person sees for each status, everywhere (Progress, run history, banners, notifications).
STATUS_WORDS: dict[str, str] = {
    "pending": "waiting", "running": "running", "passed": "done", "awaiting-review": "needs you",
    "stale": "out of date", "blocked": "blocked", "failed": "failed", "cancelled": "cancelled",
    "interrupted": "interrupted", "not-started": "not started",
}
#: Which problem to name first when several steps have one (S02E02 2026-11-25: the headline named a step that was only
#: out of date, ld-audio, instead of the one that failed, retime).
SEVERITY = {"failed": 0, "blocked": 1, "interrupted": 2, "stale": 3}
PROBLEMS = tuple(SEVERITY)

DONE = ("passed",)
SETTLED = ("passed", "failed", "blocked", "cancelled", "interrupted", "stale")
RUNNING = "running"
WAITING = "awaiting-review"


def _review_stages() -> set[str]:
    import fstk.stages  # noqa: F401  (registers the built-in stages)
    from fstk.stages.base import REGISTRY

    return {sid for sid, cls in REGISTRY.items() if getattr(cls, "review", None)}


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds != seconds:
        return "–"
    s = round(max(0.0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def worst_first(items, status_of) -> list:
    """Problems ordered most serious first (failed, blocked, interrupted, out of date), plan order within each."""
    probs = [x for x in items if status_of(x) in SEVERITY]
    return sorted(probs, key=lambda x: SEVERITY[status_of(x)])


def describe_job_state(state: Mapping[str, Any], name: Callable[[str], str] | None = None) -> str:
    """One line for a saved job: how far it got and what it is waiting on."""
    name = name or (lambda sid: sid)
    steps = state.get("steps") or {}
    done = sum(1 for st in steps.values() if st.get("status") == "passed")
    line = f"{done} of {len(steps)} steps done"
    waiting = [k for k, st in steps.items() if st.get("status") == WAITING]
    failed = worst_first(list(steps), lambda k: steps[k].get("status"))
    if failed:
        line += f" · {name(failed[0])} {STATUS_WORDS[steps[failed[0]]['status']]}"
    elif waiting:
        line += f" · waiting for your review: {name(waiting[0])}"
    elif done == len(steps) and steps:
        line += " · finished"
    return line


def _duration(st: Mapping[str, Any]) -> float | None:
    """Seconds between a saved step's start and finish, when both are recorded."""
    from datetime import datetime

    try:
        a = datetime.fromisoformat(str(st["started_utc"]))
        b = datetime.fromisoformat(str(st["finished_utc"]))
    except (KeyError, ValueError, TypeError):
        return None
    secs = (b - a).total_seconds()
    return secs if secs >= 0 else None


class Timings:
    """Recorded durations per stage (seconds), kept in one small JSON file; the median is the weight."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.data: dict[str, list[float]] = {}
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = {k: [float(x) for x in v] for k, v in raw.get("stages", {}).items()}
            except (OSError, ValueError, TypeError, AttributeError):
                self.data = {}

    def median(self, stage: str) -> float | None:
        vals = self.data.get(stage)
        return statistics.median(vals) if vals else None

    def expected(self, stage: str) -> float:
        return self.median(stage) or DEFAULT_SECONDS.get(stage, FALLBACK_SECONDS)

    def has_history(self, stage: str) -> bool:
        return bool(self.data.get(stage))

    def record(self, stage: str, seconds: float) -> None:
        if seconds < 1.0:
            return                              # reused/instant steps say nothing about how long work takes
        vals = self.data.setdefault(stage, [])
        vals.append(round(float(seconds), 1))
        del vals[:-KEEP_SAMPLES]
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            from fstk.core.fsio import atomic_write_json

            atomic_write_json(self.path, {"schema": "fstk-timings-v1", "stages": self.data})
        except OSError:
            pass                                 # timings are a convenience; never fail a run over them


def is_space_block(status: str, detail: str) -> bool:
    return status == "blocked" and "SPACE FAIL" in detail


@dataclass
class StepProgress:
    id: str
    stage: str
    status: str = "pending"
    fraction: float | None = None
    message: str = ""
    started: float | None = None                 # monotonic
    finished: float | None = None
    weight: float = FALLBACK_SECONDS
    review: bool = False                         # this stage ends in a human review
    detail: str = ""
    took: float | None = None                    # duration recorded in the saved state (a run that has ended)
    ran: bool = False                            # it started at some point (a cancelled step that never ran is "not started")

    @property
    def shown_status(self) -> str:
        """The status as a person reads it: a step stopped before it ever began is "not started"."""
        if self.status == "cancelled" and not self.ran and self.started is None:
            return "not-started"
        return self.status

    @property
    def done(self) -> bool:
        return self.status in DONE

    def elapsed(self, now: float) -> float | None:
        if self.started is None:
            return self.took
        return (self.finished if self.finished is not None else now) - self.started

    def credit(self) -> float:
        """Share of this step's weight already accomplished (0..1)."""
        if self.done or self.status == WAITING:     # waiting for a person: the machine's work is finished
            return 1.0
        if self.status == RUNNING and self.fraction is not None:
            return min(max(self.fraction, 0.0), 0.99)
        return 0.0


@dataclass
class Snapshot:
    steps: list[StepProgress]
    done: int
    total: int
    overall: float                               # 0..1 weighted
    overall_eta: float | None
    current: list[StepProgress]
    waiting: list[StepProgress]
    failed: list[StepProgress]
    elapsed: float
    state: str                                   # idle | running | waiting | failed | finished
    paused: bool = False

    def headline(self, name: Callable[[StepProgress], str] | None = None) -> str:
        name = name or (lambda s: s.id)
        if self.paused:
            return "Paused"
        if self.state == "finished":
            return f"All {self.total} steps done"
        if self.state == "failed" and self.failed:
            worst = self.failed[0]                       # already most serious first
            why = {"interrupted": "the run was interrupted; run again to continue",
                   "failed": "it failed", "blocked": "it cannot start", "stale": "it is out of date"}[worst.status]
            return f"Stopped at {name(worst)}: {why}"
        if self.state == "waiting" and self.waiting:
            return f"Waiting for you: {name(self.waiting[0])}"
        if self.current:
            return f"Running {name(self.current[0])}"
        return f"{self.done} of {self.total} steps done"


@dataclass
class ProgressTracker:
    plan: Mapping[str, Any]
    timings: Timings = field(default_factory=lambda: Timings(None))
    clock: Callable[[], float] = time.monotonic
    note: str = ""                               # what the run is doing between steps (e.g. resume checks)

    def __post_init__(self) -> None:
        self.steps: dict[str, StepProgress] = {}
        review_stages = _review_stages()
        for s in self.plan["steps"]:
            stage = s["stage"]
            self.steps[s["id"]] = StepProgress(s["id"], stage, weight=self.timings.expected(stage),
                                               review=stage in review_stages)
        self.started = self.clock()
        self.paused_at: float | None = None

    # ------------------------------------------------------------- inputs
    def load_state(self, state: Mapping[str, Any], *, live: bool = False) -> None:
        """Start from a job's saved state (a resumed run shows what is already done). ``live``: the run that saved
        it is still going in another process, so its "running" steps really are running."""
        for sid, st in (state.get("steps") or {}).items():
            step = self.steps.get(sid)
            if step is None:
                continue
            status = st.get("status", "pending")
            # a saved "running" belongs to a run that is no longer going (the engine marks it interrupted on the next
            # start): showing it as running would make a dead run look alive
            step.status = "interrupted" if (status == RUNNING and not live) else status
            step.ran = bool(st.get("started_utc"))
            step.took = _duration(st) if step.status != RUNNING else None
            if step.status in SETTLED or step.status == WAITING:
                step.detail = "; ".join(st.get("issues") or []) or str(st.get("result_note") or "")

    def on_event(self, event: Mapping[str, Any]) -> None:
        kind, sid = event.get("kind", ""), event.get("step")
        if kind == "paused":
            self.paused_at = self.paused_at or self.clock()
            return
        if kind == "resumed":
            if self.paused_at is not None:               # time spent paused is not time spent working
                gap = self.clock() - self.paused_at
                self.started += gap
                for s in self.steps.values():
                    if s.status == RUNNING and s.started is not None:
                        s.started += gap
                self.paused_at = None
            return
        if kind in ("verifying", "verified"):          # resume check of finished steps (can take a minute)
            self.note = str(event.get("detail", "")) if kind == "verifying" else ""
            return
        if kind == "steps-carried-over":                 # finished in an earlier job and still valid: not redone
            for carried in event.get("steps") or []:
                s = self.steps.get(carried)
                if s is not None:
                    s.status, s.fraction = "passed", 1.0
                    s.detail = f"kept from job {event.get('donor', '')}"
            return
        step = self.steps.get(sid) if sid else None
        if step is None:
            return
        now = self.clock()
        if kind == "step-start":
            step.status, step.started, step.finished, step.fraction = RUNNING, now, None, None
            step.message, step.detail, step.took, step.ran = "", "", None, True
        elif kind == "progress":
            frac = event.get("fraction")
            if step.status != RUNNING:
                step.status, step.started = RUNNING, step.started or now
            step.fraction = None if frac is None else min(max(float(frac), 0.0), 1.0)
            step.message = event.get("message", "") or step.message
        elif kind == "step-kept":                        # an upstream re-run gave the same result: not redone
            step.status, step.fraction = "passed", 1.0
            step.detail = str(event.get("detail", ""))
        elif kind in ("step-passed", "review-satisfied"):
            if kind == "step-passed" and step.started is not None:
                self.timings.record(step.stage, now - step.started)
            step.status, step.fraction, step.finished = "passed", 1.0, now
            if kind == "step-passed":
                step.detail = str(event.get("detail") or "")
        elif kind == "step-awaiting-review":
            if step.started is not None:
                self.timings.record(step.stage, now - step.started)
            step.status, step.finished, step.detail = WAITING, now, str(event.get("detail", ""))
        elif kind == "step-unblocked":
            step.status, step.finished, step.detail, step.fraction = "pending", None, "", None
        elif kind == "space-rechecked":
            step.detail = str(event.get("detail", ""))
        elif kind == "step-waiting":                     # another run holds the heavy slot
            step.detail = step.message = str(event.get("detail", ""))
        elif kind in ("step-failed", "step-blocked", "step-stale", "step-cancelled", "step-interrupted"):
            step.status = kind.removeprefix("step-")
            step.finished = now
            step.detail = "; ".join(event.get("issues") or []) or str(event.get("reason", ""))

    # ------------------------------------------------------------- outputs
    def _eta_step(self, step: StepProgress, now: float) -> float | None:
        if step.status != RUNNING or step.fraction is None:
            return None
        elapsed = step.elapsed(now) or 0.0
        if step.fraction < MIN_FRACTION_FOR_ETA or elapsed < MIN_SECONDS_FOR_ETA:
            return None
        return elapsed * (1.0 - step.fraction) / step.fraction

    def step_eta(self, sid: str) -> float | None:
        return self._eta_step(self.steps[sid], self.clock())

    def snapshot(self) -> Snapshot:
        now = self.paused_at if self.paused_at is not None else self.clock()
        steps = list(self.steps.values())
        total_w = sum(s.weight for s in steps) or 1.0
        overall = sum(s.weight * s.credit() for s in steps) / total_w
        current = [s for s in steps if s.status == RUNNING]
        waiting = [s for s in steps if s.status == WAITING]
        failed = worst_first(steps, lambda s: s.status)
        done = sum(1 for s in steps if s.done)
        if done == len(steps):
            state = "finished"
        elif failed and not current:
            state = "failed"
        elif current:
            state = "running"
        elif waiting:
            state = "waiting"
        else:
            state = "idle"
        return Snapshot(steps, done, len(steps), overall, self._overall_eta(steps, now), current, waiting, failed,
                        now - self.started, state, self.paused_at is not None)

    def _overall_eta(self, steps: list[StepProgress], now: float) -> float | None:
        """Remaining machine time for the whole plan, or None when it cannot be estimated honestly.

        Each unfinished step needs a basis of its own: its running rate (fraction and elapsed time), or the median
        of earlier runs of that stage on this machine. A step with neither makes the whole estimate unknown, and so
        does a failed or blocked step (the run cannot finish as it stands). The time a person spends reviewing is
        not counted. Defaults and other stages' speeds are deliberately NOT used: a fast stage says nothing about a
        slow one (an inventory that takes seconds would make a multi-hour render look nearly done)."""
        if any(s.status in ("failed", "blocked", "stale", "interrupted") for s in steps):
            return None
        todo = [s for s in steps if not s.done and s.status != WAITING]
        if not todo:
            return 0.0
        remaining = 0.0
        for s in todo:
            own = self._eta_step(s, now)
            if own is not None:
                remaining += own
                continue
            median = self.timings.median(s.stage)
            if median is None:
                return None
            elapsed = s.elapsed(now) or 0.0 if s.status == RUNNING else 0.0
            if s.status == RUNNING and s.fraction is not None:
                remaining += median * (1.0 - s.credit())
            elif elapsed >= median:
                return None                              # already slower than ever before: no honest figure
            else:
                remaining += median - elapsed
        return remaining
