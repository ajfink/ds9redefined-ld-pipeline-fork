from __future__ import annotations

from fstk.workflow.progress import DEFAULT_SECONDS, ProgressTracker, Timings, format_duration


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def plan():
    return {"steps": [{"id": "a", "stage": "capture-inventory"}, {"id": "b", "stage": "stack-render"},
                      {"id": "c", "stage": "encode-final"}, {"id": "d", "stage": "efm-select"}]}


def test_format_duration():
    assert format_duration(5) == "5s" and format_duration(125) == "2m 05s" and format_duration(7500) == "2h 05m"
    assert format_duration(None) == "–"


def test_weighted_overall_and_counts():
    clk = Clock()
    t = ProgressTracker(plan(), clock=clk)
    snap = t.snapshot()
    assert snap.total == 4 and snap.done == 0 and snap.overall == 0 and snap.state == "idle"
    t.on_event({"kind": "step-start", "step": "a"})
    clk.t += 60
    t.on_event({"kind": "step-passed", "step": "a"})
    t.on_event({"kind": "step-start", "step": "b"})
    t.on_event({"kind": "progress", "step": "b", "fraction": 0.5, "message": "rendering"})
    snap = t.snapshot()
    total = sum(DEFAULT_SECONDS[s] for s in ("capture-inventory", "stack-render", "encode-final", "efm-select"))
    want = (DEFAULT_SECONDS["capture-inventory"] + 0.5 * DEFAULT_SECONDS["stack-render"]) / total
    assert abs(snap.overall - want) < 1e-9
    assert snap.done == 1 and snap.state == "running" and snap.headline() == "Step 2 of 4: b"
    assert snap.current[0].message == "rendering"


def test_step_eta_needs_basis():
    clk = Clock()
    t = ProgressTracker(plan(), clock=clk)
    t.on_event({"kind": "step-start", "step": "b"})
    t.on_event({"kind": "progress", "step": "b", "fraction": 0.5})
    assert t.step_eta("b") is None                      # no time has passed yet: no estimate
    clk.t += 100
    assert abs(t.step_eta("b") - 100) < 1e-6
    t.on_event({"kind": "progress", "step": "b", "fraction": None, "message": "working"})
    assert t.step_eta("b") is None                      # indeterminate step: never an invented ETA


def test_overall_eta_from_history_and_from_this_run(tmp_path):
    clk = Clock()
    tm = Timings(tmp_path / "t.json")
    for stage in ("capture-inventory", "stack-render", "encode-final", "efm-select"):
        tm.record(stage, 1000)
    t = ProgressTracker(plan(), timings=tm, clock=clk)
    assert t.snapshot().overall_eta == 4000             # history basis
    # without history: nothing until a step has run long enough to measure the machine's speed
    t2 = ProgressTracker(plan(), clock=clk)
    assert t2.snapshot().overall_eta is None
    t2.on_event({"kind": "step-start", "step": "a"})
    clk.t += DEFAULT_SECONDS["capture-inventory"] * 2     # twice as slow as the default
    t2.on_event({"kind": "step-passed", "step": "a"})
    eta = t2.snapshot().overall_eta
    assert abs(eta - 2 * (DEFAULT_SECONDS["stack-render"] + DEFAULT_SECONDS["encode-final"]
                          + DEFAULT_SECONDS["efm-select"])) < 1e-6


def test_timings_persist_median_and_ignore_instant_steps(tmp_path):
    p = tmp_path / "x" / "timings.json"
    tm = Timings(p)
    for v in (100, 300, 200):
        tm.record("encode-final", v)
    tm.record("encode-final", 0.2)
    again = Timings(p)
    assert again.median("encode-final") == 200 and len(again.data["encode-final"]) == 3
    (tmp_path / "bad.json").write_text("{nope")
    assert Timings(tmp_path / "bad.json").data == {}


def test_failure_waiting_and_resume_state():
    clk = Clock()
    t = ProgressTracker(plan(), clock=clk)
    t.load_state({"steps": {"a": {"status": "passed"}, "b": {"status": "interrupted"},
                            "d": {"status": "awaiting-review"}}})
    snap = t.snapshot()
    assert snap.done == 1 and t.steps["b"].status == "pending" and snap.state == "waiting"
    assert snap.headline() == "Waiting for you: d"
    t.on_event({"kind": "step-failed", "step": "b", "issues": ["boom"]})
    snap = t.snapshot()
    assert snap.state == "failed" and "boom" in snap.failed[0].detail
    t.on_event({"kind": "step-start", "step": "b"})
    assert t.snapshot().state == "running"
    for sid in "bcd":
        t.on_event({"kind": "step-passed", "step": sid})
    assert t.snapshot().state == "finished" and t.snapshot().overall == 1.0


def test_pause_does_not_count_as_working_time():
    clk = Clock()
    t = ProgressTracker(plan(), clock=clk)
    t.on_event({"kind": "step-start", "step": "b"})
    t.on_event({"kind": "progress", "step": "b", "fraction": 0.5})
    clk.t += 100
    t.on_event({"kind": "paused"})
    clk.t += 5000                                       # a long pause
    assert t.snapshot().paused and t.snapshot().headline() == "Paused"
    assert t.snapshot().steps[1].elapsed(t.paused_at) == 100
    t.on_event({"kind": "resumed"})
    assert not t.snapshot().paused
    assert abs(t.step_eta("b") - 100) < 1e-6            # still 100 s elapsed at 50%: ETA unaffected by the pause
