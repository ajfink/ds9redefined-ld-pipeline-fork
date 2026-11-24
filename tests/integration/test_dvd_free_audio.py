"""dvd-free-audio stage + DVD-free QC check with real FFmpeg on a synthetic native EFM WAV."""
from __future__ import annotations

import json
import shutil
import struct
import wave

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.hashing import sha256_file
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.qc.dvdfree import check_dvd_free, exact_audio_samples, target_samples
from fstk.tools.ffmpeg import FFmpeg
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = [pytest.mark.media, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg")]
EP = EpisodeId.parse("DS9:S01E07")
FPS = 30000 / 1001
STACK_FRAMES = 3000                       # the pad-free stack master (ADR 0004)
OUT_FRAMES = 2380


def write_native(path, equiv_frames: float):
    n = round(equiv_frames / FPS * 44100)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(struct.pack("<hh", (i * 37) % 2000 - 1000, (i * 53) % 2000 - 1000)
                               for i in range(n)))


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    return prof


def setup(prof, *, audio_equiv_frames=STACK_FRAMES - 0.4, no_dvd=True):
    audio = prof.root("audio", EP)
    audio.mkdir(parents=True)
    write_native(audio / "P2-efm.wav", audio_equiv_frames)
    m = Manifest.new(EP, title="The Passenger")
    for key, value, note in (("mode.dvd_reference_available", False, "no DVD for this episode"),
                             ("mode.no_dvd_proceed_approved", no_dvd, "operator chose LD-only")):
        m.set(key, value, by="op", note=note)
    m.set("audio.capture", "P2", by="op", note="fixture")
    m.set("audio.pad_frames", 0, by="t")
    m.set("stack.reference", "P2", by="t", note="fixture")
    m.set("stack.frames", STACK_FRAMES, by="t")
    m.set("video.frames", OUT_FRAMES, by="t")
    m.set("video.head_trim", 0, by="t")
    m.set("video.delta_s", 1.0, by="t")
    m.set_artifact("ld_native", root="audio", name="P2-efm.wav")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return ws


def run(prof, job):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "audio", "stage": "dvd-free-audio", "params": {}, "after": []}]}
    return Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()


def test_builds_exact_sample_budget_and_passes_the_qc_check(env):
    ws = setup(env)
    st = run(env, "a1")["steps"]["audio"]
    assert st["status"] == "passed", st
    m = ws.store.load()
    assert m.get("audio.rate_method") == "dvd-free-native-rate" and m.get("audio.dvd_free_approved") is True
    # the duration rate is recorded for information; the audio plays at the disc's own 44.1 kHz
    assert m.get("audio.duration_candidate_rate_hz") == pytest.approx(44100 * (STACK_FRAMES - 0.4) / STACK_FRAMES,
                                                                      rel=1e-5)
    assert m.get("audio.asetrate") == 44100.0
    assert m.get("audio.skip_s") == pytest.approx(1.0) and abs(m.get("audio.endpoint_residual_frames")) < 1
    assert m.get("deliverable.ld_audio_source") == "ld_synced" and m.get("deliverable.mux_tracks") == ["ld"]
    ff = FFmpeg("ffmpeg", "ffprobe")
    paths = {k: env.root(m.artifact(k)["root"], EP) / m.artifact(k)["name"]
             for k in ("ld_native", "ld_corrected", "ld_synced", "audio_timing_report")}
    synced_stream = ff.probe(paths["ld_synced"]).only("audio").raw
    assert exact_audio_samples(synced_stream) == target_samples(OUT_FRAMES)
    report = json.loads(paths["audio_timing_report"].read_text())
    assert report["dvd_opened"] is False and report["effective_rate_hz"] == 44100.0
    assert not any(k.startswith("map_pad") for k in report)
    keys = ("mode.dvd_reference_available", "mode.no_dvd_proceed_approved", "deliverable.tracks",
            "audio.dvd_free_approved", "audio.rate_method", "audio.origin_verified_by_duration",
            "audio.duration_candidate_rate_hz", "audio.endpoint_tolerance_frames", "video.frames", "video.delta_s",
            "audio.pad_frames", "audio.skip_s")
    values = {k: m.get(k) for k in keys}
    shas = {"native_sha256": sha256_file(paths["ld_native"]), "corrected_sha256": sha256_file(paths["ld_corrected"]),
            "synced_sha256": sha256_file(paths["ld_synced"])}
    assert check_dvd_free(values, report, **shas, synced_stream=synced_stream, delivered_stream=synced_stream) == []
    # tampering is caught: a different skip, a re-encoded synced file, a changed frame count
    assert any("capture-pad geometry" in i for i in
               check_dvd_free({**values, "audio.skip_s": 1.01}, report, **shas, synced_stream=synced_stream,
                              delivered_stream=synced_stream))
    assert any("synced LD audio hash" in i for i in
               check_dvd_free(values, report, **{**shas, "synced_sha256": "0" * 64}, synced_stream=synced_stream,
                              delivered_stream=synced_stream))
    assert any("video frames need" in i for i in
               check_dvd_free({**values, "video.frames": OUT_FRAMES + 1}, report, **shas,
                              synced_stream=synced_stream, delivered_stream=synced_stream))


