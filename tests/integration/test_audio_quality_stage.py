"""audio-quality with real ffmpeg: a planted click and short gap are filled, a long gap is only reported."""
from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.tools.ffmpeg import FFmpeg
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = [pytest.mark.media, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")
RATE, SECONDS = 48000, 20


def make_flac(path, data):
    raw = path.with_suffix(".raw")
    raw.write_bytes(data.astype("<f4").tobytes())
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", str(raw), "-c:a", "flac",
                    "-sample_fmt", "s32", "-bits_per_raw_sample", "24", str(path)], check=True)


@pytest.fixture
def env(tmp_path):
    rng = np.random.default_rng(9)
    t = np.arange(RATE * SECONDS) / RATE
    l = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 1330 * t) + 0.004 * rng.normal(size=t.size)
    r = 0.3 * np.sin(2 * np.pi * 330 * t) + 0.1 * np.sin(2 * np.pi * 1770 * t) + 0.004 * rng.normal(size=t.size)
    x = np.stack([l, r], 1)
    x[100000, 0] += 0.8                  # click, left
    x[300000:300240, 1] = 0              # 5 ms gap, right
    x[600000:601500, 0] = 0              # ~31 ms gap: only reported
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    src = prof.root("audio", EP) / "ld_synced.flac"
    src.parent.mkdir(parents=True)
    make_flac(src, x)
    m = Manifest.new(EP, title="t")
    m.set("deliverable.ld_audio_source", "ld_synced", by="t")
    m.set("deliverable.mux_tracks", ["ld"], by="t")
    m.set("video.duration_s", float(SECONDS), by="t")
    m.set_artifact("ld_synced", root="audio", name=src.name)
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws, x


def test_repairs_short_defects_only_and_is_bit_identical_elsewhere(env):
    prof, ws, x = env
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "a1",
            "steps": [{"id": "a", "stage": "audio-quality", "after": [], "params": {}}]}
    ff = FFmpeg("ffmpeg", "ffprobe")
    st = Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": ff}).run()["steps"]["a"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert m.get("quality.audio_events_found") == 3 and m.get("quality.audio_repaired") == 2
    assert m.get("quality.ld_lufs") is not None
    fin = prof.root("audio", EP) / m.artifact("ld_audio_finished")["name"]
    src = prof.root("audio", EP) / "ld_synced.flac"
    a, b = ff.pcm_f32(src, "0:a:0", 2), ff.pcm_f32(fin, "0:a:0", 2)
    assert a.shape == b.shape
    diff = np.flatnonzero((a != b).any(axis=1))
    assert diff.min() >= 99990 and not ((diff > 100010) & (diff < 299990)).any()      # only the two fixed spots changed
    assert diff.max() < 300300 and np.all(b[600000:601500, 0] == 0)                   # the long gap is untouched
    assert abs(b[100000, 0] - x[100000, 0] + 0.8) < 0.05
    import json
    rep = json.loads((prof.root("reports", EP) / m.artifact("audio_quality_report")["name"]).read_text())
    assert rep["ld"]["left_as_captured"] and "longer than" in rep["ld"]["left_as_captured"][0]["why"]
