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
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fstk.core.errors import FstkError
from fstk.core.fsio import LockHeldError, atomic_write_json, exclusive_lock, move_aside, read_json, read_lock
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
    """The saved queue. Thread-safe; every change is written atomically.

    Two windows may share one data folder: each save first merges what the other wrote (new items, removals,
    status changes of items this window did not touch), so neither overwrites the other. A damaged file is moved
    aside (``queue.json.bad-<time>``), never overwritten, and the previous good version is kept as ``queue.json.bak``.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items: list[Item] = []
        self.running = False
        self.sleep_when_done = False
        self._sig: tuple[int, int] | None = None          # file stamp when we last read or wrote it
        self._synced: dict[str, dict[str, Any]] = {}      # id → saved fields as of that moment
        self._flags: tuple[bool, bool] = (False, False)   # (running, sleep_when_done) as of that moment
        self._load()

    @property
    def runner_lock_path(self) -> Path:
        return self.path.with_name("queue-runner.lock")

    def other_runner(self) -> str | None:
        """Who runs the queue in another process (``pid 1234``), else None."""
        info = read_lock(self.runner_lock_path)
        if info is not None and info.pid != os.getpid() and info.local_process_alive is not False:
            return f"pid {info.pid}" if info.pid else "another process"
        return None

    def _signature(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _parse(self) -> dict[str, Any] | None:
        """The saved queue, or None. A file that cannot be read is moved aside so the next save keeps it."""
        if not self.path.is_file():
            return None
        try:
            data = read_json(self.path)
            if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("items", []), list):
                raise ValueError("not a queue file")
            return data
        except (OSError, ValueError) as exc:
            kept = move_aside(self.path)
            LOG.warning("queue file unreadable (%s); starting empty, the damaged file is kept as %s", exc, kept)
            return None

    def _load(self) -> None:
        data = self._parse()
        self._sig = self._signature()
        if data is None:
            return
        self.items = [Item.from_dict(d) for d in data.get("items") or [] if isinstance(d, dict)]
        self.running = bool(data.get("running"))
        self.sleep_when_done = bool(data.get("sleep_when_done"))
        self._synced = {it.id: it.to_dict() for it in self.items}
        self._flags = (self.running, self.sleep_when_done)
        if self.other_runner() is None:
            for it in self.items:
                if it.status == RUNNING:          # the app stopped while it ran: resume its job next time
                    it.status, it.detail = QUEUED, "resumes where it stopped"

    def sync(self) -> bool:
        """Merge changes another window saved. True if anything here changed."""
        with self.lock:
            sig = self._signature()
            if sig == self._sig:
                return False
            data = self._parse()
            self._sig = self._signature()
            if data is None:
                return False
            disk = {d["id"]: d for d in data.get("items") or [] if isinstance(d, dict) and d.get("id")}
            mem = {i.id: i for i in self.items}
            changed = False
            for iid, d in disk.items():
                it = mem.get(iid)
                if it is None:
                    if iid not in self._synced:                                  # added by the other window
                        self.items.append(Item.from_dict(d))
                        changed = True
                elif it.to_dict() == self._synced.get(iid) and Item.from_dict(d).to_dict() != it.to_dict():
                    theirs = Item.from_dict(d)                                   # they changed it, we did not
                    for k in ("status", "plan", "job", "added_utc", "started_utc", "finished_utc", "detail"):
                        setattr(it, k, getattr(theirs, k))
                    changed = True
            for iid, it in list(mem.items()):
                if iid not in disk and iid in self._synced and it.to_dict() == self._synced[iid] \
                        and it.status != RUNNING:
                    self.items.remove(it)                                        # removed by the other window
                    changed = True
            if self._flags == (self.running, self.sleep_when_done):
                theirs_flags = (bool(data.get("running")), bool(data.get("sleep_when_done")))
                if theirs_flags != self._flags:
                    self.running, self.sleep_when_done = theirs_flags
                    changed = True
            self._synced = {k: Item.from_dict(d).to_dict() for k, d in disk.items()}
            self._flags = (bool(data.get("running")), bool(data.get("sleep_when_done")))
            return changed

    def save(self) -> None:
        with self.lock:
            self.sync()
            atomic_write_json(self.path, {"schema": SCHEMA, "running": self.running,
                                          "sleep_when_done": self.sleep_when_done,
                                          "items": [it.to_dict() for it in self.items]}, backup=True)
            self._sig = self._signature()
            self._synced = {it.id: it.to_dict() for it in self.items}
            self._flags = (self.running, self.sleep_when_done)

    def drop_episode(self, episode: str) -> int:
        """Remove every waiting item of an episode (it was removed from this machine). Running items stay."""
        with self.lock:
            keep = [i for i in self.items if i.episode != episode or i.status == RUNNING]
            n = len(self.items) - len(keep)
            if n:
                self.items = keep
                self.save()
            return n

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
        self._runner_lock: Any = None          # the queue-runner lock while this process runs the queue

    # -- one runner per data folder
    def _acquire_runner_lock(self) -> None:
        """Only one window runs the queue; a second one would start the same items twice."""
        if self._runner_lock is not None:
            return
        path = self.store.runner_lock_path
        for attempt in (1, 2):
            cm = exclusive_lock(path, purpose="queue-runner")
            try:
                cm.__enter__()
            except LockHeldError as exc:
                if attempt == 1 and exc.info.local_process_alive is False:
                    LOG.warning("removing the queue-runner lock of a process that is gone: %s", exc.info.pid)
                    path.unlink(missing_ok=True)
                    continue
                who = f"pid {exc.info.pid}" if exc.info.pid else "another process"
                raise FstkError(f"The queue is already being run by another FromStunToKill window ({who}). "
                                "Use that window, or close it first.") from None
            self._runner_lock = cm
            return

    def _release_runner_lock(self) -> None:
        cm, self._runner_lock = self._runner_lock, None
        if cm is not None:
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

    def other_runner(self) -> str | None:
        """Who runs the queue in another process, else None."""
        return None if self._runner_lock is not None else self.store.other_runner()

    # -- control
    @property
    def running(self) -> bool:
        return self.store.running

    def start(self) -> None:
        self._acquire_runner_lock()
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
        stopped = not any(t.is_alive() for t, _ in self.active.values())
        if stopped:
            self._release_runner_lock()
        return stopped

    def busy(self) -> bool:
        with self._lock:
            return any(t.is_alive() for t, _e in self.active.values())

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
            if not busy and not self.store.running:
                self._release_runner_lock()
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
