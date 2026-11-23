"""Episode queue: run several episodes' work one after another, unattended.

Owner decisions (2026-11-23):
* one heavy job (stack, decode, encode) at a time — enforced across every run by ``engine.HEAVY`` — while
  the next queued episode may already be downloading;
* the queue never stalls: an item that needs a review, is blocked (e.g. disk space) or fails is flagged and
  the queue moves on; **Retry** resumes that item's own job where it stopped;
* the queue is saved to disk (``<data>/queue.json``) and survives a restart: an item that was running goes
  back to queued and resumes its job; a queue that was running starts again when the app opens.

Items are ``(episode, kind)``; the plan is written when the item starts (so a delivery plan sees what intake
measured). Items of one episode run in the order queued, never at the same time: a later item waits until the
earlier ones are done.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fstk.core.errors import FstkError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso

LOG = logging.getLogger("fstk.queue")
SCHEMA = "fstk-queue-v1"
KINDS = {"intake": "Intake (download → stack reference → audio)",
         "delivery": "Delivery (stack → encode → QC)",
         "reencode": "Re-encode with current settings"}
QUEUED, RUNNING, DONE = "queued", "running", "done"
REVIEW, BLOCKED, FAILED, STOPPED, HELD = "needs-review", "blocked", "failed", "stopped", "held"
STATUS_TEXT = {QUEUED: "Queued", RUNNING: "Running", DONE: "Done", REVIEW: "Needs your review",
               BLOCKED: "Blocked", FAILED: "Failed", STOPPED: "Stopped", HELD: "Waiting for an earlier item"}
ATTENTION = (REVIEW, BLOCKED, FAILED, STOPPED)


@dataclass
class Item:
    id: str
    episode: str
    kind: str
    status: str = QUEUED
    plan: str = ""
    job: str = ""
    added_utc: str = ""
    started_utc: str = ""
    finished_utc: str = ""
    detail: str = ""
    # live only (not saved)
    progress: float = field(default=0.0, compare=False)
    step: str = field(default="", compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("id", "episode", "kind", "status", "plan", "job", "added_utc",
                                              "started_utc", "finished_utc", "detail")}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Item:
        return cls(**{k: d.get(k, "") for k in ("id", "episode", "kind", "status", "plan", "job", "added_utc",
                                                 "started_utc", "finished_utc", "detail")})


class QueueStore:
    """The saved queue. Thread-safe; every change is written atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items: list[Item] = []
        self.running = False
        self.sleep_when_done = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = read_json(self.path)
        except (OSError, ValueError) as exc:
            LOG.warning("queue file unreadable, starting empty: %s", exc)
            return
        if data.get("schema") != SCHEMA:
            return
        self.items = [Item.from_dict(d) for d in data.get("items") or []]
        self.running = bool(data.get("running"))
        self.sleep_when_done = bool(data.get("sleep_when_done"))
        for it in self.items:
            if it.status == RUNNING:          # the app stopped while it ran: resume its job next time
                it.status, it.detail = QUEUED, "resumes where it stopped"

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, {"schema": SCHEMA, "running": self.running,
                                          "sleep_when_done": self.sleep_when_done,
                                          "items": [it.to_dict() for it in self.items]})

    # -- edits
    def add(self, episode: str, kind: str) -> Item:
        if kind not in KINDS:
            raise FstkError(f"unknown queue task {kind!r}")
        with self.lock:
            dup = next((i for i in self.items if i.episode == episode and i.kind == kind
                        and i.status in (QUEUED, RUNNING, HELD)), None)
            if dup:
                raise FstkError(f"{episode} {kind} is already in the queue")
            it = Item(secrets.token_hex(4), episode, kind, added_utc=utc_now_iso())
            self.items.append(it)
            self.save()
            return it

    def get(self, item_id: str) -> Item:
        with self.lock:
            for it in self.items:
                if it.id == item_id:
                    return it
        raise FstkError(f"no queue item {item_id}")

    def remove(self, item_id: str) -> None:
        with self.lock:
            it = self.get(item_id)
            if it.status == RUNNING:
                raise FstkError("stop this item before removing it")
            self.items.remove(it)
            self.save()

    def move(self, item_id: str, delta: int) -> None:
        with self.lock:
            it = self.get(item_id)
            i = self.items.index(it)
            j = max(0, min(len(self.items) - 1, i + delta))
            self.items.insert(j, self.items.pop(i))
            self.save()

    def retry(self, item_id: str) -> None:
        with self.lock:
            it = self.get(item_id)
            if it.status in (QUEUED, RUNNING):
                return
            if it.status == DONE:
                it.plan = it.job = ""        # run the task again as a new job
            it.status, it.detail, it.finished_utc = QUEUED, "", ""
            self.save()

    def clear_finished(self) -> None:
        with self.lock:
            self.items = [i for i in self.items if i.status != DONE]
            self.save()

    def position(self, item: Item) -> int:
        with self.lock:
            return self.items.index(item) if item in self.items else len(self.items)

    def eligible(self, active_episodes: set[str]) -> list[Item]:
        """Queued items that may start now, in queue order; marks items held behind an earlier one."""
        out: list[Item] = []
        with self.lock:
            seen: dict[str, bool] = {}            # episode → an earlier item is not done
            for it in self.items:
                earlier_open = seen.get(it.episode, False)
                if it.status in (QUEUED, HELD):
                    if earlier_open or it.episode in active_episodes:
                        if earlier_open and it.status == QUEUED:
                            it.status = HELD
                    else:
                        if it.status == HELD:
                            it.status = QUEUED
                        out.append(it)
                if it.status != DONE:
                    seen[it.episode] = True
        return out


