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

    def full_decode(self, script, *, name="", cancel=None, progress=None):
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


class FakeVSHybrid(FakeVS):
    """A 10 s true-video stretch (all fields combed) at frames 1500–1799; the field-difference pass reports
    a telecine repeat pattern when ``plate`` is set."""

    def __init__(self, plate):
        super().__init__()
        self.plate = plate

    def full_decode(self, script, *, name="", cancel=None, progress=None):
        text = Path(script).read_text()
        if name == "field-diff":
            out = re.search(r"OUT = '([^']+)'", text).group(1)
            ranges = eval(re.search(r"RANGES = (\[.*\])", text).group(1))
            n = sum(2 * (b - a) - 2 for a, b in ranges)
            vals = [(0.001 if self.plate and k % 10 in (3, 8) else 0.02 + (k % 7) * 0.001) for k in range(n)]
            Path(out).write_text("".join(f"{k} {v:.6f}\n" for k, v in reversed(list(enumerate(vals)))))
            return FullDecode(n, 0, 0, [])
        log = re.search(r"output='([^']+)'", text).group(1)
        lines = synthetic_log().splitlines()
        for i in range(1500, 1800):
            lines[i] = f"{i} c [200]"
        Path(log).write_text("\n".join(lines) + "\n")
        return FullDecode(self.frames, 0, 0, [])


@pytest.mark.parametrize("plate", [False, True])
def test_video_stretch_gets_a_hybrid_plan_and_plate_test(env, monkeypatch, plate):
    from fstk.stages import cadence

    monkeypatch.setitem(cadence.AUTO_ACCEPT, "max_repair_pct", 100.0)
    monkeypatch.setitem(cadence.AUTO_ACCEPT, "max_inserts", 100)
    monkeypatch.setitem(cadence.AUTO_ACCEPT, "min_period5_pct", 0.0)
    monkeypatch.setitem(cadence.AUTO_ACCEPT, "max_unmatched", 100)
    prof, ws = env
    st = run(env, f"h{int(plate)}", FakeVSHybrid(plate))["steps"]["cad"]
    assert st["status"] == "passed", st
    m = ws.store.load()
    doc = json.loads((prof.root("probe", EP) / m.artifact("hybrid_plan")["name"]).read_text())
    seg = next(s for s in doc["segments"] if s["kind"] == "video-interlaced")
    assert seg["source_frames"][0] == 1500 and seg["plate"] is plate and seg["active"] is (not plate)
    assert m.get("video.hybrid_seconds") == (0.0 if plate else pytest.approx(10.01, abs=0.01))
