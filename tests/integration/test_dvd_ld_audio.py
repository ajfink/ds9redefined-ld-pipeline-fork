"""dvd-ld-audio end to end: real FFmpeg decodes of a synthetic DVD stereo track and LD EFM WAV."""
from __future__ import annotations

import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.media import dvd_audio as da
from fstk.stages import dvdaudio
from fstk.tools.ffmpeg import FFmpeg
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace
from tests.unit.test_dvd_audio import ACTS, ICPT, SLOPE, content

pytestmark = [pytest.mark.media, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")
FRAMES = 7000                       # 23.976 output frames ≈ 292 s
DELTA = 5.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(dvdaudio.ACCEPT, "min_windows", 12)        # a short synthetic side
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    audio = prof.root("audio", EP)
    audio.mkdir(parents=True)
    sr = 44100
    t = np.arange(int(320 * sr)) / sr
    x = np.clip(content(ICPT + SLOPE * t), -32767, 32767).astype("<i2")
    with wave.open(str(audio / "M1-efm.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.column_stack([x, x]).tobytes())
    amap = da.ActMap(ACTS)
    td = np.arange(325 * 48000) / 48000
    coarse = td[::480]
    m = np.array([amap.dvd_to_master_s(v) if amap.act_of_dvd(v * da.FPS) is not None else np.nan for v in coarse])
    y = np.clip(content(np.interp(td, coarse[np.isfinite(m)], m[np.isfinite(m)])), -32767, 32767).astype("<i2")
    raw = tmp_path / "dvd.s16"
    np.column_stack([y, y]).tofile(raw)
    dvd = tmp_path / "dvd.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=100:sample_rate=48000",
                    "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", str(raw), "-map", "0:a", "-map", "1:a",
                    "-ac:a:0", "6", "-t", "325", "-c:a", "flac", str(dvd)], check=True, timeout=300)
    mf = Manifest.new(EP, title="In the Hands of the Prophets")
    mf.set("mode.dvd_reference_available", True, by="op")
    mf.set("video.dvd_alignment", {"decision": "AUTO-ACCEPTED", "acts": ACTS}, by="t")
    for key, value in (("audio.capture", "M1"), ("audio.pad_frames", 96), ("video.frames", FRAMES),
                       ("video.delta_s", DELTA)):
        mf.set(key, value, by="t")
    mf.set_artifact("ld_native", root="audio", name="M1-efm.wav")
    mf.set_artifact("dvd", path=str(dvd))
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(mf, by="t", reason="setup")
    return prof, ws


def run(prof, job):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "audio", "stage": "dvd-ld-audio", "params": {}, "after": []}]}
    return Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()


def test_measures_the_clock_against_the_dvd_and_builds_the_synced_track(env):
    prof, ws = env
    st = run(prof, "a1")["steps"]["audio"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert abs(m.get("audio.wav0_master_frame") / da.FPS - ICPT) < 0.003
    assert abs(m.get("audio.asetrate") - 44100 / SLOPE) < 0.2
    assert abs(m.get("audio.skip_s") - (DELTA - ICPT)) < 0.003
    rep = json.loads((prof.root("reports", EP) / m.artifact("audio_timing_report")["name"]).read_text())
    assert rep["dvd_stereo_stream"] == "0:a:1"                        # the unique 2-channel stream
    assert rep["resample"]["synced_samples"] == round(FRAMES * 1001 * 44100 / 24000)
    assert rep["cross_checks"]["origin_minus_pad_geometry_frames"] == pytest.approx(ICPT * da.FPS - 96, abs=0.1)


def test_av_check_correction_is_applied_on_a_dvd_episode(env):
    prof, ws = env
    assert run(prof, "a1")["steps"]["audio"]["status"] == "passed"
    first = ws.store.load().get("audio.skip_s")
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "fix",
            "steps": [{"id": "fix", "stage": "av-correction", "after": [],
                       "params": {"track": "ld", "verdicts": [{"t_s": t, "verdict": "late"} for t in (40, 120, 250)]}},
                      {"id": "audio", "stage": "dvd-ld-audio", "params": {}, "after": ["fix"]}]}
    state = Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()
    assert state["steps"]["audio"]["status"] == "passed", state["steps"]
    m = ws.store.load()
    off = m.get("audio.av_correction")["ld"]["current"]["offset_ms"]
    assert off < 0 and abs(m.get("audio.skip_s") - (first - off / 1000)) < 1e-6   # audio now earlier
