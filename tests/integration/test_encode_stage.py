"""encode-final with the real x265 recipe and real verification (VapourSynth replaced by a y4m file)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.tools import process
from fstk.tools.ffmpeg import FFmpeg
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = [pytest.mark.media, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")
FRAMES = 24


class Y4mVapourSynth:
    """Stands in for vspipe: serves a pre-rendered y4m clip of the 'script' output."""

    def __init__(self, y4m: Path, frames: int):
        self.y4m, self.frames = y4m, frames

    def info(self, script):
        return {"frames": self.frames, "fps_num": 24000, "fps_den": 1001}

    def encode(self, script, args, *, name="", cancel=None, on_line=None):
        return process.run(["ffmpeg", "-y", "-hide_banner", "-nostdin", "-f", "yuv4mpegpipe",
                            "-i", str(self.y4m), *args], cancel=cancel)


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    master = prof.root("prores", EP) / "S01E20-stack-master.mov"
    master.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30000/1001",
                    "-frames:v", "30", "-vf", "setsar=10/11", "-c:v", "ffv1", str(master)], check=True)
    y4m = tmp_path / "ivtc.y4m"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24000/1001",
                    "-frames:v", str(FRAMES), "-pix_fmt", "yuv420p10le", "-strict", "-1", str(y4m)], check=True)
    script = prof.root("scripts", EP) / "encode.vpy"
    script.parent.mkdir(parents=True)
    script.write_text("# stand-in\n")
    m = Manifest.new(EP, title="t")
    m.set("video.frames", FRAMES, by="t")
    m.set("video.duration_s", FRAMES * 1001 / 24000, by="t")
    m.set("audio.capture", "P2", by="t")
    m.set("mode.chroma_output", "420", by="t")      # the y4m fixture is 4:2:0; the 4:2:2 default has its own test
    m.set_artifact("stack_master", root="prores", name=master.name)
    m.set_artifact("encode_script", root="scripts", name=script.name)
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws, y4m


def run(env, job, frames=FRAMES):
    prof, ws, y4m = env
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job, "steps": [
        {"id": "enc", "stage": "encode-final", "after": [],
         "params": {"planned_gib": 0.01, "estimate_basis": "test clip"}}]}
    return Engine(prof, plan, actor="op", tool_overrides={
        "vapoursynth": Y4mVapourSynth(y4m, frames), "ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()


def test_real_x265_encode_passes_every_check(env):
    prof, ws, _ = env
    state = run(env, "e1")
    st = state["steps"]["enc"]
    assert st["status"] == "passed", st.get("issues")                # no human review (owner, 2026-11-21)
    assert ws.store.load().artifact("padfixed_video")
    out = prof.root("out", EP) / "S01E20-23.976-padfixed-video.mkv"
    assert out.is_file() and not out.with_name(out.stem + ".tmp.mkv").exists()
    info = FFmpeg("ffmpeg", "ffprobe").probe(out).only("video")
    assert info.raw["profile"] == "Main 10" and info.raw.get("display_aspect_ratio") == "40:33"
    report = json.loads((prof.root("reports", EP) / "encode-final.json").read_text())
    assert report["status"] == "PASS"


def test_frame_count_disagreement_stops_before_encoding(env):
    prof, ws, _ = env
    state = run(env, "e2", frames=FRAMES + 1)
    st = state["steps"]["enc"]
    assert st["status"] == "failed" and "video.frames" in st["issues"][0]
    assert not (prof.root("out", EP)).exists() or not any(prof.root("out", EP).iterdir())


def test_default_encode_keeps_422_and_the_checks_know_it(env, tmp_path):
    prof, ws, _ = env
    y422 = tmp_path / "ivtc422.y4m"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24000/1001",
                    "-frames:v", str(FRAMES), "-pix_fmt", "yuv422p10le", "-strict", "-1", str(y422)], check=True)
    with ws.store.transaction(by="t", reason="default chroma") as m:
        m.unset("mode.chroma_output", by="t", reason="default is 4:2:2")
    state = run((prof, ws, y422), "e422")
    st = state["steps"]["enc"]
    assert st["status"] == "passed", st.get("issues")
    out = prof.root("out", EP) / "S01E20-23.976-padfixed-video.mkv"
    raw = FFmpeg("ffmpeg", "ffprobe").probe(out).only("video").raw
    assert raw["pix_fmt"] == "yuv422p10le" and raw["profile"] == "Rext"
