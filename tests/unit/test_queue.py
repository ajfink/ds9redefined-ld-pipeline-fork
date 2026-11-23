"""Episode queue: persistence, ordering, held items, never stalling, restart."""
from __future__ import annotations

import json
import time

import pytest

import tests.unit.test_engine as te  # noqa: F401  (registers the t-* stages)
from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace
from fstk.workflow.queue import DONE, HELD, QUEUED, REVIEW, QueueRunner, QueueStore

EPS = ("DS9:S01E20", "DS9:S01E21")


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    for e in EPS:
        ep = EpisodeId.parse(e)
        EpisodeWorkspace(prof, ep).store.create(Manifest.new(ep, title="t"), by="t", reason="setup")
    return prof, tmp_path


def make_runner(env, store, **kw):
    prof, tmp = env
    counter = {"n": 0}

    def planner(episode, kind):
        counter["n"] += 1
        stage = {"intake": "t-compute", "delivery": "t-compute", "reencode": "t-review"}[kind]
        job = f"{kind}-{counter['n']}"
        path = tmp / f"{job}.plan.json"
        path.write_text(json.dumps({"schema": PLAN_SCHEMA, "episode": episode, "job": job,
                                    "steps": [{"id": "a", "stage": stage, "params": {}, "after": []}]}))
        return path

    def factory(plan_path, events, priority):
        from fstk.workflow.engine import load_plan

        return Engine(prof, load_plan(plan_path), actor="q", events=events, priority=priority)

    return QueueRunner(store, planner=planner, engine_factory=factory, poll=0.05, **kw)


def wait_for(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_store_edits_persist_and_running_items_resume_after_restart(tmp_path):
    store = QueueStore(tmp_path / "queue.json")
    a = store.add("DS9:S01E20", "intake")
    b = store.add("DS9:S01E21", "intake")
    with pytest.raises(FstkError, match="already in the queue"):
        store.add("DS9:S01E20", "intake")
    store.move(b.id, -1)
    assert [i.id for i in store.items] == [b.id, a.id]
    a.status = "running"
    store.running = True
    store.save()
    again = QueueStore(tmp_path / "queue.json")
    assert again.running is True
    assert [i.status for i in again.items] == [QUEUED, QUEUED]       # the running item goes back to queued
    again.remove(b.id)
    assert [i.id for i in QueueStore(tmp_path / "queue.json").items] == [a.id]


def test_queue_runs_in_order_holds_later_items_and_never_stalls(env):
    prof, tmp = env
    store = QueueStore(tmp / "queue.json")
    first = store.add("DS9:S01E20", "reencode")           # ends waiting for a review
    held = store.add("DS9:S01E20", "delivery")            # same episode: waits for the item before it
    other = store.add("DS9:S01E21", "intake")             # another episode: not held up
    finished = []
    runner = make_runner(env, store, on_finished=finished.append)
    runner.start()
    assert wait_for(lambda: first.status == REVIEW and other.status == DONE)
    assert held.status == HELD                             # never ran past the review
    assert "a" in first.detail
    runner.stop()
    runner.shutdown(5)
    assert {i.id for i in finished} == {first.id, other.id}


def test_retry_after_the_review_continues_the_same_job(env):
    prof, tmp = env
    store = QueueStore(tmp / "queue.json")
    it = store.add("DS9:S01E21", "intake")
    runner = make_runner(env, store)
    runner.start()
    assert wait_for(lambda: it.status == DONE)
    job = it.job
    store.retry(it.id)                                     # a finished item runs again as a new job
    assert it.status == QUEUED and it.plan == ""
    assert wait_for(lambda: it.status == DONE and it.job != job)
    runner.shutdown(5)


def test_stop_starts_nothing_new(env):
    prof, tmp = env
    store = QueueStore(tmp / "queue.json")
    runner = make_runner(env, store)
    runner.start()
    runner.stop()
    it = store.add("DS9:S01E20", "intake")
    time.sleep(0.4)
    assert it.status == QUEUED
    runner.start()
    assert wait_for(lambda: it.status == DONE)
    runner.shutdown(5)


def test_queue_tab_adds_runs_and_reports(env, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from fstk.ui.queue_panel import QueuePanel

    app = QApplication.instance() or QApplication([])
    prof, tmp = env
    store = QueueStore(tmp / "queue.json")
    runner = make_runner(env, store)
    notes = []
    panel = QueuePanel(runner, current_episode=lambda: "DS9:S01E21", episodes=lambda: list(EPS),
                       notify=lambda title, text: notes.append(title), sleep=lambda: notes.append("slept"))
    panel._fill_add_menu()
    labels = [a.text() for a in panel.add_menu.actions() if a.text()]
    assert labels[0] == "DS9:S01E21: intake, then delivery" and labels[-1] == "Several episodes…"
    panel.add("DS9:S01E21", ("intake", "delivery"))
    assert [i.kind for i in store.items] == ["intake", "delivery"] and panel.table.rowCount() == 2
    panel.sleep_box.setChecked(True)
    panel.toggle_running()
    assert panel.start_button.text() == "Stop queue"

    def settled():
        app.processEvents()
        return all(i.status == DONE for i in store.items) and "Queue finished" in notes

    assert wait_for(settled)
    assert panel.table.item(1, 3).text() == "Done" and panel.sleep_timer.isActive()   # sleeps 1 minute later
    panel.sleep_box.setChecked(False)
    assert not panel.sleep_timer.isActive()                                         # unticking cancels it
    panel._clear()
    assert panel.table.rowCount() == 0
    runner.shutdown(5)


def test_sleep_command_per_os():
    from fstk.ui.services import Services

    assert Services.sleep_command("Windows")[0] == "powershell"
    assert Services.sleep_command("Darwin") == ["pmset", "sleepnow"]
    assert Services.sleep_command("Linux") == ["systemctl", "suspend"]
