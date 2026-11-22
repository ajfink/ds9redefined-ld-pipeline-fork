"""pad-audit → pad-repair → encode-final script selection, with a fake VapourSynth and RIFE."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.media import pad_repair as pr
from fstk.media import pads as pm
from fstk.tools.ffmpeg import FFmpeg
from fstk.tools.vapoursynth import ScriptScan
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = pytest.mark.media
EP = EpisodeId.parse("DS9:S01E20")
FRAMES, W, H = 60, 192, 120
F24 = pm.F24
BASE = "import vapoursynth as vs\ncore = vs.core\nout = core.std.BlankClip(length=60)\nout.set_output()\n"


def scene(seed, w=W, h=H):
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(rng.integers(0, 255, (h, w, 3), dtype=np.uint8), (0, 0), 5)
    return cv2.normalize(img, None, 40, 215, cv2.NORM_MINMAX)


A, B = scene(1), scene(2)


def ld_frame(i: int) -> np.ndarray:           # shot A until frame 41, shot B from 42; PADs 20 and 41 are black
    if i in (20, 41):
        return np.zeros((H, W, 3), np.uint8)
    return np.roll(A, 3 * i, axis=1) if i < 42 else np.roll(B, 3 * (i - 42), axis=1)


class FakeVS:
    def __init__(self, pads=(20, 41)):
        self.pads = pads
        self.dvd_window = None

    def info(self, script):
        return {"frames": FRAMES, "fps_num": 24000, "fps_den": 1001, "width": W, "height": H}

    def scan(self, script, **kw):
        thumbs = np.stack([cv2.resize(cv2.cvtColor(ld_frame(i), cv2.COLOR_RGB2GRAY), (48, 36),
                                      interpolation=cv2.INTER_AREA).ravel() for i in range(FRAMES)])
        ev = [(p / F24, (p + 1) / F24, 1 / F24) for p in self.pads]
        return ScriptScan(thumbs, ev, [])

    def frames(self, script, first, last, *, width, height, pix_fmt="rgb24", **kw):
        if "dvd-window" in Path(script).name:
            return self.dvd_window
        return np.stack([ld_frame(i) for i in range(first, last + 1)])


class FakeRife:
    method = "RIFE-test-CPU-TTA"

    def interpolate(self, prev, nxt, out, cancel=None):
        a, b = cv2.imread(str(prev)), cv2.imread(str(nxt))
        cv2.imwrite(str(out), ((a.astype(int) + b.astype(int)) // 2).astype(np.uint8))


@pytest.fixture
def env(tmp_path, monkeypatch):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    monkeypatch.setattr("fstk.stages.pads.reference_map_ranges", lambda *a, **k: [25, 52, 200])
    monkeypatch.setattr("fstk.stages.pads.source_id_for_capture", lambda *a, **k: 1)
    script = prof.root("scripts", EP) / "ivtc-encode.vpy"
    script.parent.mkdir(parents=True)
    script.write_text(BASE)
    project = prof.root("stack", EP) / "stack.orcprj"
    project.parent.mkdir(parents=True)
    project.write_text("x")
    m = Manifest.new(EP, title="t")
    for k, v in {"video.frames": FRAMES, "video.head_trim": 0, "audio.pad_frames": 0, "stack.reference": "P1"}.items():
        m.set(k, v, by="t")
    m.set_artifact("encode_script", root="scripts", name=script.name)
    m.set_artifact("stack_project", root="stack", name=project.name)
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws


def run(prof, steps, job, vs, **tools):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job, "steps": steps}
    return Engine(prof, plan, actor="op", tool_overrides={"vapoursynth": vs, "rife": FakeRife(), **tools}).run()


AUDIT = {"id": "audit", "stage": "pad-audit", "after": [], "params": {}}
REPAIR = {"id": "repair", "stage": "pad-repair", "after": ["audit"], "params": {}}


def test_audit_finds_both_pads_and_classifies_the_cut(env):
    prof, ws = env
    st = run(prof, [AUDIT], "a1", FakeVS())["steps"]["audit"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert m.get("pads.map_pads") == 3 and m.get("pads.rendered_isolated") == 2
    assert m.get("pads.trimmed_away") == 1 and m.get("pads.never_rendered") == 0
    assert m.get("pads.cut_pads") == 1 and m.get("pads.cut_pad_frames") == [41]
    rep = json.loads(ws.store.load().artifact("pad_audit") and
                     (prof.root("reports", EP) / m.artifact("pad_audit")["name"]).read_text())
    assert rep["candidates"] == [20, 41]


def test_dark_content_that_is_not_a_map_pad_is_left_alone(env):
    prof, ws = env
    st = run(prof, [AUDIT], "a2", FakeVS(pads=(20, 41, 52)))["steps"]["audit"]
    assert st["status"] == "passed"
    assert ws.store.load().get("pads.content_blacks_left_alone") == [52]


def test_no_dvd_repair_rife_on_non_cut_and_hold_on_cut(env):
    prof, ws = env
    state = run(prof, [AUDIT, REPAIR], "r1", FakeVS())
    st = state["steps"]["repair"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert m.get("pads.repaired") == 2
    rows = json.loads((prof.root("reports", EP) / m.artifact("pad_repair_report")["name"]).read_text())["rows"]
    by = {r["frame"]: r for r in rows}
    assert by[20]["method"] == "RIFE-test-CPU-TTA" and by[41]["method"] == pr.HOLD
    script = (prof.root("scripts", EP) / m.artifact("padfix_script")["name"]).read_text()
    assert "(20, 'rife', 0), (41, 'hold-prev-scene-cut', None)" in script and pr.padfix_matches_base(script, BASE)
    if shutil.which("ffmpeg"):
        payload = Path(m.artifact("pad_payload")["path"])
        assert payload.is_file() and FFmpeg("ffmpeg", "ffprobe").count_frames(payload) == 1
    from fstk.qc.gates import read_frame_list
    from fstk.qc.identity import read_replacements

    assert read_frame_list(prof.root("reports", EP) / m.artifact("pad_repair_frames")["name"]) == [20, 41]

    reps = read_replacements(prof.root("reports", EP) / m.artifact("pad_replacements")["name"], "S01E20", Path("."))
    assert set(reps) == {20, 41}


def test_dvd_route_borrows_the_frame_on_a_cut(env):
    prof, ws = env
    # DVD window: shot A frames 37..39 then (missing LD frame) 40' = A rolled on, then shot B 40.. at DVD scale
    with ws.store.transaction(by="t", reason="dvd") as m:
        m.set("mode.dvd_reference_available", True, by="t")
        m.set_artifact("dvd", root="out", name="dvd.mkv")
        m.set("video.dvd_alignment", {"acts": [{"start_frame": 0, "end_frame": 200, "knots": [0, 200],
                                                "values": [0, 0]}]}, by="t")
    seq = [np.roll(A, 3 * k, axis=1) for k in range(37, 42)] + [np.roll(B, 3 * k, axis=1) for k in range(0, 4)]
    fv = FakeVS()
    fv.dvd_window = np.stack([cv2.resize(f, (W * 3 // 4, H), interpolation=cv2.INTER_AREA) for f in seq])
    st = run(prof, [AUDIT, REPAIR], "r2", fv)["steps"]["repair"]
    assert st["status"] == "passed", st.get("issues")
    rows = json.loads((prof.root("reports", EP) / ws.store.load().artifact("pad_repair_report")["name"]).read_text())["rows"]
    cut = next(r for r in rows if r["frame"] == 41)
    assert cut["method"] == "dvd-cut-frame", cut["note"]
    assert "colour-matched to the prev LD frame" in cut["note"]
    got = cv2.cvtColor(cv2.imread(cut["png"]), cv2.COLOR_BGR2RGB)
    truth = np.roll(A, 3 * 41, axis=1)
    assert np.abs(got[8:-8, 8:-8].astype(int) - truth[8:-8, 8:-8].astype(int)).mean() < 8
    assert {r["frame"]: r["method"] for r in rows}[20] == "RIFE-test-CPU-TTA"   # non-cut PAD still uses RIFE


def test_encode_final_prefers_a_current_padfix_script_and_refuses_a_stale_one(env):
    prof, ws = env
    run(prof, [AUDIT, REPAIR], "r3", FakeVS())
    master = prof.root("prores", EP) / "m.mov"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"x")
    with ws.store.transaction(by="t", reason="encode inputs") as m:
        m.set("video.duration_s", FRAMES * 1001 / 24000, by="t")
        m.set("audio.capture", "P2", by="t")
        m.set_artifact("stack_master", root="prores", name=master.name)
    from fstk.stages.base import StageContext
    from fstk.stages.encode import EncodeFinal

    def ctx():
        return StageContext(prof, EP, ws.store, "op")

    m = ws.store.load()
    fixed = prof.root("scripts", EP) / m.artifact("padfix_script")["name"]
    plan = EncodeFinal().plan(ctx(), {"planned_gib": 0.01, "estimate_basis": "t"})
    assert plan.data["script"] == str(fixed)
    (prof.root("scripts", EP) / "ivtc-encode.vpy").write_text(BASE + "# retrimmed\n")   # video-trim ran again
    from fstk.core.errors import GateError

    with pytest.raises(GateError, match="older encode script"):
        EncodeFinal().plan(ctx(), {"planned_gib": 0.01, "estimate_basis": "t"})
