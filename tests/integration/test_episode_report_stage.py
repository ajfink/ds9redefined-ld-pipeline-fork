"""episode-report with real ffmpeg: report + contact sheet land next to the deliverable."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.tools.ffmpeg import FFmpeg
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = [pytest.mark.media, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")


def test_report_and_contact_sheet(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    vdir = prof.root("out", EP)
    vdir.mkdir(parents=True)
    video = vdir / "ep.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=10",
                    "-c:v", "libx264", "-color_primaries", "smpte170m", "-color_trc", "smpte170m",
                    "-colorspace", "smpte170m", "-color_range", "tv", str(video)], check=True)
    rdir = prof.root("reports", EP)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "quality-video-x.json").write_text(json.dumps({"dropouts": {"found": 1, "concealed": 0, "residual_count": 1,
                                              "residual": [{"frame": 120, "reason": "tall"}]}, "proof": {"issues": []}}))
    m = Manifest.new(EP, title="t")
    m.set("video.frames", 240, by="t")
    m.set("video.duration_s", 10.0, by="t")
    m.set("deliverable.key", "final", by="t")
    m.set_artifact("final", root="out", name=video.name)
    m.set_artifact("quality_video_report", root="reports", name="quality-video-x.json")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "r1",
            "steps": [{"id": "r", "stage": "episode-report", "after": [], "params": {}}]}
    st = Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()["steps"]["r"]
    assert st["status"] == "passed", st
    assert (video.parent / "ep.quality.md").is_file()
    got = ws.store.load()
    assert got.get("quality.verdict") == "REVIEW"
    rep = json.loads((prof.root("reports", EP) / got.artifact("episode_quality_report")["name"]).read_text())
    assert all(t["status"] == "PASS" for t in rep["delivered_tags"])
    sheet = prof.root("reports", EP) / got.artifact("episode_contact_sheet")["name"]
    assert sheet.is_file() and sheet.stat().st_size > 1000
