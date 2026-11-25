# ruff: noqa: F811  (``env`` is test_engine's fixture, taken as a parameter)
"""Finished steps are kept across restarts, resumed jobs and new jobs (owner request 2026-11-24: "make sure stages
completed are being saved / aren't being rerun when the app is restarted / a session resumes").

Each test counts how often every step really ran (``CALLS``) through a chain  measure → copy → copy2  where each
step reads the previous step's file, like the real stack → cadence → trim chain.
"""
from __future__ import annotations

import json
from pathlib import Path

from fstk.stages.base import Stage, StepOutput, StepPlan, register
from tests.unit.test_engine import CALLS, _bump, env, plan, run, step  # noqa: F401

FAIL_ONCE: set[str] = set()


@register
class TCopy(Stage):
    id = "t-copy"

    def plan(self, ctx, params):
        src = ctx.root("reports") / params["src"]
        out = ctx.root("reports") / params["out"]
        return StepPlan(self.id, params, inputs=[src], outputs=[out], data={"src": str(src), "out": str(out)})

    def run(self, ctx, plan):
        name = plan.params["out"]
        _bump(name)
        if name in FAIL_ONCE:
            FAIL_ONCE.discard(name)
            from fstk.core.errors import ToolError
            raise ToolError(f"{name}: simulated tool failure")
        Path(plan.data["out"]).write_text(Path(plan.data["src"]).read_text().upper())
        return StepOutput()


def chain(job, **extra):
    _, _, src, _ = ENV[0]
    return plan(job, step("m", "t-measure", source=str(src), **extra),
                step("a", "t-copy", after=["m"], src="measure.txt", out="a.txt"),
                step("b", "t-copy", after=["a"], src="a.txt", out="b.txt"))


ENV: list = []


def runs(name):
    return CALLS.get(name, 0)


def setup(env):
    ENV[:] = [env]
    FAIL_ONCE.clear()


def state_file(env, job):
    _, ws, _, _ = env
    return ws.job_dir(job) / "state.json"


# ------------------------------------------------------------------ the same job again (app restarted)
def test_after_a_failure_only_the_failed_step_runs_again(env):
    setup(env)
    FAIL_ONCE.add("b.txt")
    first = run(env, chain("j"))
    assert first["steps"]["b"]["status"] == "failed"
    again = run(env, chain("j"))                                       # restart, Run again: same job resumes
    assert again["steps"]["b"]["status"] == "passed"
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (1, 1, 2)


def test_a_step_cut_off_by_closing_the_app_runs_again_and_nothing_before_it(env):
    setup(env)
    run(env, chain("j"))
    path = state_file(env, "j")
    st = json.loads(path.read_text())
    st["steps"]["b"]["status"] = "running"                            # the app died while b was running
    path.write_text(json.dumps(st))
    again = run(env, chain("j"))
    assert again["steps"]["b"]["status"] == "passed"
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (1, 1, 2)


def test_a_finished_job_run_again_does_nothing(env):
    setup(env)
    run(env, chain("j"))
    run(env, chain("j"))
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (1, 1, 1)


# ------------------------------------------------------------------ a new job (plan changed, app updated)
def test_a_new_job_keeps_every_finished_step_of_an_earlier_one(env):
    """Before CP60 a plan that differed at all (an update adding a step, a moved estimate) redid everything."""
    setup(env)
    run(env, chain("delivery-old"))
    p = chain("delivery-new", planned_gib=3.2)                                  # estimates move between runs
    p["steps"].append(step("c", "t-copy", after=["b"], src="b.txt", out="c.txt"))   # an update added a step
    new = run(env, p)
    assert {s: new["steps"][s]["status"] for s in "mabc"} == dict.fromkeys("mabc", "passed")
    assert new["steps"]["m"]["carried_from"] == "delivery-old" and "carried_from" not in new["steps"]["c"]
    assert (runs("measure"), runs("a.txt"), runs("b.txt"), runs("c.txt")) == (1, 1, 1, 1)


