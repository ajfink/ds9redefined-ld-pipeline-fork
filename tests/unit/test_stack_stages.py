"""Stack stages against a fake Orc that behaves like the real CLI (incl. non-zero exits on success)."""
from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.tools.orc import RefreshResult, project_source_labels
from fstk.tools.process import ProcessResult
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace
from fstk.workspace.space import GIB, SpaceLedger

EP = EpisodeId.parse("DS9:S01E20")
LABELS = ["L1", "M1", "P1", "P6"]
FRAMES = 1000


class FakeOrc:
    def __init__(self, preferred="P6", coverage=(10, 90, 900), flip_flop=False, render_frames=FRAMES):
        self.preferred, self.coverage, self.flip_flop, self.render_frames = preferred, coverage, flip_flop, render_frames
        self.refreshes = 0

    def refresh(self, project, *, log_name="", cancel=None):
        self.refreshes += 1
        order = project_source_labels(project)
        pref = order[1] if self.flip_flop else self.preferred
        stats = {"sourceCount": len(order), "suggestedReference": order.index(pref) + 1,
                 "referenceRunnerUp": 1 if order[0] != pref else 2, "stackSigma": 1.2, "bestSingleSigma": 2.0,
                 "fitTrusted": True, "fellBackToMean": False, "framesWith1Source": self.coverage[0],
                 "framesWith2Sources": self.coverage[1], "framesWith3PlusSources": self.coverage[2]}
        report = {"completed": True, "saved": True, "nodes": [{
            "stage": "source_align", "tool": "source_alignment", "status": "success", "statistics": stats,
            "parameter_changes": {"alignmentMap": ", ".join(f"{i + 1}+{i * 3}" for i in range(len(order)))},
            "sources": [{"label": f"TBC Source {x}"} for x in order]}]}
        return RefreshResult(report, ProcessResult(["orc"], 1, 0.1, b"", [], None))  # non-zero exit, real success

    def process_project(self, project, *, log_name="", cancel=None, on_line=None):
        labels = project_source_labels(project)
        sink = next(line.split("value: ", 1)[1] for line in Path(project).read_text().splitlines()
                    if line.strip().startswith("value:") and line.strip().endswith(".mkv"))
        Path(sink).write_bytes(b"\0" * 1024)
        n = len(labels)
        text = "\n".join([*(f"tbc_source: Loaded TBC Source {x}" for x in labels),
                          'StackerStage: {"inputs": [' + ",".join(str(i + 1) for i in range(n)) + f'], "pairs": {n * (n - 1) // 2}}}',
                          "unrecoverable samples: 0", f"Successfully wrote {self.render_frames} frames"])
        return ProcessResult(["orc"], 0, 1.0, None, [], None), text


class FakeFF:
    def __init__(self, codec="ffv1"):
        self.codec = codec

    def count_frames(self, path, stream="v:0"):
        return FRAMES

    def probe(self, path):
        from types import SimpleNamespace
        stream = SimpleNamespace(raw={"codec_name": self.codec})
        return SimpleNamespace(only=lambda kind: stream)


Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def env(tmp_path):
    dl = tmp_path / "dl"
    folder = dl / "DS9-S01E20-LaserDisc-Copies"
    folder.mkdir(parents=True)
    for x in LABELS:
        (folder / f"DS9-S01E20-In the Hands of the Prophets-{x}.tbc").write_bytes(x.encode() * 100)
        (folder / f"DS9-S01E20-In the Hands of the Prophets-{x}.tbc.json").write_text("{}")
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "data"),
                              "media_root": str(tmp_path / "media"), "sources": {"downloads_root": str(dl)},
                              "limits": {"min_free_gb": 1}})
    (tmp_path / "media").mkdir()
    m = Manifest.new(EP, title="t")
    m.set_sources({"captures": [{"label": x, "files": {}} for x in LABELS]})
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    ledger = SpaceLedger(tmp_path / "ledger.json", reserve_gib=1, disk_usage=lambda p: Usage(0, 0, 100 * GIB))
    return prof, ws, ledger


