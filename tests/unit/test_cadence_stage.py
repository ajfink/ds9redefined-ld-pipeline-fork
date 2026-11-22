from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.evidence.approvals import ApprovalBook
from fstk.manifest.model import Manifest
from fstk.tools.vapoursynth import FullDecode
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace
from tests.unit.test_cadence_map import synthetic_log

EP = EpisodeId.parse("DS9:S01E20")


class FakeVS:
    def __init__(self, frames=3000, log_rows=3000, vspipe_rc=0):
        self.frames, self.log_rows, self.vspipe_rc = frames, log_rows, vspipe_rc

    def full_decode(self, script, *, name="", cancel=None):
        text = Path(script).read_text()
        log = re.search(r"output='([^']+)'", text).group(1)
        assert "Trim(src, 118, None)" in text
        Path(log).write_text("\n".join(synthetic_log().splitlines()[: self.log_rows]) + "\n")
        return FullDecode(self.frames, self.vspipe_rc, 0, [])


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    master = prof.root("prores", EP) / "S01E20-stack-master.mov"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"master")
    m = Manifest.new(EP, title="t")
    m.set("stack.frames", 3118, by="t")
    m.set("audio.pad_frames", 118, by="t")
    m.set_artifact("stack_master", root="prores", name=master.name)
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws


def run(env, job, vs):
    prof, ws = env
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "cad", "stage": "cadence-evidence", "params": {}, "after": []}]}
    return Engine(prof, plan, actor="op", tool_overrides={"vapoursynth": vs}).run()


def test_cadence_outside_the_v38_ranges_waits_for_review(env, monkeypatch):
    from fstk.stages import cadence

    monkeypatch.setitem(cadence.AUTO_ACCEPT, "min_period5_pct", 101.0)   # force "outside the ranges"
    prof, ws = env
    state = run(env, "c1", FakeVS())
    st = state["steps"]["cad"]
    assert st["status"] == "awaiting-review", st
    assert not ws.store.load().is_set("video.repair_frames")
    report = json.loads((prof.root("reports", EP) / "cadence.json").read_text())
    assert report["analysis"]["insert_count"] == 1 and report["decoded_frames"] == 3000
    di_map = (prof.root("probe", EP) / "di-map-ds9-s01e20.txt").read_text()
    assert "[400 401]" in di_map
    evidence = [Path(fp["path"]) for fp in st["outputs"]]
    ApprovalBook(ws.state_dir / "approvals").record(review="cadence-review", decision="APPROVED",
                                                    note="insert at 40 s checked frame by frame",
                                                    actor="op", evidence=evidence)
    state = run(env, "c1", FakeVS())
    m = ws.store.load()
    assert state["steps"]["cad"]["status"] == "passed"
    assert m.get("video.inserts") == 1 and m.get("video.repair_frames") == report["analysis"]["repair_frame_count"]


@pytest.mark.parametrize("vs, why", [(FakeVS(frames=2999), "incomplete"), (FakeVS(log_rows=2990), "rejected"),
                                     (FakeVS(vspipe_rc=1), "incomplete")])
def test_incomplete_evidence_fails_closed(env, vs, why):
    state = run(env, f"bad-{why}-{vs.frames}-{vs.log_rows}-{vs.vspipe_rc}", vs)
    st = state["steps"]["cad"]
    assert st["status"] == "failed" and why in st["issues"][0]


def test_rerun_moves_stale_log_aside(env):
    prof, ws = env
    run(env, "c2", FakeVS())
    run(env, "c3", FakeVS())
    stale = list(prof.root("probe", EP).glob("full-tfm-log-ds9-s01e20.superseded-*.txt"))
    assert len(stale) == 1


def test_cadence_inside_the_v38_ranges_is_accepted_automatically(env, monkeypatch):
    from fstk.stages import cadence

    monkeypatch.setitem(cadence.AUTO_ACCEPT, "min_period5_pct", 0.0)
    monkeypatch.setitem(cadence.AUTO_ACCEPT, "max_repair_pct", 100.0)
    monkeypatch.setitem(cadence.AUTO_ACCEPT, "max_unmatched", 10**6)       # the fake log is not a real episode
    prof, ws = env
    st = run(env, "c9", FakeVS())["steps"]["cad"]
    assert st["status"] == "passed", st                                   # no human needed
    assert ws.store.load().get("video.inserts") == 1
    report = json.loads((prof.root("reports", EP) / "cadence.json").read_text())
    assert report["auto_accept_issues"] == []
