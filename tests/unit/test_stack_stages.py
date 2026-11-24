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

    def packet_index(self, path, cancel=None):
        return {"pts": list(range(FRAMES)), "size": [1000] * FRAMES, "key": [True] * FRAMES, "time_base": "1/1000"}

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


def test_imported_prores_master_is_left_alone_and_a_lossless_one_rendered(env):
    prof, ws, _ = env
    run(env, [REF], "p0", orc=FakeOrc())
    old = prof.root("prores", EP) / "S01E20-stack-master.mov"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"v38 prores")
    with ws.store.transaction(by="t", reason="v38 import") as m:
        m.set_artifact("stack_master", root="prores", name=old.name)
    state = run(env, [REF, RENDER], "p1", orc=FakeOrc(), ffmpeg=FakeFF())
    assert state["steps"]["render"]["status"] == "passed", state["steps"]["render"].get("issues")
    assert old.read_bytes() == b"v38 prores"                                   # never touched
    m = ws.store.load()
    assert m.artifact("stack_master")["name"] == "S01E20-stack-master.mkv"
    render = prof.root("stack", EP) / "stack-full-ds9-s01e20-lossless.orcprj"
    assert "mkv-ffv1" in render.read_text() and "S01E20-stack-master.mkv" in render.read_text()


def test_existing_lossless_master_is_reused_and_a_lossy_one_refused(env):
    prof, ws, _ = env
    run(env, [REF], "q0", orc=FakeOrc())
    master = prof.root("prores", EP) / "S01E20-stack-master.mkv"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"ffv1")
    orc = FakeOrc()
    state = run(env, [REF, RENDER], "q1", orc=orc, ffmpeg=FakeFF())
    assert state["steps"]["render"]["status"] == "passed"
    rep = json.loads((prof.root("reports", EP) / "stack-render.json").read_text())
    assert rep["evidence"]["reused"] is True and rep["orc_exit_code"] is None
    state = run(env, [REF, RENDER], "q2", orc=FakeOrc(), ffmpeg=FakeFF(codec="prores"))
    assert state["steps"]["render"]["status"] == "failed"
    assert any("not lossless" in i for i in state["steps"]["render"]["issues"])


def test_finished_refresh_passes_are_reused_after_a_stop(env):
    from fstk.core.errors import ToolError

    class Dies(FakeOrc):
        def refresh(self, project, **kw):
            if self.refreshes >= 1:                 # the second pass is cut off (app closed)
                raise ToolError("killed")
            return super().refresh(project, **kw)

    first = Dies(preferred="P6")
    assert run(env, [REF], "ref-a", orc=first)["steps"]["ref"]["status"] == "failed"
    again = FakeOrc(preferred="P6")
    state = run(env, [REF], "ref-b", orc=again)
    assert state["steps"]["ref"]["status"] == "passed", state["steps"]["ref"]
    assert again.refreshes == 1                     # pass 1 came from the saved result; only pass 2 ran


def _master_with_zero_frames(path, frames=2000, zero=(900, 1800)):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    cond = "+".join(f"eq(N\\,{z})" for z in zero)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x48:r=30000/1001",
                    "-frames:v", str(frames), "-vf",
                    f"format=yuv422p10le,geq=lum='if({cond},0,lum(X,Y))':cb='if({cond},0,cb(X,Y))'"
                    f":cr='if({cond},0,cr(X,Y))'",
                    "-c:v", "ffv1", "-g", "1", str(path)], check=True)


class _Ctx:
    def __init__(self):
        import threading

        self.cancel = threading.Event()
        self.messages = []

    def report(self, fraction, message):
        self.messages.append(message)

    def check_cancel(self):
        pass


def test_a_master_rendered_with_timecode_pads_loses_them_by_a_lossless_copy(tmp_path):
    import numpy as np

    from fstk.stages.stack import StackRender
    from fstk.tools.ffmpeg import FFmpeg

    master = tmp_path / "S01E20-stack-master.mkv"
    _master_with_zero_frames(master)
    ff = FFmpeg("ffmpeg", "ffprobe", log_dir=tmp_path)
    before = ff.gray_thumbnails(master, 16, 12)
    index = ff.packet_index(master)
    got = StackRender()._drop_timecode_pads(_Ctx(), ff, master, index, [900, 1800])
    assert got["frames"] == 1998 and got["original_kept_as"] == "S01E20-stack-master-with-timecode-pads.mkv"
    after = ff.gray_thumbnails(master, 16, 12)
    keep = np.ones(len(before), bool)
    keep[[900, 1800]] = False
    assert np.array_equal(after, before[keep]) and (tmp_path / got["original_kept_as"]).is_file()
    assert ff.count_frames(master) == 1998


def test_a_master_is_not_touched_unless_every_predicted_frame_is_blank(tmp_path):
    from fstk.core.errors import ArtifactError
    from fstk.stages.stack import StackRender
    from fstk.tools.ffmpeg import FFmpeg

    master = tmp_path / "m.mkv"
    _master_with_zero_frames(master, zero=(900,))
    ff = FFmpeg("ffmpeg", "ffprobe", log_dir=tmp_path)
    size = master.stat().st_size
    with pytest.raises(ArtifactError, match=r"not blank|not all-zero"):
        StackRender()._drop_timecode_pads(_Ctx(), ff, master, ff.packet_index(master), [900, 1800])
    assert master.stat().st_size == size and not list(tmp_path.glob("*timecode*"))
