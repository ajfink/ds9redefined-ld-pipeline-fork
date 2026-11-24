"""Workflow engine: run a plan of stage steps as a DAG with durable, verifiable state.

Plan (``fstk-workflow-plan-v1``)::

    {"schema": ..., "episode": "DS9:S01E20", "job": "s01e20-cadence",
     "steps": [{"id": "probe", "stage": "dvd-probe", "params": {...}, "after": []}, ...]}

Execution rules
* A step runs when every step in ``after`` has passed and (if that step's stage declares a
  ``review``) its approval is current. Ready steps run concurrently up to the profile's
  per-resource limits.
* Gates are evaluated against the manifest immediately before planning the step.
* Space: a step that declares ``SpaceNeed`` runs inside a ledger reservation.
* Commit: after ``verify`` returns no issues, proposed measurements/artifacts are committed
  in one manifest transaction whose evidence includes the step's input and output
  fingerprints. Otherwise nothing is written.
* Resume: a previously passed step is reused only if its recorded input and output
  fingerprints still match; otherwise it reruns and its dependents are invalidated.
* Exit codes are never success: success is ``verify() == []``.

State lives at ``<data_root>/episodes/<S>/<C>/plans/<job>/state.json`` (atomic replace); each
step logs to ``.../logs/<job>/<step>.log``.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import fstk.stages  # noqa: F401  (registers built-in stages)
from fstk.config.profile import Profile
from fstk.core.errors import FstkError, GateError, WorkflowError
from fstk.core.fsio import atomic_write_json, canonical_json, exclusive_lock, read_json
from fstk.core.hashing import Fingerprint, sha256_bytes
from fstk.core.identity import EpisodeId
from fstk.core.timeutil import utc_now_iso
from fstk.evidence.approvals import ApprovalBook
from fstk.manifest.store import ManifestStore
from fstk.stages.base import (
    RESOURCES,
    StageContext,
    StepOutput,
    StepPlan,
    get_stage,
    input_scope_for,
    output_scope_for,
)
from fstk.tools.process import PAUSE
from fstk.workspace.space import SpaceLedger
from fstk.workspace.volumes import hint_for_space

LOG = logging.getLogger("fstk.engine")
PLAN_SCHEMA = "fstk-workflow-plan-v1"
STATE_SCHEMA = "fstk-workflow-state-v1"
TERMINAL = ("passed", "failed", "cancelled", "blocked")

EventSink = Callable[[dict[str, Any]], None]


@dataclass
class EpisodeWorkspace:
    profile: Profile
    episode: EpisodeId

    @property
    def state_dir(self) -> Path:
        return self.profile.episode_state_dir(self.episode)

    @property
    def store(self) -> ManifestStore:
        return ManifestStore(self.state_dir / "manifest")

    @property
    def approvals(self) -> ApprovalBook:
        return ApprovalBook(self.state_dir / "approvals")

    def job_dir(self, job: str) -> Path:
        return self.state_dir / "plans" / job

    def log_dir(self, job: str) -> Path:
        return self.state_dir / "logs" / job


# ------------------------------------------------------------------------------ plan
def validate_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != PLAN_SCHEMA:
        raise WorkflowError(f"plan schema must be {PLAN_SCHEMA}")
    job = str(plan.get("job") or "")
    if not job or not all(c.isalnum() or c in "-_." for c in job):
        raise WorkflowError("plan.job must be a non-empty [A-Za-z0-9._-] identifier")
    EpisodeId.parse(plan.get("episode", ""))
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("plan.steps must be a non-empty list")
    ids = [s.get("id") for s in steps]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise WorkflowError("step ids must be unique non-empty strings")
    for s in steps:
        get_stage(s.get("stage", ""))
        if not isinstance(s.get("params", {}), dict):
            raise WorkflowError(f"step {s['id']}: params must be an object")
        for dep in s.get("after", []):
            if dep not in ids:
                raise WorkflowError(f"step {s['id']} depends on unknown step {dep!r}")
    # cycle check (Kahn)
    indeg = {s["id"]: len(s.get("after", [])) for s in steps}
    children: dict[str, list[str]] = {i: [] for i in ids}
    for s in steps:
        for dep in s.get("after", []):
            children[dep].append(s["id"])
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if seen != len(ids):
        raise WorkflowError("plan has a dependency cycle")


def plan_digest(plan: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(plan))


def _descendants(plan: dict[str, Any], step_id: str) -> set[str]:
    out: set[str] = set()
    frontier = [step_id]
    while frontier:
        cur = frontier.pop()
        for s in plan["steps"]:
            if cur in s.get("after", []) and s["id"] not in out:
                out.add(s["id"])
                frontier.append(s["id"])
    return out


# ------------------------------------------------------------------------------ engine
HEAVY_RESOURCES = ("compute", "gpu")


class HeavyGate:
    """One heavy job at a time across every plan this app runs (owner decision 2026-11-23).

    A run that holds the gate may run its own compute and GPU steps under its own limits; every other run
    waits until it has none left. Downloads and prep steps never wait here, so the next episode can download
    while this one stacks or encodes. When the gate frees, the waiting run with the lowest ``priority``
    (its queue position) gets it next."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: object | None = None
        self._label = ""
        self._count = 0
        self._waiting: dict[object, int] = {}

    def try_acquire(self, owner: object, priority: int = 1_000_000, label: str = "") -> bool:
        with self._lock:
            if self._holder not in (None, owner):
                self._waiting[owner] = priority
                return False
            if self._holder is None and any(p < priority for o, p in self._waiting.items() if o is not owner):
                self._waiting[owner] = priority
                return False
            self._waiting.pop(owner, None)
            self._holder, self._label = owner, label
            self._count += 1
            return True

    def release(self, owner: object) -> None:
        with self._lock:
            if self._holder is owner:
                self._count -= 1
                if self._count <= 0:
                    self._holder, self._count, self._label = None, 0, ""

    def forget(self, owner: object) -> None:
        with self._lock:
            self._waiting.pop(owner, None)
            if self._holder is owner:
                self._holder, self._count, self._label = None, 0, ""

    @property
    def holder_label(self) -> str:
        with self._lock:
            return self._label


