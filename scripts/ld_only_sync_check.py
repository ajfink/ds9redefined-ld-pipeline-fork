#!/usr/bin/env python3
"""Validate the no-DVD LD-audio timeline and exact output sample budget.

This is deliberately not an absolute lip-sync cross-correlation: without a DVD
or another shared reference, that measurement is unavailable. It verifies the
user-approved duration/PAD method, selected capture's measured leading offset,
source/report hashes, and exact sample count against the final video timeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

FPS_NUM, FPS_DEN = 24000, 1001
SAMPLE_RATE = 44100


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def probe_stream(ld: Ld, media: Path, selector: str) -> dict:
    p = subprocess.run(
        [ld.ffprobe(), "-v", "error", "-select_streams", selector,
         "-show_entries", "stream=codec_name,sample_rate,channels,duration_ts,time_base",
         "-of", "json", str(media)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise PipelineError(f"ffprobe failed for {media} {selector}: {p.stderr[-800:]}")
    try:
        streams = json.loads(p.stdout or "{}").get("streams", [])
    except json.JSONDecodeError as exc:
        raise PipelineError(f"invalid ffprobe JSON for {media}: {exc}") from exc
    if len(streams) != 1:
        raise PipelineError(f"expected one {selector} stream in {media}; found {len(streams)}")
    return streams[0]


def exact_audio_samples(stream: dict) -> int:
    try:
        duration_ts = int(stream["duration_ts"])
        time_base = Fraction(stream["time_base"])
        sample_rate = int(stream["sample_rate"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise PipelineError(f"audio stream lacks exact duration/time-base metadata: {stream}") from exc
    exact = duration_ts * time_base * sample_rate
    if exact.denominator != 1:
        raise PipelineError(f"audio duration does not resolve to an integer sample count: {exact}")
    return exact.numerator


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--video", help="override the delivered video/audio file")
    ap.add_argument("--report-out", help="new JSON evidence report path")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    data = ep.data
    if data.get("dvd_reference_available") is not False or data.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("no-DVD sync check requires an explicitly approved no-DVD episode manifest")
    tracks = list(data.get("deliverable_tracks") or ["ld"])
    if tracks != ["ld"]:
        raise PipelineError("no-DVD delivery must contain LD stereo only; DVD-derived tracks cannot be claimed")
    audio = data.get("audio") or {}
    if audio.get("dvd_free_approved") is not True or audio.get("rate_method") != "dvd-free-duration-pad":
        raise PipelineError("manifest has no promoted DVD-free duration/PAD audio recipe")
    if audio.get("origin_verified_by_duration") is not True:
        raise PipelineError("DVD-free capture-origin/duration evidence is not marked verified")

    report_path = ep.optional_path("audio_timing_report")
    if report_path is None or not report_path.is_file():
        raise PipelineError("DVD-free audio timing report missing from the manifest")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read DVD-free timing report {report_path}: {exc}") from exc
    if report.get("dvd_opened") is not False or report.get("promoted_to_manifest") is not True:
        raise PipelineError("timing report does not prove DVD-free production and manifest promotion")
    if report.get("is_approval") is not True:
        raise PipelineError("timing report is still candidate-only")
    duration_rate = float(report.get("duration_candidate_effective_rate_hz") or 0.0)
    recorded_rate = float(audio.get("duration_candidate_rate_hz") or 0.0)
    if duration_rate <= 0 or abs(duration_rate - recorded_rate) > 1e-6:
        raise PipelineError("manifest duration-rate value differs from the promoted timing report")
    residual = report.get("audio_minus_projected_content_frames")
    tolerance = float(audio.get("endpoint_tolerance_frames", 1.0))
    if residual is None or abs(float(residual)) > tolerance:
        raise PipelineError(f"PAD/duration endpoint residual {residual!r} exceeds ±{tolerance:.3f} frames")

    native = ep.path("ld_native")
    corrected = ep.path("ld_corrected")
    synced = ep.path("ld_synced")
    for path in (native, corrected, synced):
        if not path.is_file():
            raise PipelineError(f"required no-DVD audio artifact missing: {path}")
    if sha256(native) != report.get("source_audio_sha256"):
        raise PipelineError("native EFM WAV hash differs from the promoted timing report")
    full = report.get("experimental_candidate") or {}
    synced_report = report.get("experimental_synced_candidate") or {}
    if sha256(corrected) != full.get("output_sha256") or Path(full.get("path", "")).resolve() != corrected.resolve():
        raise PipelineError("corrected LD audio path/hash differs from the promoted timing report")
    if sha256(synced) != synced_report.get("output_sha256") or Path(synced_report.get("path", "")).resolve() != synced.resolve():
        raise PipelineError("synced LD audio path/hash differs from the promoted timing report")

    video_data = data.get("video") or {}
    frames = int(video_data.get("frames") or 0)
    delta_s = video_data.get("delta_s")
    pad_frames = audio.get("pad_frames")
    if frames <= 0 or delta_s is None or pad_frames is None or audio.get("skip_s") is None:
        raise PipelineError("measured output frames, video.delta_s, and audio.pad_frames are required")
    expected_skip = float(delta_s) - int(pad_frames) / (30000.0 / 1001.0)
    measured_skip = float(audio.get("skip_s"))
    if abs(measured_skip - expected_skip) > 0.5 / SAMPLE_RATE:
        raise PipelineError(f"audio.skip_s {measured_skip:.9f} disagrees with measured capture-pad geometry "
                            f"{expected_skip:.9f}s by more than half a sample")
    sync_recipe = report["experimental_synced_candidate"]
    if abs(float(sync_recipe.get("skip_s") or 0.0) - measured_skip) > 0.5 / SAMPLE_RATE:
        raise PipelineError("manifest AudioSkip differs from the promoted DVD-free timing recipe")
    expected_start = math.floor(measured_skip * SAMPLE_RATE + 1e-9)
    if int(sync_recipe.get("start_sample", -1)) != expected_start:
        raise PipelineError("synced audio start sample differs from the promoted timing recipe")

    video = Path(args.video) if args.video else ep.path(data.get("deliverable") or "pre_upscale")
    if not video.is_file():
        raise PipelineError(f"delivered video missing: {video}")
    video_stream = probe_stream(ld, video, "v:0")
    source_audio_stream = probe_stream(ld, synced, "a:0")
    delivered_audio_stream = probe_stream(ld, video, "a:0")
    if (delivered_audio_stream.get("codec_name") != "flac" or
            delivered_audio_stream.get("sample_rate") != str(SAMPLE_RATE) or
            int(delivered_audio_stream.get("channels") or 0) != 2):
        raise PipelineError(f"delivered LD track has unexpected stream format: {delivered_audio_stream}")
    actual_samples = exact_audio_samples(source_audio_stream)
    target_samples = round(frames * FPS_DEN * SAMPLE_RATE / FPS_NUM)
    if actual_samples != target_samples:
        raise PipelineError(f"delivered audio has {actual_samples} samples; expected {target_samples} "
                            f"for {frames} video frames")
    if int(sync_recipe.get("target_samples", -1)) != target_samples:
        raise PipelineError("promoted audio report target sample count disagrees with video.frames")

    result = {
        "schema": "ld-only-sync-check-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code,
        "sync_method": "duration/PAD rate + Orc capture-pad geometry + exact video sample budget",
        "external_absolute_av_reference_available": False,
        "dvd_opened_for_timing_or_sync": False,
        "dvd_free_proceed_user_approved": True,
        "absolute_lip_sync_caveat": "No DVD or other external reference was supplied; absolute A/V offset was not independently cross-correlated.",
        "dvd_free_timing_report": str(report_path),
        "dvd_free_timing_report_sha256": sha256(report_path),
        "native_efm_sha256": sha256(native),
        "corrected_audio_sha256": sha256(corrected),
        "synced_audio_sha256": sha256(synced),
        "video": str(video),
        "video_frames": frames,
        "video_stream": video_stream,
        "audio_stream": delivered_audio_stream,
        "synced_source_stream": source_audio_stream,
        "actual_audio_samples": actual_samples,
        "target_audio_samples": target_samples,
        "skip_s": measured_skip,
        "start_sample": sync_recipe["start_sample"],
        "endpoint_residual_frames": float(residual),
        "endpoint_tolerance_frames": tolerance,
        "status": "PASS_WITHOUT_EXTERNAL_ABSOLUTE_AV_REFERENCE",
    }
    out = Path(args.report_out) if args.report_out else ld.probe() / (
        f"{ep.code}-ld-only-sync-check-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.json")
    if out.exists():
        raise PipelineError(f"refusing to overwrite sync report: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"PASS - no-DVD LD timebase and exact endpoint sample budget verified; report: {out}")
    print("WARN - absolute lip-sync offset was not independently cross-correlated (no DVD reference was supplied).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
