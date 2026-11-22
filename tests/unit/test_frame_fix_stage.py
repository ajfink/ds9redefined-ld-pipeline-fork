"""frame-fixes against a fake VapourSynth: chain resolution, refusal across a cut, grain report."""
from __future__ import annotations

import json

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.media import manual_fix as mf
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

EP = EpisodeId.parse("DS9:S01E20")
BASE = "import vapoursynth as vs\ncore = vs.core\nout = core.std.BlankClip(length=100)\nout.set_output()\n"
NOTE = "dropout streak across the console"


class FakeVS:
    def __init__(self, cut=False):
        self.cut = cut
        rng = np.random.default_rng(1)
        self.img = rng.integers(0, 65535, (48, 64, 3), dtype=np.uint16)
        self.other = rng.integers(0, 65535, (48, 64, 3), dtype=np.uint16)

    def info(self, script):
        return {"frames": 100, "width": 64, "height": 48, "fps_num": 24000, "fps_den": 1001}

    def frames(self, script, first, last, *, width, height, pix_fmt="rgb48le", **kw):
        n = last - first + 1
        if pix_fmt == "gray":
            g = np.full((height, width), 128, np.uint8)
            g[::2, ::2] = 140 if "manualfix" in str(script) else 160       # rebuilt frames: less grain
            return np.stack([g] * n)
        out = [self.img.copy() for _ in range(n)]
        if self.cut:
            out[-1] = self.other
        return np.stack(out)


def env(tmp_path, fixes):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    scripts = prof.root("scripts", EP)
    scripts.mkdir(parents=True)
    (scripts / "ivtc-encode.vpy").write_text(BASE)
    m = Manifest.new(EP, title="t")
    for k, v in (("video.frames", 100), ("video.head_trim", 0), ("audio.pad_frames", 0)):
        m.set(k, v, by="t")
    m.set("video.manual_fixes", [f.as_dict() for f in fixes], by="op")
    m.set_artifact("encode_script", root="scripts", name="ivtc-encode.vpy")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws


def run(prof, vs, job="f1"):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "ff", "stage": "frame-fixes", "after": [], "params": {}}]}
    return Engine(prof, plan, actor="op", tool_overrides={"vapoursynth": vs}).run()["steps"]["ff"]


def test_no_fixes_records_an_unchanged_chain(tmp_path):
    prof, ws = env(tmp_path, [])
    st = run(prof, FakeVS())
    assert st["status"] == "passed", st
    m = ws.store.load()
    text = (prof.root("scripts", EP) / m.artifact("manualfix_script")["name"]).read_text()
    assert mf.manualfix_matches_base(text, BASE) and "MANUAL_FIXES" not in text


def test_interpolation_across_a_cut_is_refused(tmp_path):
    prof, ws = env(tmp_path, [mf.Fix(10, 10, mf.INTERPOLATE, note=NOTE)])
    st = run(prof, FakeVS(cut=True))
    assert st["status"] in ("blocked", "failed") and "shot change" in " ".join(st["issues"])


def test_interpolation_is_applied_and_its_grain_reported(tmp_path):
    prof, ws = env(tmp_path, [mf.Fix(10, 11, mf.INTERPOLATE, note=NOTE)])
    st = run(prof, FakeVS())
    assert st["status"] == "passed", st
    m = ws.store.load()
    rep = json.loads((prof.root("reports", EP) / m.artifact("manualfix_report")["name"]).read_text())
    (fx,) = rep["fixes"]
    assert fx["status"] == "applied" and fx["grain_ratio_vs_neighbour"] < 1


@pytest.mark.parametrize("bad", [mf.Fix(10, 14, mf.INTERPOLATE, note=NOTE), mf.Fix(10, 10, mf.DVD, note=NOTE)])
def test_invalid_or_impossible_fixes_block(tmp_path, bad):
    prof, ws = env(tmp_path, [bad])
    st = run(prof, FakeVS())
    assert st["status"] == "blocked", st
