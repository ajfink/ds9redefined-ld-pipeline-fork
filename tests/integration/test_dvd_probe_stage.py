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

pytestmark = [pytest.mark.media,
              pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")


def test_dvd_probe_end_to_end(tmp_path):
    dvd_dir = tmp_path / "dvd"
    dvd_dir.mkdir()
    dvd = dvd_dir / "DS9 S01E20.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30000/1001",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-frames:v", "60",
                    "-t", "2.1", "-c:v", "mpeg2video", "-c:a", "ac3", "-ac", "2",
                    "-metadata:s:a:0", "language=eng", str(dvd)], check=True)
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media"),
                              "sources": {"dvd_root": str(dvd_dir)}, "limits": {"min_free_gb": 1}})
    ws = EpisodeWorkspace(prof, EP)
    m = Manifest.new(EP, title="In the Hands of the Prophets")
    m.set_artifact("dvd", root="dvd_root", name=dvd.name)
    ws.store.create(m, by="test", reason="setup")
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "intake",
            "steps": [{"id": "probe", "stage": "dvd-probe", "params": {}, "after": []}]}
    events = []
    state = Engine(prof, plan, actor="tester", events=events.append,
                   tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()
    assert state["steps"]["probe"]["status"] == "passed", state["steps"]["probe"]
    report = json.loads((prof.root("reports", EP) / "dvd-probe.json").read_text())
    assert report["decoded_video_frames"] == 60 and report["seek_policy"] == "seek-allowed"
    assert {s["type"] for s in report["streams"]} == {"video", "audio"}
    assert any(e["kind"] == "progress" for e in events)
    assert ws.store.load().set_keys() == []          # probe records no manifest values
