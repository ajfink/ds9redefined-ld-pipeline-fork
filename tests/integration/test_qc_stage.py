"""qc-report against a real x265/FLAC Matroska deliverable built with FFmpeg."""
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
FRAMES = 492
DURATION = FRAMES * 1001 / 24000
BLACK = 240


SOURCE_INPUTS = ["-f", "lavfi", "-i", "testsrc2=size=768x484:rate=24000/1001",
                 "-f", "lavfi", "-i", "cellauto=s=768x484:rate=24000/1001:rule=30:full=0"]
BLEND = "[1:v]format=yuv420p[c];[0:v][c]blend=all_mode=average"


def build(path, *, black_frame=None, audio=None):
    """x265 Main10 768×484 video + LD FLAC (a generated tone, or a stream copy of ``audio``)."""
    vf = "format=yuv420p10le"
    if black_frame is not None:
        vf = f"drawbox=enable='eq(n,{black_frame})':color=black:t=fill," + vf
    if audio is None:
        audio_in = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
        audio_codec = ["-c:a", "flac", "-sample_fmt", "s16", "-ac", "2", "-t", f"{DURATION:.6f}"]
    else:
        audio_in, audio_codec = ["-i", str(audio)], ["-c:a", "copy"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *SOURCE_INPUTS, *audio_in,
                    "-filter_complex", f"{BLEND},{vf}[v]", "-map", "[v]", "-map", "2:a", "-frames:v", str(FRAMES),
                    "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", *audio_codec,
                    "-metadata:s:v:0", "title=LaserDisc P1, 3:2 pulldown removed, 23.976p",
                    "-disposition:v:0", "default", "-metadata:s:a:0", "title=LD 2.0 (original Dolby Surround)",
                    "-metadata:s:a:0", "language=eng", "-disposition:a:0", "default",
                    "-max_interleave_delta", "0", str(path)], check=True, timeout=300)


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    out, probe = prof.root("out", EP), prof.root("probe", EP)
    out.mkdir(parents=True)
    probe.mkdir(parents=True)
    return prof, out


def setup(prof, out, deliverable):
    ff = FFmpeg("ffmpeg", "ffprobe")
    m = Manifest.new(EP, title="In the Hands of the Prophets")
    m.set("video.frames", FRAMES, by="t")
    m.set("video.duration_s", float(ff.probe(deliverable).format["duration"]), by="t")
    m.set("deliverable.tracks", ["ld"], by="op")
    m.set("mode.chroma_output", "420", by="op")        # this fixture's video is 4:2:0
    m.set("mode.dvd_reference_available", False, by="op", note="fixture has no DVD")
    m.set("qc.ld_audio_md5", ff.packet_md5(deliverable, "0:a:0"), by="t")
    m.set_artifact("padfixed", root="out", name=deliverable.name)
    EpisodeWorkspace(prof, EP).store.create(m, by="t", reason="setup")


def run(prof, job):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "qc", "stage": "qc-report", "params": {}, "after": []}]}
    return Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()


def _report(prof):
    (path,) = sorted(prof.root("reports", EP).glob("qc-*.json"))
    return json.loads(path.read_text())


def test_picture_gates_pass_but_audio_without_a_dvd_free_recipe_fails(env):
    prof, out = env
    d = out / "S01E20-23.976-padfixed.mkv"
    build(d)
    setup(prof, out, d)
    st = run(prof, "q1")["steps"]["qc"]
    # every picture/container gate passes; the LD audio was never built by dvd-free-audio, so the
    # DVD-free timeline cannot be proven and delivery stays blocked (fail closed)
    assert st["status"] == "failed"
    assert st["issues"] == ["QC gate failed: DVD-free sync"]
    rep = _report(prof)
    status = {r["gate"]: r["status"] for r in rep["results"]}
    assert "replacement identity" not in status          # retired with the PAD frames (ADR 0004)
    for gate in ("full decode + single black frames", "frame count", "stream inventory", "audio seekability",
                 "video packet hash", "LD audio hash", "manifest cross-check"):
        assert status[gate] == "PASS", (gate, rep["results"])
    assert status["absolute A/V"] == "SKIP" and rep["candidate_is_approval"] is False
    assert rep["frame_count"] == FRAMES and rep["review_intervals"] == []
    assert not EpisodeWorkspace(prof, EP).store.load().is_set("qc.video_md5")   # nothing committed
    assert (prof.root("reports", EP) / (rep_name := f"qc-{rep['run']}.md")).is_file(), rep_name


def test_single_black_frame_warns_with_a_review_interval(env):
    prof, out = env
    d = out / "S01E20-23.976-padfixed.mkv"
    build(d, black_frame=BLACK)
    setup(prof, out, d)
    run(prof, "q2")
    rep = _report(prof)
    gate = next(r for r in rep["results"] if r["gate"] == "full decode + single black frames")
    assert gate["status"] == "WARN", json.dumps(rep["results"], indent=1)[:3000]
    assert [iv["id"] for iv in rep["review_intervals"]] == [f"black-f{BLACK}"]
    assert rep["review_intervals"][0]["start_s"] < BLACK / (24000 / 1001)


