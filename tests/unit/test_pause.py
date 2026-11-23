from __future__ import annotations

import sys
import threading
import time

import pytest

from fstk.tools import process
from fstk.tools.process import PAUSE


@pytest.fixture(autouse=True)
def _unpaused():
    PAUSE.resume()
    yield
    PAUSE.resume()


COUNTER = ("import sys,time\n"
           "for i in range(400):\n"
           "    print(i, flush=True)\n"
           "    time.sleep(0.05)\n")


def _run_in_thread(lines, cancel=None):
    out = {}

    def go():
        out["res"] = process.run([sys.executable, "-c", COUNTER], on_line=lambda s, l: lines.append(l),
                                 cancel=cancel, poll_interval=0.05)

    t = threading.Thread(target=go)
    t.start()
    return t, out


def test_pause_freezes_a_running_tool_and_resume_continues_it():
    lines: list[str] = []
    t, out = _run_in_thread(lines, cancel=(cancel := threading.Event()))
    deadline = time.time() + 10
    while len(lines) < 3 and time.time() < deadline:
        time.sleep(0.05)
    PAUSE.pause()
    time.sleep(0.6)                                      # let the monitor suspend it
    n = len(lines)
    time.sleep(1.0)
    assert len(lines) <= n + 1, "output kept coming while paused"
    PAUSE.resume()
    deadline = time.time() + 10
    while len(lines) < n + 5 and time.time() < deadline:
        time.sleep(0.05)
    assert len(lines) >= n + 5, "tool did not continue after resume"
    cancel.set()
    t.join(15)
    assert out["res"].cancelled


def test_cancel_while_paused_still_stops_the_tool():
    lines: list[str] = []
    cancel = threading.Event()
    t, out = _run_in_thread(lines, cancel)
    while len(lines) < 2:
        time.sleep(0.05)
    PAUSE.pause()
    time.sleep(0.6)
    cancel.set()
    t.join(20)
    assert not t.is_alive() and out["res"].cancelled


def test_python_stage_waits_at_report_until_resumed():
    from fstk.stages.base import StageContext

    ctx = StageContext.__new__(StageContext)
    ctx.cancel, ctx.progress = threading.Event(), lambda m, f: seen.append(f)
    seen: list = []
    PAUSE.pause()
    t = threading.Thread(target=lambda: ctx.report(0.5, "x"))
    t.start()
    time.sleep(0.5)
    assert seen == [] and t.is_alive()
    PAUSE.resume()
    t.join(5)
    assert seen == [0.5]


def test_engine_pause_holds_new_steps_and_resume_finishes(tmp_path):
    import json
    from pathlib import Path

    from fstk.config.profile import SCHEMA, Profile
    from fstk.core.identity import EpisodeId
    from fstk.manifest.v38 import import_v38
    from fstk.stages.base import Stage, StepOutput, StepPlan, register
    from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

    @register
    class TSlow(Stage):
        id = "t-slow"

        def plan(self, ctx, params):
            return StepPlan(self.id, params, inputs=[], outputs=[], data={})

        def run(self, ctx, plan):
            for i in range(10):
                time.sleep(0.05)
                ctx.report(i / 10, f"tick {i}")
            return StepOutput()

    fix = Path(__file__).parents[1] / "fixtures" / "v38" / "manifests" / "S01E20.json"
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    ep = EpisodeId.parse("DS9:S01E20")
    EpisodeWorkspace(prof, ep).store.create(import_v38(json.loads(fix.read_text())), by="t", reason="fixture")
    events: list[dict] = []
    plan = {"schema": PLAN_SCHEMA, "episode": "DS9:S01E20", "job": "pz",
            "steps": [{"id": "a", "stage": "t-slow", "params": {}, "after": []},
                      {"id": "b", "stage": "t-slow", "params": {}, "after": ["a"]}]}
    engine = Engine(prof, plan, actor="t", events=events.append)
    out = {}
    th = threading.Thread(target=lambda: out.update(state=engine.run()))
    th.start()
    while not any(e["kind"] == "progress" for e in events):
        time.sleep(0.02)
    engine.pause()
    time.sleep(1.0)                                   # step a would finish and b start if not paused
    n = len(events)
    time.sleep(0.6)
    assert len(events) == n, "events kept flowing while paused"
    assert not any(e["kind"] == "step-start" and e["step"] == "b" for e in events)
    assert events[-1]["kind"] == "paused" or any(e["kind"] == "paused" for e in events)
    engine.resume()
    th.join(30)
    assert out["state"]["steps"]["b"]["status"] == "passed"
    kinds = [e["kind"] for e in events]
    assert kinds.index("paused") < kinds.index("resumed")