def run(env, steps, job, **tools):
    prof, ws, ledger = env
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job, "steps": steps}
    return Engine(prof, plan, actor="op", ledger=ledger, tool_overrides=tools).run()


REF = {"id": "ref", "stage": "stack-reference", "params": {}, "after": []}
RENDER = {"id": "render", "stage": "stack-render", "after": ["ref"],
          "params": {"planned_gib": 2.0, "estimate_basis": "test: frames × measured ProRes bytes/frame"}}


def test_reference_loop_converges_and_records_measurements(env):
    prof, ws, _ = env
    orc = FakeOrc(preferred="P6")
    state = run(env, [REF], "ref1", orc=orc)
    assert state["steps"]["ref"]["status"] == "passed", state["steps"]["ref"]
    m = ws.store.load()
    assert m.get("stack.reference") == "P6" and m.get("stack.frames") == 1000
    assert m.get("stack.alignment_map").startswith("1+0, 2+3")
    assert m.get("stack.fit_trusted") is True and m.get("stack.stack_sigma") == 1.2
    assert orc.refreshes == 2           # provisional L1 first → Orc says P6 → confirm with P6 first
    project = prof.root("stack", EP) / "stack-full-ds9-s01e20.orcprj"
    assert project_source_labels(project)[0] == "P6"
    report = json.loads((prof.root("reports", EP) / "stack-reference.json").read_text())
    assert [p["decision"]["suggested_label"] for p in report["passes"]] == ["P6", "P6"]


def test_unstable_recommendation_fails_without_writing(env):
    prof, ws, _ = env
    state = run(env, [REF], "ref2", orc=FakeOrc(flip_flop=True))
    assert state["steps"]["ref"]["status"] == "failed" and "did not stabilise" in state["steps"]["ref"]["issues"][0]
    assert not ws.store.load().is_set("stack.reference")


def test_render_requires_an_estimate_but_no_review(env):
    prof, ws, _ = env
    orc = FakeOrc()
    bad = dict(RENDER, params={})
    state = run(env, [REF, bad], "r0", orc=orc, ffmpeg=FakeFF())
    assert state["steps"]["render"]["status"] == "blocked" and "planned_gib" in state["steps"]["render"]["issues"][0]
    state = run(env, [REF, RENDER], "r1", orc=orc, ffmpeg=FakeFF())
    assert state["steps"]["render"]["status"] == "passed", state["steps"]["render"].get("issues")   # owner: no review
    report = prof.root("reports", EP) / "stack-render.json"
    assert json.loads(report.read_text())["status"] == "PASS"


def test_render_rejects_a_lossy_master(env):
    state = run(env, [REF, RENDER], "r3", orc=FakeOrc(), ffmpeg=FakeFF(codec="prores"))
    st = state["steps"]["render"]
    assert st["status"] == "failed" and any("not lossless" in i for i in st["issues"])


def test_generated_project_forces_lossless_sink():
    from fstk.tools.orc_project import build_stack_project, bundled_template
    tpl = bundled_template().replace("mkv-ffv1", "mov-prores")     # an old ProRes template is corrected
    text = build_stack_project(tpl, ["A", "B"], {"A": Path("a.tbc"), "B": Path("b.tbc")}, Path("m.mkv"), "x")
    assert "mkv-ffv1" in text and "mov-prores" not in text and "prores_profile" not in text
    assert "value: m.mkv" in text


def test_render_frame_mismatch_fails_even_with_clean_exit(env):
    state = run(env, [REF, RENDER], "r2", orc=FakeOrc(render_frames=999), ffmpeg=FakeFF())
    issues = state["steps"]["render"]["issues"]
    assert state["steps"]["render"]["status"] == "failed" and any("999" in i for i in issues)