def _no_dvd_episode(prof, out):
    """LD-only episode with dvd-free-audio run and the deliverable built from its synced audio."""
    from tests.integration.test_dvd_free_audio import write_native

    audio_root, stack = prof.root("audio", EP), prof.root("stack", EP)
    audio_root.mkdir(parents=True)
    stack.mkdir(parents=True)
    write_native(audio_root / "P1-efm.wav", 614)        # one frame short of the 615-frame stack: inside ±2
    (stack / "stack.orcprj").write_text(
        "dag:\n  nodes:\n    - id: 1\n      stage: tbc_source\n      user_label: TBC Source P1\n"
        "    - id: 2\n      stage: frame_map\n      parameters:\n        ranges:\n          type: string\n"
        "          value: 0-299,PAD_1,300-613\n  edges:\n    - from: 1\n      to: 2\n", encoding="utf-8")
    m = Manifest.new(EP, title="In the Hands of the Prophets")
    m.set("mode.chroma_output", "420", by="op")
    m.set("mode.dvd_reference_available", False, by="op", note="no DVD for this episode")
    m.set("mode.no_dvd_proceed_approved", True, by="op", note="operator chose LD-only")
    for key, value in (("audio.pad_frames", 0), ("stack.frames", 615), ("video.frames", FRAMES),
                       ("video.head_trim", 0), ("video.delta_s", 0.5)):
        m.set(key, value, by="t")
    m.set("audio.capture", "P1", by="op", note="fixture")
    m.set("stack.reference", "P1", by="t", note="fixture")
    m.set_artifact("ld_native", root="audio", name="P1-efm.wav")
    m.set_artifact("stack_project", root="stack", name="stack.orcprj")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    ff = FFmpeg("ffmpeg", "ffprobe")
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "audio",
            "steps": [{"id": "audio", "stage": "dvd-free-audio", "params": {}, "after": []}]}
    assert Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": ff}).run()["steps"]["audio"]["status"] == "passed"
    _deliver(prof, out, ws, "S01E20-23.976-padfixed.mkv")
    return ws, ff


def _deliver(prof, out, ws, name):
    ff = FFmpeg("ffmpeg", "ffprobe")
    synced = prof.root("audio", EP) / ws.store.load().artifact("ld_synced")["name"]
    d = out / name
    build(d, audio=synced)
    with ws.store.transaction(by="t", reason="deliverable built") as m:
        m.set("video.duration_s", float(ff.probe(d).format["duration"]), by="t")
        m.set_artifact("padfixed", root="out", name=d.name)


def test_no_dvd_episode_passes_full_qc_and_takes_a_delivery_approval(env):
    """End to end for an LD-only episode: dvd-free-audio → deliverable → every QC gate → approval."""
    from fstk.evidence.approvals import ApprovalBook

    prof, out = env
    ws, ff = _no_dvd_episode(prof, out)
    st = run(prof, "q4")["steps"]["qc"]
    assert st["status"] == "awaiting-review", st
    rep = _report(prof)
    assert rep["summary"]["fail"] == 0, rep["results"]
    status = {r["gate"]: r["status"] for r in rep["results"]}
    assert status["DVD-free sync"] == "PASS" and status["LD audio hash"] == "PASS"
    assert not ws.store.load().is_set("qc.video_md5")                    # held until a human approves
    report_json = prof.root("reports", EP) / f"qc-{rep['run']}.json"
    report_md = report_json.with_suffix(".md")
    ApprovalBook(ws.state_dir / "approvals").record(review="delivery", decision="APPROVED", actor="op",
                                                    note="watched the review intervals and listened to the LD track",
                                                    evidence=[report_json, report_md])
    assert run(prof, "q4")["steps"]["qc"]["status"] == "passed"
    m = ws.store.load()
    assert m.get("qc.video_md5") == rep["packet_hashes"]["video"]


def test_av_check_rerun_corrects_the_ld_audio_and_qc_still_proves_it(env):
    """The owner's A/V check: chunks are offered; "audio too early" → av-correction → the LD audio is
    rebuilt later by the solved offset → new deliverable → QC's DVD-free gate accepts the correction."""
    prof, out = env
    ws, ff = _no_dvd_episode(prof, out)
    assert run(prof, "q5")["steps"]["qc"]["status"] == "awaiting-review"
    rep = _report(prof)
    chunks = rep["av_check"]["chunks"]
    assert chunks and all(c["end_s"] > c["start_s"] for c in chunks)
    first_start = json.loads((prof.root("reports", EP) / ws.store.load().artifact("audio_timing_report")["name"])
                             .read_text())["synced"]["start_sample"]
    verdicts = [{"t_s": c["t_s"], "verdict": "early"} for c in chunks]
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "rerun1",
            "steps": [{"id": "fix", "stage": "av-correction", "after": [],
                       "params": {"track": "ld", "verdicts": verdicts, "qc_report": f"qc-{rep['run']}.json"}},
                      {"id": "audio", "stage": "dvd-free-audio", "params": {}, "after": ["fix"]}]}
    state = Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": ff}).run()
    assert state["steps"]["audio"]["status"] == "passed", state["steps"]
    m = ws.store.load()
    cur = m.get("audio.av_correction")["ld"]["current"]
    assert 20 < cur["offset_ms"] <= 80 and cur["drift_ppm"] == 0
    timing = json.loads((prof.root("reports", EP) / m.artifact("audio_timing_report")["name"]).read_text())
    assert timing["av_correction"] == cur
    shift = first_start - timing["synced"]["start_sample"]                 # audio now starts later
    assert abs(shift - cur["offset_ms"] / 1000 * 44100) <= 1
    _deliver(prof, out, ws, "S01E20-23.976-padfixed-r2.mkv")
    run(prof, "q6")
    rep2 = max(prof.root("reports", EP).glob("qc-*.json"), key=lambda p: p.stat().st_mtime_ns)
    status = {r["gate"]: r["status"] for r in json.loads(rep2.read_text())["results"]}
    assert status["DVD-free sync"] == "PASS", status