def test_a_new_job_redoes_what_changed_since_and_everything_after_it(env):
    setup(env)
    run(env, chain("delivery-old"))
    _, _, src, _ = env
    src.write_text("a b c")                                             # the source changed: 3 words, not 4
    run(env, chain("delivery-new"))
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (2, 2, 2)


def test_a_new_job_does_not_keep_a_step_whose_value_another_job_replaced(env):
    setup(env)
    run(env, chain("delivery-old"))
    _, ws, _, _ = env
    with ws.store.transaction(by="t", reason="another job measured again") as m:
        m.set("video.frames", 99, by="stage:t-measure", note="elsewhere")
    run(env, chain("delivery-new"))
    assert runs("measure") == 2                                          # not kept: its value is not its own any more


def test_different_settings_are_not_carried_over(env):
    setup(env)
    run(env, chain("delivery-old"))
    run(env, chain("delivery-new", name="measure-2"))                             # a real parameter changed
    assert runs("measure") == 1 and runs("measure-2") == 1


def test_plans_made_to_redo_something_always_run(env):
    """A re-encode, an A/V round or a redo is a new job on purpose: nothing is carried into it."""
    setup(env)
    run(env, chain("delivery-old"))
    redo = run(env, chain("reencode-x"))
    assert "carried_from" not in redo["steps"]["m"] and runs("measure") == 2


def test_a_stage_that_fills_its_own_gaps_is_planned_again(env, monkeypatch):
    import tests.unit.test_engine as te

    setup(env)
    run(env, chain("delivery-old"))
    monkeypatch.setattr(te.TMeasure, "carry_over", False)               # like efm-decode / archive-download
    new = run(env, chain("delivery-new"))
    assert runs("measure") == 2 and "carried_from" not in new["steps"]["m"]


def test_the_progress_panel_shows_kept_and_carried_steps_as_done():
    from fstk.workflow.progress import ProgressTracker

    t = ProgressTracker({"steps": [{"id": "m", "stage": "t-measure"}, {"id": "a", "stage": "t-copy"}]})
    t.on_event({"kind": "steps-carried-over", "donor": "delivery-old", "steps": ["m"]})
    t.on_event({"kind": "step-kept", "step": "a", "detail": "kept"})
    assert {s.id: s.status for s in t.snapshot().steps} == {"m": "passed", "a": "passed"}


# ------------------------------------------------------------------ an upstream re-run with the same result
def test_when_an_upstream_rerun_gives_the_same_result_later_steps_are_kept(env):
    """PC-B 2026-11-24 14:20: stack-render ran again, gave the identical master, and 32 min of cadence re-ran."""
    setup(env)
    events = []
    run(env, chain("j"))
    _, ws, src, _ = env
    src.write_text("w x y z")                                            # input changed, but still 4 words
    again = run(env, chain("j"), events=events.append)
    assert runs("measure") == 2 and (runs("a.txt"), runs("b.txt")) == (1, 1)
    assert again["steps"]["a"]["status"] == again["steps"]["b"]["status"] == "passed"
    assert [e["step"] for e in events if e["kind"] == "step-kept"] == ["a", "b"]
    run(env, chain("j"))                                                 # and they stay kept afterwards
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (2, 1, 1)


def test_when_an_upstream_rerun_gives_a_different_result_later_steps_run(env):
    setup(env)
    run(env, chain("j"))
    _, _, src, _ = env
    src.write_text("one two")
    again = run(env, chain("j"))
    assert again["steps"]["b"]["status"] == "passed"
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (2, 2, 2)
    assert Path(again["steps"]["b"]["outputs"][0]["path"]).read_text() == "FRAMES=2\n"


def test_an_upstream_rerun_that_fails_keeps_later_steps_waiting_then_compares_when_it_passes(env):
    setup(env)
    run(env, chain("j"))
    _, _, src, _ = env
    src.write_text("w x y z")
    import tests.unit.test_engine as te
    real = te.TMeasure.run

    def broken(self, ctx, plan):
        from fstk.core.errors import ToolError
        raise ToolError("tool missing")

    te.TMeasure.run = broken
    try:
        failed = run(env, chain("j"))
    finally:
        te.TMeasure.run = real
    assert failed["steps"]["m"]["status"] == "failed" and failed["steps"]["a"]["status"] in ("pending", "blocked")
    fixed = run(env, chain("j"))                                         # next session: m runs, same result
    assert fixed["steps"]["a"]["status"] == fixed["steps"]["b"]["status"] == "passed"
    assert (runs("a.txt"), runs("b.txt")) == (1, 1)