def outcome(state: dict[str, Any], cancelled: bool) -> tuple[str, str]:
    """A finished engine run → (queue status, short detail)."""
    steps = state.get("steps") or {}
    by = {s: st.get("status") for s, st in steps.items()}
    if by and all(v == "passed" for v in by.values()):
        return DONE, f"all {len(by)} steps done"
    if cancelled or any(v == "cancelled" for v in by.values()):
        return STOPPED, "stopped; Retry continues where it stopped"
    for status, label in (("awaiting-review", REVIEW), ("failed", FAILED), ("blocked", BLOCKED)):
        hit = [s for s, v in by.items() if v == status]
        if hit:
            issues = "; ".join(steps[hit[0]].get("issues") or [])[:300]
            return label, f"{hit[0]}" + (f": {issues}" if issues else "")
    return FAILED, "stopped without finishing"


Planner = Callable[[str, str], Path]                       # (episode, kind) → plan path
EngineFactory = Callable[..., Any]                          # (plan_path, events, priority) → Engine


class QueueRunner:
    """Starts queued items in order. Up to ``max_active`` items run at once (default 2: one can download
    while another holds the heavy slot); one item per episode."""

    def __init__(self, store: QueueStore, *, planner: Planner, engine_factory: EngineFactory,
                 timings=None, max_active: int = 2, on_change: Callable[[str], None] | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 on_finished: Callable[[Item], None] | None = None, on_idle: Callable[[], None] | None = None,
                 poll: float = 0.5):
        self.store, self.planner, self.engine_factory = store, planner, engine_factory
        self.timings, self.max_active, self.poll = timings, max_active, poll
        self.on_change, self.on_event, self.on_finished, self.on_idle = on_change, on_event, on_finished, on_idle
        self.active: dict[str, tuple[threading.Thread, Any]] = {}     # item id → (thread, engine or None)
        self.trackers: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._was_busy = False

    # -- control
    @property
    def running(self) -> bool:
        return self.store.running

    def start(self) -> None:
        with self.store.lock:
            self.store.running = True
            self.store.save()
        self._ensure_thread()
        self._changed("")

    def stop(self) -> None:
        """Start nothing new; items already running carry on."""
        with self.store.lock:
            self.store.running = False
            self.store.save()
        self._changed("")

    def cancel(self, item_id: str) -> None:
        with self._lock:
            entry = self.active.get(item_id)
        if entry and entry[1] is not None:
            entry[1].cancel.set()
            with contextlib.suppress(Exception):
                entry[1].resume()

    def cancel_all(self) -> None:
        for item_id in list(self.active):
            self.cancel(item_id)

    def shutdown(self, timeout: float = 120.0) -> bool:
        """Stop the dispatcher and every running item (app closing). The queue's running flag is kept, so it
        starts again next time. Returns False if something did not stop within ``timeout``."""
        self._stop.set()
        self.cancel_all()
        deadline = time.monotonic() + timeout
        for thread, _engine in list(self.active.values()):
            thread.join(max(0.0, deadline - time.monotonic()))
        if self._thread:
            self._thread.join(max(0.0, deadline - time.monotonic()))
        with self.store.lock:
            for it in self.store.items:
                if it.status == STOPPED and it.detail.startswith("stopped"):
                    it.status, it.detail = QUEUED, "resumes where it stopped"
            self.store.save()
        return not any(t.is_alive() for t, _ in self.active.values())

    def busy(self) -> bool:
        return bool(self.active)

    # -- internals
    def _changed(self, item_id: str) -> None:
        if self.on_change:
            with contextlib.suppress(Exception):
                self.on_change(item_id)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="queue-dispatch", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch()
            except Exception:  # never let the dispatcher die silently
                LOG.exception("queue dispatcher error")
            self._stop.wait(self.poll)

    def _dispatch(self) -> None:
        with self._lock:
            for item_id in [k for k, (t, _e) in self.active.items() if not t.is_alive()]:
                self.active.pop(item_id)
            active_eps = set()
            for item_id in self.active:
                with contextlib.suppress(FstkError):
                    active_eps.add(self.store.get(item_id).episode)
            if self.store.running and len(self.active) < self.max_active:
                for it in self.store.eligible(active_eps):
                    if len(self.active) >= self.max_active:
                        break
                    if it.episode in active_eps:
                        continue
                    active_eps.add(it.episode)
                    t = threading.Thread(target=self._run_item, args=(it,), name=f"queue-{it.episode}", daemon=True)
                    self.active[it.id] = (t, None)
                    it.status, it.started_utc, it.detail = RUNNING, utc_now_iso(), "starting"
                    self.store.save()
                    t.start()
                    self._changed(it.id)
            busy = bool(self.active)
        if self._was_busy and not busy and self.store.running and not any(
                i.status in (QUEUED, RUNNING) for i in self.store.items):
            LOG.info("queue finished")
            if self.on_idle:
                with contextlib.suppress(Exception):
                    self.on_idle()
        self._was_busy = busy

    def _run_item(self, it: Item) -> None:
        from fstk.workflow.engine import load_plan
        from fstk.workflow.progress import ProgressTracker

        engine = None
        try:
            if not it.plan or not Path(it.plan).is_file():
                it.plan = str(self.planner(it.episode, it.kind))
            plan = load_plan(it.plan)
            it.job = plan["job"]
            tracker = ProgressTracker(plan, self.timings) if self.timings is not None else ProgressTracker(plan)
            self.trackers[it.id] = tracker

            def events(ev: dict, _it=it, _tr=tracker) -> None:
                _tr.on_event(ev)
                snap = _tr.snapshot()
                _it.progress = snap.overall
                cur = snap.current[0] if snap.current else None
                if cur:
                    _it.step = f"{cur.id}: {cur.message}" if cur.message else cur.id
                elif ev.get("kind") == "step-waiting":
                    _it.step = f"{ev.get('step')}: {ev.get('detail')}"
                else:
                    _it.step = snap.headline()
                if self.on_event:
                    with contextlib.suppress(Exception):
                        self.on_event(_it.id, ev)

            engine = self.engine_factory(it.plan, events, self.store.position(it))
            with self._lock:
                self.active[it.id] = (self.active[it.id][0], engine)
            it.detail = ""
            self.store.save()
            self._changed(it.id)
            LOG.info("queue start %s %s job=%s", it.episode, it.kind, it.job)
            with contextlib.suppress(Exception):
                state_path = engine.state_path
                if Path(state_path).is_file():
                    tracker.load_state(read_json(state_path))
            state = engine.run()
            it.status, it.detail = outcome(state, engine.cancel.is_set())
        except FstkError as exc:
            it.status, it.detail = (STOPPED if engine is not None and engine.cancel.is_set() else BLOCKED), str(exc)
        except Exception as exc:  # unexpected: keep the queue going
            LOG.exception("queue item %s crashed", it.id)
            it.status, it.detail = FAILED, f"internal error: {exc!r}"
        it.finished_utc = utc_now_iso()
        if it.status == DONE:
            it.progress = 1.0
        LOG.info("queue end %s %s → %s %s", it.episode, it.kind, it.status, it.detail)
        self.store.save()
        self._changed(it.id)
        if self.on_finished:
            with contextlib.suppress(Exception):
                self.on_finished(it)
