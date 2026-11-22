from __future__ import annotations

import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA as PROFILE_SCHEMA
from fstk.config.profile import Profile
from fstk.core.errors import ManifestError, WorkflowError
from fstk.core.identity import EpisodeId
from fstk.evidence.approvals import ApprovalBook
from fstk.manifest.model import Manifest
from fstk.stages.base import SpaceNeed, Stage, StepOutput, StepPlan, ValueSet, register
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace, validate_plan
from fstk.workspace.space import GIB, SpaceLedger

EP = EpisodeId.parse("DS9:S01E20")
CALLS: dict[str, int] = {}
ACTIVE = {"compute": 0, "max_compute": 0}
_lock = threading.Lock()


def _bump(name):
    CALLS[name] = CALLS.get(name, 0) + 1


@register
class TMeasure(Stage):
    id = "t-measure"
    writes = ("video.frames",)
    writes_artifacts = ("t_report",)

    def plan(self, ctx, params):
        src = Path(params["source"])
        out = ctx.root("reports") / f"{params.get('name', 'measure')}.txt"
        return StepPlan(self.id, params, inputs=[src], outputs=[out], data={"src": str(src), "out": str(out)})

    def run(self, ctx, plan):
        _bump(plan.params.get("name", "measure"))
        frames = len(Path(plan.data["src"]).read_text().split())
        Path(plan.data["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(plan.data["out"]).write_text(f"frames={frames}\n")
        out = StepOutput()
        out.measure("video.frames", frames, note="counted words")
        out.artifacts["t_report"] = {"root": "reports", "name": Path(plan.data["out"]).name}
        return out

    def verify(self, ctx, plan, output):
        return [] if Path(plan.data["out"]).read_text().startswith("frames=") else ["bad report"]


@register
class TNeedsFrames(Stage):
    id = "t-needs-frames"
    requires = (ValueSet("video.frames"),)
    writes = ("video.head_trim",)

    def run(self, ctx, plan):
        _bump("needs")
        out = StepOutput()
        out.measure("video.head_trim", 0)
        return out


@register
class TBadVerify(Stage):
    id = "t-bad-verify"
    writes = ("video.tail_trim",)

    def run(self, ctx, plan):
        _bump("bad")
        out = StepOutput()
        out.measure("video.tail_trim", 7)
        return out

    def verify(self, ctx, plan, output):
        return ["artifact truncated"]


@register
class TIllegalWrite(Stage):
    id = "t-illegal"
    writes = ()

    def run(self, ctx, plan):
        out = StepOutput()
        out.measure("video.tail_trim", 1)
        return out


@register
class TReview(Stage):
    id = "t-review"
    review = "t-evidence"
    writes = ("video.inserts",)

    def plan(self, ctx, params):
        out = ctx.root("evidence") / "sheet.txt"
        return StepPlan(self.id, params, outputs=[out], data={"out": str(out)})

    def run(self, ctx, plan):
        _bump("review")
        Path(plan.data["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(plan.data["out"]).write_text("contact sheet")
        out = StepOutput()
        out.measure("video.inserts", 3)
        return out


@register
class TCompute(Stage):
    id = "t-compute"
    resource = "compute"

    def run(self, ctx, plan):
        with _lock:
            ACTIVE["compute"] += 1
            ACTIVE["max_compute"] = max(ACTIVE["max_compute"], ACTIVE["compute"])
        time.sleep(0.2)
        with _lock:
            ACTIVE["compute"] -= 1
        return StepOutput()


@register
class TSpace(Stage):
    id = "t-space"

    def plan(self, ctx, params):
        return StepPlan(self.id, params, space=SpaceNeed("work", params["gib"], "test estimate"))

    def run(self, ctx, plan):
        _bump("space")
        return StepOutput()


Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def env(tmp_path):
    CALLS.clear()
    prof = Profile.from_dict({"schema": PROFILE_SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media"),
                              "limits": {"min_free_gb": 10}})
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(Manifest.new(EP, title="t"), by="test", reason="setup")
    src = tmp_path / "src.txt"
    src.write_text("a b c d")
    (tmp_path / "media").mkdir()
    ledger = SpaceLedger(tmp_path / "ledger.json", reserve_gib=10,
                         disk_usage=lambda p: Usage(0, 0, 50 * GIB))
    return prof, ws, src, ledger


def plan(job, *steps):
    return {"schema": PLAN_SCHEMA, "episode": "DS9:S01E20", "job": job, "steps": list(steps)}


def step(sid, stage, after=(), **params):
    return {"id": sid, "stage": stage, "after": list(after), "params": params}


def run(env, p, **kw):
    prof, ws, src, ledger = env
    return Engine(prof, p, actor="tester", ledger=ledger, **kw).run()


def test_commit_after_verify_with_provenance(env):
    prof, ws, src, _ = env
    state = run(env, plan("j1", step("m", "t-measure", source=str(src)),
                         step("n", "t-needs-frames", after=["m"])))
    assert state["steps"]["m"]["status"] == "passed" and state["steps"]["n"]["status"] == "passed"
    m = ws.store.load()
    assert m.get("video.frames") == 4 and m.get("video.head_trim") == 0
    entry = m.entry("video.frames")
    assert entry["stage"] == "t-measure" and entry["by"] == "stage:t-measure"
    assert any(e.get("path") == str(src) for e in entry["evidence"])
    assert m.artifact("t_report") == {"root": "reports", "name": "measure.txt"}
    assert ws.store.verify() == []


def test_failed_verify_writes_nothing_and_blocks_dependents(env):
    prof, ws, src, _ = env
    before = ws.store.read().raw
    state = run(env, plan("j2", step("b", "t-bad-verify"), step("n", "t-needs-frames", after=["b"])))
    assert state["steps"]["b"]["status"] == "failed" and state["steps"]["b"]["issues"] == ["artifact truncated"]
    assert state["steps"]["n"]["status"] == "blocked" and state["steps"]["n"]["blocked_by"] == ["b"]
    assert ws.store.read().raw == before


def test_gate_failure_blocks_without_running(env):
    state = run(env, plan("j3", step("n", "t-needs-frames")))
    assert state["steps"]["n"]["status"] == "blocked" and "video.frames" in state["steps"]["n"]["issues"][0]
    assert "needs" not in CALLS


def test_undeclared_write_is_refused(env):
    prof, ws, *_ = env
    state = run(env, plan("j4", step("x", "t-illegal")))
    assert state["steps"]["x"]["status"] == "failed" and "undeclared" in state["steps"]["x"]["issues"][0]
    assert not ws.store.load().is_set("video.tail_trim")


def test_resume_skips_verified_steps_and_reruns_changed_ones(env):
    prof, ws, src, _ = env
    p = plan("j5", step("m", "t-measure", source=str(src)), step("n", "t-needs-frames", after=["m"]))
    run(env, p)
    run(env, p)
    assert CALLS == {"measure": 1, "needs": 1}
    src.write_text("a b c d e f")
    state = run(env, p)
    assert CALLS == {"measure": 2, "needs": 2}
    assert state["steps"]["m"]["previous_status"] == "stale"
    assert ws.store.load().get("video.frames") == 6
    journal_ops = [r["changes"][0]["key"] for r in ws.store.journal()[1:]]
    assert journal_ops.count("video.frames") == 2


def test_same_job_different_plan_is_refused(env):
    prof, ws, src, _ = env
    run(env, plan("j6", step("m", "t-measure", source=str(src))))
    with pytest.raises(WorkflowError, match="different plan"):
        run(env, plan("j6", step("m", "t-measure", source=str(src), name="other")))


def test_review_gates_dependents_and_goes_stale_when_evidence_changes(env):
    prof, ws, src, _ = env
    p = plan("j7", step("r", "t-review"), step("m", "t-measure", after=["r"], source=str(src)))
    state = run(env, p)
    assert state["steps"]["r"]["status"] == "awaiting-review" and state["steps"]["m"]["status"] == "blocked"
    assert not ws.store.load().is_set("video.inserts")          # nothing committed before approval
    sheet = prof.root("evidence", EP) / "sheet.txt"
    book = ApprovalBook(ws.state_dir / "approvals")
    with pytest.raises(Exception, match="human note"):
        book.record(review="t-evidence", decision="APPROVED", note=" ", actor="op", evidence=[sheet])
    book.record(review="t-evidence", decision="APPROVED", note="looks right", actor="op", evidence=[sheet])
    state = run(env, p)
    assert state["steps"]["r"]["status"] == "passed" and state["steps"]["m"]["status"] == "passed"
    assert CALLS["review"] == 1
    entry = ws.store.load().entry("video.inserts")
    assert entry["value"] == 3 and any(e.get("kind") == "approval" for e in entry["evidence"])
    sheet.write_text("tampered")
    assert book.status("t-evidence").state == "stale"


def test_rejection_blocks(env):
    prof, ws, src, _ = env
    p = plan("j8", step("r", "t-review"), step("m", "t-measure", after=["r"], source=str(src)))
    run(env, p)
    sheet = prof.root("evidence", EP) / "sheet.txt"
    ApprovalBook(ws.state_dir / "approvals").record(review="t-evidence", decision="REJECTED",
                                                    note="wrong boundary", actor="op", evidence=[sheet])
    state = run(env, p)
    assert state["steps"]["m"]["status"] == "blocked"
    assert not ws.store.load().is_set("video.inserts")


def test_resource_limits_serialize_compute(env):
    ACTIVE.update(compute=0, max_compute=0)
    state = run(env, plan("j9", step("a", "t-compute"), step("b", "t-compute"), step("c", "t-compute")))
    assert all(s["status"] == "passed" for s in state["steps"].values())
    assert ACTIVE["max_compute"] == 1


def test_space_gate(env):
    state = run(env, plan("j10", step("ok", "t-space", gib=30.0)))
    assert state["steps"]["ok"]["status"] == "passed"
    state = run(env, plan("j11", step("big", "t-space", gib=45.0)))
    assert state["steps"]["big"]["status"] == "blocked" and "SPACE FAIL" in state["steps"]["big"]["issues"][0]


@pytest.mark.parametrize("bad, msg", [
    ({"steps": []}, "non-empty"),
    ({"steps": [step("a", "t-compute", after=["b"]), step("b", "t-compute", after=["a"])]}, "cycle"),
    ({"steps": [step("a", "t-compute", after=["zz"])]}, "unknown step"),
    ({"steps": [step("a", "no-such-stage")]}, "unknown stage"),
    ({"job": "bad job"}, "identifier"),
])
def test_plan_validation(bad, msg):
    p = plan("j", step("a", "t-compute"))
    p.update(bad)
    with pytest.raises(WorkflowError, match=msg):
        validate_plan(p)


def test_value_set_requires_declared_key():
    with pytest.raises(ManifestError):
        ValueSet("video.nonsense")



def test_large_outputs_are_fingerprinted_head_tail(tmp_path):
    from fstk.core.hashing import SCOPE_FULL, SCOPE_HEAD_TAIL, Fingerprint
    from fstk.stages.base import LARGE_OUTPUT_BYTES, output_scope_for
    big = tmp_path / "master.mkv"
    with open(big, "wb") as f:  # sparse: costs no disk, reads only head + tail
        f.write(b"head")
        f.truncate(LARGE_OUTPUT_BYTES)
        f.seek(LARGE_OUTPUT_BYTES - 4)
        f.write(b"tail")
    small = tmp_path / "report.json"
    small.write_text("{}")
    assert output_scope_for(big, {}) == SCOPE_HEAD_TAIL
    assert output_scope_for(small, {}) == SCOPE_FULL
    assert output_scope_for(big, {str(big): SCOPE_FULL}) == SCOPE_FULL
    fp = Fingerprint.of(big, scope=output_scope_for(big, {}))
    assert fp.scope == SCOPE_HEAD_TAIL and fp.matches()


def test_every_stage_module_is_registered_by_the_package_import():
    """Guards against a stage module that only registers when a test imports it directly."""
    import pkgutil

    import fstk.stages as pkg
    from fstk.stages.base import REGISTRY

    modules = {m.name for m in pkgutil.iter_modules(pkg.__path__)} - {"base", "ldaudio_build"}
    owners = {cls.__module__.rsplit(".", 1)[-1] for cls in REGISTRY.values()}
    assert modules <= owners, modules - owners