def test_a_large_input_is_checked_by_size_and_ends_not_read_in_full(tmp_path):
    """PC-B 2026-11-24 16:36: cadence's 35 GB master input was fully hashed on every resume (1 min 49 s silent)."""
    from fstk.core.hashing import SCOPE_FULL, SCOPE_HEAD_TAIL, SCOPE_JSON
    from fstk.stages.base import LARGE_OUTPUT_BYTES, input_scope_for

    big = tmp_path / "master.mkv"
    with big.open("wb") as f:                                            # sparse: no real 1 GiB written
        f.truncate(LARGE_OUTPUT_BYTES)
    small = tmp_path / "small.flac"
    small.write_bytes(b"x")
    (tmp_path / "r.json").write_text("{}")
    assert input_scope_for(big, {}) == SCOPE_HEAD_TAIL
    assert input_scope_for(small, {}) == SCOPE_FULL
    assert input_scope_for(tmp_path / "r.json", {}) == SCOPE_JSON
    assert input_scope_for(big, {str(big): SCOPE_FULL}) == SCOPE_FULL   # a stage can still ask for a full hash


def test_the_progress_panel_says_when_the_run_is_checking_finished_steps():
    from fstk.workflow.progress import ProgressTracker

    t = ProgressTracker({"steps": [{"id": "m", "stage": "t-measure"}]})
    t.on_event({"kind": "verifying", "detail": "checking the files of finished steps before carrying on"})
    assert "checking" in t.note
    t.on_event({"kind": "verified"})
    assert t.note == ""


# ------------------------------------------------------------------ "Restart from here" (owner request 2026-11-24)
def _restart(env, job, sid):
    from fstk.workflow.engine import restart_from

    _, ws, _, _ = env
    path = state_file(env, job)
    st = json.loads(path.read_text())
    affected = restart_from(chain(job), st, sid, ws.store.load(), actor="op")
    path.write_text(json.dumps(st))
    return affected, st


def test_restart_from_a_step_runs_it_again_and_keeps_later_steps_when_it_gives_the_same(env):
    setup(env)
    run(env, chain("j"))
    affected, st = _restart(env, "j", "a")
    assert affected == ["a", "b"] and st["restarts"][-1]["step"] == "a"
    assert st["steps"]["a"]["status"] == "stale" and st["steps"]["b"]["status"] == "stale"
    again = run(env, chain("j"))
    assert all(s["status"] == "passed" for s in again["steps"].values())
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (1, 2, 1)     # b kept: a gave the same file back


def test_restart_from_runs_later_steps_when_the_result_changes(env):
    setup(env)
    run(env, chain("j"))
    _, _, src, _ = env
    _restart(env, "j", "m")
    src.write_text("something else")                  # the re-run measures a different source
    again = run(env, chain("j"))
    assert again["steps"]["b"]["status"] == "passed"
    assert (runs("measure"), runs("a.txt"), runs("b.txt")) == (2, 2, 2)


def test_restart_from_an_unknown_step_is_refused(env):
    import pytest

    from fstk.core.errors import WorkflowError

    setup(env)
    run(env, chain("j"))
    with pytest.raises(WorkflowError):
        _restart(env, "j", "zz")


def test_a_passed_step_shows_what_it_reported(env):
    from fstk.workflow.progress import ProgressTracker

    setup(env)
    first = run(env, chain("j"))
    t = ProgressTracker(chain("j"))
    t.load_state(first)
    t.on_event({"kind": "step-passed", "step": "a", "detail": "no frame fixes chosen"})
    assert t.steps["a"].detail == "no frame fixes chosen" and t.steps["b"].detail == ""