HEAVY = HeavyGate()


class Engine:
    def __init__(self, profile: Profile, plan: dict[str, Any], *, actor: str,
                 events: EventSink | None = None, tool_overrides: dict[str, Any] | None = None,
                 ledger: SpaceLedger | None = None, priority: int = 1_000_000):
        validate_plan(plan)
        self.profile = profile
        self.plan = plan
        self.actor = actor
        self.episode = EpisodeId.parse(plan["episode"])
        self.ws = EpisodeWorkspace(profile, self.episode)
        self.job = plan["job"]
        self.state_path = self.ws.job_dir(self.job) / "state.json"
        self.lock_path = self.ws.job_dir(self.job) / ".job.lock"
        self.events = events
        self.tool_overrides = tool_overrides or {}
        self.ledger = ledger or SpaceLedger.for_profile(profile)
        self.cancel = threading.Event()
        self.priority = priority
        self._waiting_heavy: set[str] = set()
        self._progress_logged: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self.state: dict[str, Any] = {}

    # ---------------------------------------------------------------- pause / resume
    def pause(self) -> None:
        """Freeze running tools where they are and start no new step until ``resume``."""
        if not PAUSE.is_paused:
            PAUSE.pause()
            self._emit("paused")

    def resume(self) -> None:
        if PAUSE.is_paused:
            PAUSE.resume()
            self._emit("resumed")

    # ---------------------------------------------------------------- disk-space recheck
    @staticmethod
    def blocked_by_space(st: dict[str, Any]) -> bool:
        return st.get("status") == "blocked" and any("SPACE FAIL" in str(i) for i in st.get("issues") or [])

    def recheck_space(self) -> dict[str, Any]:
        """Re-measure free space for every step blocked by the disk-space check; a step that now fits goes back to
        pending (a running engine launches it at once; otherwise the next run does). Returns {step: SpaceCheck}.
        Steps blocked for any other reason are left alone."""
        results: dict[str, Any] = {}
        for s in self.plan["steps"]:
            sid = s["id"]
            st = self.state["steps"][sid]
            if not self.blocked_by_space(st):
                continue
            stage = get_stage(s["stage"])
            ctx = StageContext(self.profile, self.episode, self.ws.store, self.actor, cancel=self.cancel,
                               log_dir=self.ws.log_dir(self.job) / sid, tool_overrides=self.tool_overrides)
            try:
                splan = stage.plan(ctx, dict(s.get("params") or {}))
            except FstkError as exc:
                self._emit("space-rechecked", step=sid, detail=f"cannot re-plan: {exc}", ok=False)
                continue
            if splan.space is None:
                continue
            target = splan.space.target or ctx.root(splan.space.root)
            check = self.ledger.check(target, splan.space.planned_gib)
            check = replace(check, was_free_bytes=self._last_free_bytes(st),
                                        measured_at=time.strftime("%H:%M:%S"))
            st["space_checked"] = {"free_bytes": check.free_bytes, "at": utc_now_iso()}
            results[sid] = check
            if check.ok:
                with self._state_lock:
                    st.update(status="pending", issues=[])
                    st.pop("finished_utc", None)
                self._emit("step-unblocked", step=sid, detail=check.describe())
            else:
                with self._state_lock:
                    st["issues"] = [check.describe() + " — make space or move the work root; the reserve is never "
                                    "lowered and nothing is deleted without the operator's authorization. "
                                    + hint_for_space(check.reserve_bytes / 2**30, target)]
                self._emit("space-rechecked", step=sid, detail=st["issues"][0], ok=False)
        self._save()
        return results

    @staticmethod
    def _last_free_bytes(st: dict[str, Any]) -> int | None:
        """Free bytes at the previous measurement: the recheck's own record, else the figure in the blocking message."""
        last = st.get("space_checked")
        if isinstance(last, dict) and isinstance(last.get("free_bytes"), int):
            return last["free_bytes"]
        for issue in st.get("issues") or []:
            m = re.search(r"free=([\d.]+) GiB", str(issue))
            if m:
                return round(float(m.group(1)) * 2**30)
        return None

    def recheck_space_offline(self) -> dict[str, Any]:
        """The same recheck when no run is active here: edits the saved job state under the job lock."""
        with exclusive_lock(self.lock_path, purpose=f"recheck-space:{self.job}"):
            self._load_or_init()
            return self.recheck_space()

    # ---------------------------------------------------------------- state
    def _emit(self, kind: str, **data) -> None:
        self._log_event(kind, data)
        if self.events:
            with contextlib.suppress(Exception):  # observers never break a run
                self.events({"kind": kind, "utc": utc_now_iso(), "job": self.job, **data})

    def _log_event(self, kind: str, data: dict) -> None:
        """Every event goes to the application log; progress only every 30 s per step (it is frequent)."""
        with contextlib.suppress(Exception):
            sid = data.get("step", "")
            if kind == "progress":
                now = time.monotonic()
                last = self._progress_logged.get(sid, -1e9)
                if now - last < 30:
                    return
                self._progress_logged[sid] = now
            detail = {k: v for k, v in data.items() if k not in ("step",)}
            level = logging.WARNING if kind in ("step-failed", "step-blocked", "tool-version-drift") else logging.INFO
            LOG.log(level, "%s job=%s step=%s %s", kind, self.job, sid, detail if detail else "")

    def _preflight(self) -> None:
        """Every tool the plan's steps use must resolve and run before anything starts (``workflow.preflight``)."""
        from fstk.core.errors import PreflightError
        from fstk.workflow import preflight

        warnings: list[str] = []
        problems = preflight.tool_problems(self.profile, preflight.plan_tools(self.plan), skip=self.tool_overrides,
                                           warnings=warnings)
        if warnings:
            self._emit("preflight-warning", issues=warnings)
        if problems:
            self._emit("preflight-failed", issues=problems)
            raise PreflightError(preflight.describe(problems))

    def _record_tool_versions(self) -> None:
        """Trace which tool/library versions this run used; report drift (never blocks a run)."""
        from fstk.tools.requirements import record_versions

        with contextlib.suppress(Exception):
            drift = record_versions(self.profile, self.profile.root("reports", self.episode))
            if drift:
                self._emit("tool-version-drift", changes=drift)

    def _save(self) -> None:
        with self._state_lock:
            atomic_write_json(self.state_path, self.state)

    def _load_or_init(self) -> None:
        digest = plan_digest(self.plan)
        fresh = {"schema": STATE_SCHEMA, "job": self.job, "episode": self.episode.canonical,
                 "plan_digest": digest, "created_utc": utc_now_iso(),
                 "steps": {s["id"]: {"status": "pending", "stage": s["stage"]} for s in self.plan["steps"]}}
        if not self.state_path.exists():
            self.state = fresh
            return
        old = read_json(self.state_path)
        if old.get("schema") != STATE_SCHEMA or old.get("plan_digest") != digest:
            raise WorkflowError(f"existing state for job {self.job!r} belongs to a different plan; "
                                "use a new job id (state is never silently discarded)")
        self.state = old
        for st in self.state["steps"].values():
            if st.get("status") == "running":
                st["status"] = "interrupted"

    def _step(self, sid: str) -> dict[str, Any]:
        return next(s for s in self.plan["steps"] if s["id"] == sid)

    # ---------------------------------------------------------------- resume checks
    def _still_valid(self, sid: str) -> tuple[bool, str]:
        st = self.state["steps"][sid]
        for group in ("inputs", "outputs"):
            for fp in st.get(group, []):
                if not Fingerprint.from_dict(fp).matches(trust_mtime=False):
                    return False, f"{group[:-1]} changed: {fp['path']}"
        return True, "fingerprints match"

    def _review_ok(self, sid: str) -> tuple[bool, str]:
        stage = get_stage(self._step(sid)["stage"])
        if not stage.review:
            return True, ""
        outputs = [Path(fp["path"]) for fp in self.state["steps"][sid].get("outputs", [])]
        status = self.ws.approvals.status(stage.review, manifest=self.ws.store.load(),
                                          expected_evidence=outputs)
        return status.satisfied, f"review {stage.review}: {status.state} {'; '.join(status.reasons)}"

    def _rejected_here(self, sid: str) -> bool:
        """The latest decision on this step's review rejected the result this step is holding (recorded after
        the step finished, on its evidence). The delivery review is left alone: its own windows start the
        reruns (corrections, re-encodes)."""
        stage = get_stage(self._step(sid)["stage"])
        if not stage.review or stage.review == "delivery":
            return False
        rows = self.ws.approvals.history(stage.review)
        latest = rows[-1] if rows else None
        if not latest or latest.get("decision") != "REJECTED":
            return False
        st = self.state["steps"][sid]
        outputs = {fp["path"] for fp in st.get("outputs", [])}
        bound = {fp.get("path") for fp in (latest.get("bindings") or {}).get("evidence", [])}
        return bool(outputs) and outputs <= bound and str(latest.get("utc", "")) >= str(st.get("finished_utc", ""))

    # ---------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        from fstk.workspace.shared import frozen_reason, try_publish

        frozen = frozen_reason(self.profile, self.episode)
        if frozen:
            raise WorkflowError(frozen)
        self._preflight()
        PAUSE.resume()
        try:
            return self._run_locked()
        finally:
            PAUSE.resume()
            if self.profile.shared_root:
                self._emit("shared-publish", outcome=try_publish(self.profile, self.episode))

    def _run_locked(self) -> dict[str, Any]:
        with exclusive_lock(self.lock_path, purpose=f"job:{self.job}"):
            self._load_or_init()
            self._record_tool_versions()
            plan_copy = self.ws.job_dir(self.job) / "plan.json"
            if not plan_copy.exists():
                atomic_write_json(plan_copy, self.plan)
            self._reconcile()
            self._save()
            self._schedule()
            self._save()
        return self.state

    def _reconcile(self) -> None:
        """Re-verify passed steps; invalidate anything downstream of a changed step."""
        invalid: set[str] = set()
        for s in self.plan["steps"]:
            st = self.state["steps"][s["id"]]
            if st["status"] == "passed":
                ok, why = self._still_valid(s["id"])
                if not ok:
                    invalid.add(s["id"])
                    st.update(status="stale", stale_reason=why)
                    self._emit("step-stale", step=s["id"], reason=why)
        for sid in list(invalid):
            for d in _descendants(self.plan, sid):
                if self.state["steps"][d]["status"] in ("passed", "awaiting-review"):
                    self.state["steps"][d].update(status="stale", stale_reason=f"upstream {sid} changed")
        for st in self.state["steps"].values():
            if st["status"] in ("failed", "cancelled", "interrupted", "stale", "blocked", "awaiting-review"):
                st["previous_status"] = st["status"]
                st["status"] = "pending" if st["status"] != "awaiting-review" else "awaiting-review"

    def _ready(self) -> list[str]:
        ready = []
        for s in self.plan["steps"]:
            st = self.state["steps"][s["id"]]
            if st["status"] == "awaiting-review":
                ok, _why = self._review_ok(s["id"])
                if ok:
                    self._commit_pending(s["id"])
                    st["status"] = "passed"
                    self._emit("review-satisfied", step=s["id"])
                    continue
                if not self._rejected_here(s["id"]):
                    continue
                # a person rejected exactly this result: do the step again (its values were never committed)
                st.pop("pending_commit", None)
                st.update(status="pending", previous_status="rejected")
                self._emit("review-rejected-redo", step=s["id"])
            if st["status"] != "pending":
                continue
            deps_ok = True
            for dep in s.get("after", []):
                if self.state["steps"][dep]["status"] != "passed":
                    deps_ok = False
                    break
            if deps_ok:
                ready.append(s["id"])
        return ready

    def _schedule(self) -> None:
        limits = {r: self.profile.limits.resources.get(r, 1) for r in RESOURCES}
        busy = {r: 0 for r in RESOURCES}
        running: dict[Future, tuple[str, str | None]] = {}
        with ThreadPoolExecutor(max_workers=max(1, sum(limits.values()) + 1)) as pool:
            try:
                while True:
                    if self.cancel.is_set():
                        break
                    launched = False
                    held_back = PAUSE.is_paused and bool(self._ready())
                    for sid in ([] if PAUSE.is_paused else self._ready()):
                        if any(sid == v[0] for v in running.values()):
                            continue
                        res = get_stage(self._step(sid)["stage"]).resource
                        if res and busy[res] >= limits[res]:
                            continue
                        if res in HEAVY_RESOURCES and not HEAVY.try_acquire(self, self.priority,
                                                                             f"{self.episode} {sid}"):
                            held_back = True
                            if sid not in self._waiting_heavy:
                                self._waiting_heavy.add(sid)
                                self._emit("step-waiting", step=sid,
                                           detail=f"waiting for {HEAVY.holder_label or 'another run'} "
                                                  "(one heavy job at a time)")
                            continue
                        self._waiting_heavy.discard(sid)
                        if res:
                            busy[res] += 1
                        self.state["steps"][sid].update(status="running", started_utc=utc_now_iso())
                        self._save()
                        self._emit("step-start", step=sid)
                        running[pool.submit(self._execute, sid)] = (sid, res)
                        launched = True
                    if not running:
                        if launched:
                            continue
                        if held_back:
                            time.sleep(0.5)        # paused, or another run has the heavy slot: keep waiting
                            continue
                        break
                    done, _ = wait(list(running), return_when=FIRST_COMPLETED, timeout=0.5)
                    for fut in done:
                        sid, res = running.pop(fut)
                        if res:
                            busy[res] -= 1
                        if res in HEAVY_RESOURCES:
                            HEAVY.release(self)
                        fut.result()  # _execute never raises
                for fut in running:
                    fut.result()
                    sid, res = running[fut]
                    if res in HEAVY_RESOURCES:
                        HEAVY.release(self)
            finally:
                HEAVY.forget(self)
        # mark unreachable steps
        for s in self.plan["steps"]:
            st = self.state["steps"][s["id"]]
            if st["status"] == "pending":
                blockers = [d for d in s.get("after", []) if self.state["steps"][d]["status"] != "passed"]
                st.update(status="blocked" if not self.cancel.is_set() else "cancelled",
                          blocked_by=blockers)

    def _execute(self, sid: str) -> None:
        step = self._step(sid)
        stage = get_stage(step["stage"])
        st = self.state["steps"][sid]
        log_dir = self.ws.log_dir(self.job) / sid
        ctx = StageContext(self.profile, self.episode, self.ws.store, self.actor, cancel=self.cancel,
                           progress=lambda msg, frac: self._emit("progress", step=sid, message=msg,
                                                                 fraction=frac),
                           log_dir=log_dir, tool_overrides=self.tool_overrides)
        try:
            stage.assert_gates(ctx)
            plan = stage.plan(ctx, dict(step.get("params") or {}))
            inputs = [Fingerprint.of(p, scope=input_scope_for(Path(p), plan.input_scope)).to_dict()
                      for p in plan.inputs]
            st["inputs"] = inputs
            st["plan_summary"] = plan.describe()
            self._save()
            output = self._run_with_space(ctx, stage, plan)
            stage.check_writes(output)
            issues = stage.verify(ctx, plan, output)
            if issues:
                st.update(status="failed", finished_utc=utc_now_iso(), issues=issues)
                self._emit("step-failed", step=sid, issues=issues)
                return
            outputs = [Fingerprint.of(p, scope=output_scope_for(Path(p), plan.output_scope)).to_dict()
                       for p in plan.outputs if Path(p).is_file()]
            missing = [str(p) for p in plan.outputs if not Path(p).is_file()]
            if missing:
                st.update(status="failed", finished_utc=utc_now_iso(),
                          issues=[f"declared output missing: {m}" for m in missing])
                self._emit("step-failed", step=sid, issues=st["issues"])
                return
            st.update(outputs=outputs, finished_utc=utc_now_iso(), issues=[],
                      report=str(output.report) if output.report else None)
            if stage.review and output.needs_review:
                # Values from a reviewed stage are committed only once a human approves the
                # evidence (v38 recorded cadence/trim metrics only after approval).
                st["pending_commit"] = {
                    "measurements": [{"key": m.key, "value": m.value, "evidence": m.evidence, "note": m.note}
                                     for m in output.measurements],
                    "artifacts": output.artifacts, "sources_update": output.sources_update}
                ok, why = self._review_ok(sid)
                st["review_detail"] = why
                if ok:
                    self._commit_pending(sid)
                    st["status"] = "passed"
                    self._emit("step-passed", step=sid)
                else:
                    st["status"] = "awaiting-review"
                    self._emit("step-awaiting-review", step=sid, detail=why)
            else:
                self._commit(stage, sid, plan, output, inputs, outputs)
                st["committed"] = [m.key for m in output.measurements]
                if stage.review:
                    st["review_detail"] = f"review {stage.review}: not needed (all automatic checks passed)"
                st["status"] = "passed"
                self._emit("step-passed", step=sid)
        except GateError as exc:
            st.update(status="blocked", finished_utc=utc_now_iso(), issues=[str(exc)])
            self._emit("step-blocked", step=sid, issues=[str(exc)])
        except FstkError as exc:
            status = "cancelled" if self.cancel.is_set() else "failed"
            st.update(status=status, finished_utc=utc_now_iso(), issues=[str(exc)])
            self._emit(f"step-{status}", step=sid, issues=[str(exc)])
        except Exception as exc:  # unexpected: keep the traceback in state for diagnosis
            st.update(status="failed", finished_utc=utc_now_iso(),
                      issues=[f"internal error: {exc!r}"], traceback=traceback.format_exc()[-4000:])
            self._emit("step-failed", step=sid, issues=st["issues"])
        finally:
            self._save()

    def _run_with_space(self, ctx: StageContext, stage, plan: StepPlan) -> StepOutput:
        if plan.space is None:
            return stage.run(ctx, plan)
        target = plan.space.target or ctx.root(plan.space.root)
        with self.ledger.reserve(target, plan.space.planned_gib, label=f"{self.job}:{stage.id}"):
            return stage.run(ctx, plan)

    def _commit_pending(self, sid: str) -> None:
        st = self.state["steps"][sid]
        pending = st.pop("pending_commit", None)
        if not pending or not (pending["measurements"] or pending["artifacts"] or pending.get("sources_update")):
            return
        stage = get_stage(self._step(sid)["stage"])
        base = [{"kind": "step", "job": self.job, "step": sid}, *st.get("inputs", []),
                *st.get("outputs", []), *_approval_evidence(self, sid)]
        with self.ws.store.transaction(by=self.actor, reason=f"{stage.id} approved", stage=stage.id) as m:
            for meas in pending["measurements"]:
                m.set(meas["key"], meas["value"], by=f"stage:{stage.id}", stage=stage.id,
                      evidence=[*base, *meas["evidence"]], note=meas["note"])
            for key, spec in pending["artifacts"].items():
                m.set_artifact(key, **spec)
            if pending.get("sources_update"):
                m.set_sources({**m.doc["sources"], **pending["sources_update"]})
        st["committed"] = [x["key"] for x in pending["measurements"]]

    def _commit(self, stage, sid: str, plan: StepPlan, output: StepOutput,
                inputs: list[dict], outputs: list[dict]) -> None:
        if not output.measurements and not output.artifacts and not output.sources_update:
            return
        base_evidence = [{"kind": "step", "job": self.job, "step": sid}, *inputs, *outputs]
        with self.ws.store.transaction(by=self.actor, reason=f"{stage.id} verified", stage=stage.id) as m:
            for meas in output.measurements:
                m.set(meas.key, meas.value, by=f"stage:{stage.id}", stage=stage.id,
                      evidence=[*base_evidence, *meas.evidence], note=meas.note)
            for key, spec in output.artifacts.items():
                m.set_artifact(key, **spec)
            if output.sources_update:
                m.set_sources({**m.doc["sources"], **output.sources_update})


def _approval_evidence(engine: Engine, sid: str) -> list[dict]:
    stage = get_stage(engine._step(sid)["stage"])
    status = engine.ws.approvals.status(stage.review, manifest=engine.ws.store.load())
    rec = status.record or {}
    return [{"kind": "approval", "review": stage.review, "path": rec.get("_path"),
             "actor": rec.get("actor"), "utc": rec.get("utc")}]


def load_plan(path: str | Path) -> dict[str, Any]:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_plan(plan)
    return plan