def test_endpoint_residual_over_two_frames_blocks_without_writing(env):
    ws = setup(env, audio_equiv_frames=STACK_FRAMES - 2.5)
    st = run(env, "a2")["steps"]["audio"]
    assert st["status"] == "blocked" and "endpoint residual" in st["issues"][0]
    assert not ws.store.load().is_set("audio.rate_method")
    assert sorted(p.name for p in env.root("audio", EP).iterdir()) == ["P2-efm.wav"]


def test_requires_the_operators_no_dvd_choice(env):
    setup(env, no_dvd=False)
    st = run(env, "a3")["steps"]["audio"]
    assert st["status"] == "blocked" and "no_dvd_proceed_approved" in st["issues"][0]


def test_v38_recipe_deliveries_still_verify_under_their_own_rule(env):
    """A delivery made with v38's duration/PAD recipe (audio stretched to cover the PADs) keeps passing QC;
    the same report claiming the new native-rate method does not (ADR 0004)."""
    from fstk.qc.dvdfree import LEGACY_RATE_METHOD

    ws = setup(env)
    assert run(env, "a4")["steps"]["audio"]["status"] == "passed"
    m = ws.store.load()
    ff = FFmpeg("ffmpeg", "ffprobe")
    paths = {k: env.root(m.artifact(k)["root"], EP) / m.artifact(k)["name"]
             for k in ("ld_native", "ld_corrected", "ld_synced", "audio_timing_report")}
    stream = ff.probe(paths["ld_synced"]).only("audio").raw
    report = json.loads(paths["audio_timing_report"].read_text())
    legacy = {**report, "effective_rate_hz": report["duration_candidate_effective_rate_hz"]}
    keys = ("mode.dvd_reference_available", "mode.no_dvd_proceed_approved", "deliverable.tracks",
            "audio.dvd_free_approved", "audio.origin_verified_by_duration", "audio.duration_candidate_rate_hz",
            "audio.endpoint_tolerance_frames", "video.frames", "video.delta_s", "audio.pad_frames", "audio.skip_s")
    values = {k: m.get(k) for k in keys}
    shas = {"native_sha256": sha256_file(paths["ld_native"]), "corrected_sha256": sha256_file(paths["ld_corrected"]),
            "synced_sha256": sha256_file(paths["ld_synced"])}
    common = {**shas, "synced_stream": stream, "delivered_stream": stream}
    assert check_dvd_free({**values, "audio.rate_method": LEGACY_RATE_METHOD}, legacy, **common) == []
    assert any("applied rate" in i for i in
               check_dvd_free({**values, "audio.rate_method": "dvd-free-native-rate"}, legacy, **common))
