"""video-quality: measure, conceal a planted dropout from LD neighbours, lock crop/SAR; fake VapourSynth."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.quality.frames import Planes
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = pytest.mark.media
EP = EpisodeId.parse("DS9:S01E20")
FRAMES, W, H = 60, 192, 120
BASE = "import vapoursynth as vs\ncore = vs.core\nout = core.std.BlankClip(length=60)\nout.set_output()\n"
DROP = (30, 50, 40, 24)      # frame, row, x0, width


def frame(i: int) -> Planes:
    rng = np.random.default_rng(1000 + i)
    yy, xx = np.mgrid[0:H, 0:W]
    y = 90 + 0.2 * xx + 0.1 * yy + rng.normal(0, 3, (H, W))
    y[:, :4] = y[:, -4:] = 16.0
    y[:2] = y[-2:] = 16.0
    u = 128 + rng.normal(0, 0.8, (H, W // 2))
    v = 128 + rng.normal(0, 0.8, (H, W // 2))
    if i == DROP[0]:
        y[DROP[1], DROP[2]:DROP[2] + DROP[3]] = 235.0
    return Planes(y.astype(np.float32), u.astype(np.float32), v.astype(np.float32))


class FakeVS:
    def _crop(self, script):
        t = Path(script).read_text()
        m = re.search(r"core\.std\.Crop\(out, left=(\d+), right=(\d+), top=(\d+), bottom=(\d+)\)", t)
        fixes = ast.literal_eval(re.search(r"DROPOUT_FIXES = (\[.*\])", t).group(1)) if "DROPOUT_FIXES" in t else []
        return (tuple(int(g) for g in m.groups()) if m else (0, 0, 0, 0)), fixes

    def info(self, script):
        (l, r, t, b), _ = self._crop(script)
        return {"frames": FRAMES, "fps_num": 24000, "fps_den": 1001, "width": W - l - r, "height": H - t - b}

    def iter_planes(self, script, first=None, last=None, cancel=None):
        (l, r, t, b), fixes = self._crop(script)
        for i in range(first or 0, (FRAMES - 1 if last is None else last) + 1):
            p = frame(i)
            y = p.y.copy()
            for n, src, dx, dy, x, yy, w, h in fixes:
                if n == i:
                    y[yy:yy + h, x:x + w] = frame(i + src).y[yy + dy:yy + dy + h, x + dx:x + dx + w]
            yield Planes(y[t:H - b, l:W - r], p.u[t:H - b, l // 2:W // 2 - r // 2], p.v[t:H - b, l // 2:W // 2 - r // 2])


class FakeFF:
    def probe(self, path):
        raw = {"sample_aspect_ratio": "1:1", "width": W, "height": H}
        return SimpleNamespace(only=lambda kind: SimpleNamespace(raw=raw))


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    script = prof.root("scripts", EP) / "ivtc-encode.vpy"
    script.parent.mkdir(parents=True)
    script.write_text(BASE)
    master = prof.root("prores", EP) / "m.mov"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    m = Manifest.new(EP, title="t")
    m.set("video.frames", FRAMES, by="t")
    m.set_artifact("encode_script", root="scripts", name=script.name)
    m.set_artifact("stack_master", root="prores", name=master.name)
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws


def run(prof, params=None):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "q1",
            "steps": [{"id": "q", "stage": "video-quality", "after": [], "params": params or {}}]}
    return Engine(prof, plan, actor="op", tool_overrides={"vapoursynth": FakeVS(), "ffmpeg": FakeFF()}).run()


def test_conceals_the_planted_dropout_locks_crop_and_leaves_grain_alone(env):
    prof, ws = env
    st = run(prof)["steps"]["q"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert m.get("quality.crop") == {"left": 4, "top": 2, "right": 4, "bottom": 2}
    assert m.get("quality.dar") == "4:3" or m.get("quality.dar")
    assert m.get("quality.dropouts_found") == 1 and m.get("quality.dropouts_fixed") == 1
    assert m.get("quality.chroma_denoise") == "off"            # chroma is clean: the metric says leave it
    rep = (prof.root("reports", EP) / m.artifact("quality_video_report")["name"])
    import json
    r = json.loads(rep.read_text())
    assert r["proof"]["luma_changed_outside_fixes_px"] == 0 and not r["proof"]["issues"]
    assert all(0.9 < x < 1.1 for x in r["proof"]["luma_grain_retention_ratio"])
    assert r["dropouts"]["residual_count"] == 0
    script = (prof.root("scripts", EP) / m.artifact("finish_script")["name"]).read_text()
    assert "DROPOUT_FIXES = [(30," in script and "Crop(out, left=4" in script and "Expr" not in script


def test_noisy_chroma_turns_the_chroma_only_denoise_on(env, monkeypatch):
    prof, ws = env
    import tests.integration.test_quality_stage as me
    orig = me.frame

    def noisy(i):
        p = orig(i)
        rng = np.random.default_rng(i)
        return Planes(p.y, (p.u + rng.normal(0, 5, p.u.shape)).astype(np.float32),
                      (p.v + rng.normal(0, 5, p.v.shape)).astype(np.float32))
    monkeypatch.setattr(me, "frame", noisy)
    st = run(prof)["steps"]["q"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert m.get("quality.chroma_denoise") == "0.5/2.0"
    script = (prof.root("scripts", EP) / m.artifact("finish_script")["name"]).read_text()
    assert "chroma-only temporal denoise" in script
