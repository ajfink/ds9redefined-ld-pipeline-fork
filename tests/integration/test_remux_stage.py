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

pytestmark = [pytest.mark.media, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", *args], check=True)


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    out, audio, subs = prof.root("out", EP), prof.root("audio", EP), prof.root("subs", EP)
    for d in (out, audio, subs):
        d.mkdir(parents=True)
    video = out / "S01E20-23.976-padfixed-video.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24000/1001", "-frames:v", "48", "-c:v", "libx264", str(video))
    # Real LD/5.1 tracks are built to the video's sample budget, so -t never cuts them.
    ff("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1.9", "-ac", "2", "-c:a", "flac",
       str(audio / "S01E20-LD20-synced.flac"))
    ff("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000", "-t", "1.9", "-ac", "6", "-c:a", "ac3",
       str(audio / "dvd-51-retimed.ac3"))
    (subs / "retimed.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nHello.\n")
    m = Manifest.new(EP, title="t")
    m.set("video.duration_s", 48 * 1001 / 24000, by="t")
    m.set("audio.capture", "P2", by="t")
    m.set("deliverable.mux_tracks", ["ld", "51", "srt"], by="op")
    m.set("deliverable.ld_audio_source", "ld_synced", by="op")
    m.set_artifact("padfixed_video", root="out", name=video.name)
    m.set_artifact("ld_synced", root="audio", name="S01E20-LD20-synced.flac")
    m.set_artifact("retimed_51", root="audio", name="dvd-51-retimed.ac3")
    m.set_artifact("retimed_srt", root="subs", name="retimed.srt")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws, video


def run(env, job):
    prof, ws, _ = env
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "mux", "stage": "remux", "params": {}, "after": []}]}
    return Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()


def test_remux_needs_no_review_and_proves_identity(env):
    prof, ws, video = env
    state = run(env, "m1")
    st = state["steps"]["mux"]
    assert st["status"] == "passed", st.get("issues")                     # owner: no remux review
    report = json.loads((prof.root("reports", EP) / "remux.json").read_text())
    assert report["status"] == "PASS" and all(r["match"] for r in report["identity"])
    assert [r["stream"] for r in report["identity"]] == ["video", "LD audio", "DVD 5.1", "compatibility audio"]
    streams = FFmpeg("ffmpeg", "ffprobe").probe(prof.root("out", EP) / "S01E20-23.976-padfixed.mkv").streams
    audio = [s.raw for s in streams if s.codec_type == "audio"]
    assert audio[0]["disposition"]["default"] == 1 and audio[1]["disposition"]["default"] == 0
    assert audio[2]["codec_name"] == "aac" and audio[2]["disposition"]["default"] == 0   # compatibility track last
    layout = ws.store.load().get("deliverable.audio_layout")
    assert [t["role"] for t in layout] == ["ld", "51", "compat"]
    # an A/V-check rerun remuxes again: a new version, the first one untouched
    first = (prof.root("out", EP) / "S01E20-23.976-padfixed.mkv").read_bytes()
    assert run(env, "m1-rerun")["steps"]["mux"]["status"] == "passed"
    assert (prof.root("out", EP) / "S01E20-23.976-padfixed-r2.mkv").is_file()
    assert (prof.root("out", EP) / "S01E20-23.976-padfixed.mkv").read_bytes() == first
    assert ws.store.load().artifact("padfixed")["name"] == "S01E20-23.976-padfixed-r2.mkv"


def test_overlong_track_is_caught_by_packet_identity(env):
    prof, ws, video = env
    ff("-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "3.0", "-ac", "2", "-c:a", "flac",
       str(prof.root("audio", EP) / "S01E20-LD20-synced.flac"))
    st = run(env, "m2")["steps"]["mux"]
    assert st["status"] == "failed" and any("LD audio packet hash changed" in i for i in st["issues"])
    assert not (prof.root("out", EP) / "S01E20-23.976-padfixed.mkv").exists()


def test_remux_follows_the_episode_audio_settings(env):
    prof, ws, _ = env
    with ws.store.transaction(by="op", reason="settings") as m:
        m.set("settings.output", {"audio": {"stereo_codec": "opus", "stereo_bitrate": 256, "include_51": False,
                                            "compat": False}}, by="op")
    st = run(env, "m-settings")["steps"]["mux"]
    assert st["status"] == "passed", st.get("issues")
    out = prof.root("out", EP) / "S01E20-23.976-padfixed.mkv"
    audio = [s.raw for s in FFmpeg("ffmpeg", "ffprobe").probe(out).streams if s.codec_type == "audio"]
    assert [a["codec_name"] for a in audio] == ["opus"]
    report = json.loads((prof.root("reports", EP) / "remux.json").read_text())
    assert all(r["match"] for r in report["identity"])          # identity checked against the encoded file
