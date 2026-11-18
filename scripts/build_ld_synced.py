#!/usr/bin/env python3
"""Build the sample-accurate, output-duration LD FLAC used by the remux stage.

This is an audio-only operation. It consumes the measured full-side
``ld_corrected`` 44.1 kHz/16-bit stereo WAV or FLAC, trims by the manifest's measured AudioSkip in samples,
then pads/trims to the exact video-frame duration. It never edits the manifest,
reads a DVD, or re-encodes video. Existing output files are never overwritten.

  python scripts/build_ld_synced.py --episode S01E07 --dry-run
  python scripts/build_ld_synced.py --episode S01E07
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import (Ld, PipelineError, assert_writable_path, load_episode,
                      redact_shared_text)  # noqa: E402
from fileutil import hash_file  # noqa: E402

SAMPLE_RATE = 44100
FPS_NUM, FPS_DEN = 24000, 1001


def sample_plan(skip_s: float, frames: int) -> tuple[int, int]:
    if skip_s < 0 or frames <= 0:
        raise PipelineError("AudioSkip must be nonnegative and video frame count positive")
    start_sample = math.floor(skip_s * SAMPLE_RATE + 1e-9)
    target_samples = round(frames * FPS_DEN * SAMPLE_RATE / FPS_NUM)
    return start_sample, target_samples


def sha256(path: Path) -> str:
    return hash_file(path, chunk_size=8 * 1024 * 1024)


def probe_audio(ld: Ld, path: Path) -> dict:
    p = subprocess.run([ld.ffprobe(), "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,duration,duration_ts,time_base",
                        "-of", "json", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode:
        raise PipelineError(f"ffprobe failed on synced FLAC: {p.stderr[-800:]}")
    try:
        streams = json.loads(p.stdout).get("streams", [])
    except json.JSONDecodeError as e:
        raise PipelineError(f"invalid ffprobe JSON for synced FLAC: {e}") from e
    if len(streams) != 1:
        raise PipelineError(f"expected one audio stream in {path}; found {len(streams)}")
    return streams[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--input", help="override files.ld_corrected")
    ap.add_argument("--output", help="override files.ld_synced; must be a new .flac path")
    ap.add_argument("--skip-s", type=float, help="experimental measured AudioSkip override; does not edit the manifest")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    source = Path(args.input) if args.input else ep.path("ld_corrected")
    output = Path(args.output) if args.output else ep.path("ld_synced")
    assert_writable_path(output, ld)
    if not source.is_file():
        raise PipelineError(f"corrected LD audio missing: {source}")
    if output.suffix.lower() != ".flac":
        raise PipelineError(f"synced LD output must be FLAC: {output}")
    if output.exists():
        raise PipelineError(f"refusing to overwrite synced audio: {output}; preserve it and choose a new --output")
    info = probe_audio(ld, source)
    bit_depth = int(info.get("bits_per_raw_sample") or info.get("bits_per_sample") or 0)
    if info.get("sample_rate") != str(SAMPLE_RATE) or int(info.get("channels") or 0) != 2 or bit_depth not in (0, 16):
        raise PipelineError(f"expected corrected 44.1 kHz / 16-bit / stereo audio; got {info}")
    skip = float(args.skip_s if args.skip_s is not None else ep.audio_skip_s)
    frames = int(ep.frame_count)
    duration = float(ep.duration_s)
    expected_duration = frames * FPS_DEN / FPS_NUM
    if abs(duration - expected_duration) > 0.002:
        raise PipelineError(f"video.duration_s={duration:.9f} disagrees with exact frame duration {expected_duration:.9f}")
    start_sample, target_samples = sample_plan(skip, frames)
    available_samples = max(0, round(float(info.get("duration") or 0.0) * SAMPLE_RATE) - start_sample)
    if available_samples == 0:
        raise PipelineError("AudioSkip reaches/passes the end of corrected WAV")

    filt = (f"atrim=start_sample={start_sample},asetpts=PTS-STARTPTS,"
            f"apad=whole_len={target_samples},atrim=end_sample={target_samples}")
    temp = output.with_name(output.stem + ".tmp.flac")
    if temp.exists():
        raise PipelineError(f"temporary synced FLAC exists; inspect it before retrying: {temp}")
    cmd = [ld.ffmpeg(), "-n", "-hide_banner", "-v", "error", "-i", str(source),
           "-map", "0:a:0", "-af", filt, "-c:a", "flac", "-compression_level", "8",
           "-sample_fmt", "s16", "-ar", str(SAMPLE_RATE), "-f", "flac", str(temp)]

    print(f"episode      : {ep.code}")
    print(f"source       : {source}")
    print(f"input        : {info}")
    print(f"AudioSkip    : {skip:.9f}s -> start_sample={start_sample} (floor at {SAMPLE_RATE} Hz)")
    print(f"target       : {frames} frames -> {target_samples} samples / {target_samples/SAMPLE_RATE:.9f}s")
    print(f"filter       : {filt}")
    print(f"output       : {output}")
    print(f"ffmpeg args  : {cmd}")
    if args.dry_run:
        print("DRY RUN - no output audio was written")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        temp.unlink(missing_ok=True)
        raise PipelineError(f"synced audio build failed ({p.returncode}): {p.stderr[-1200:]}")
    if not temp.is_file():
        raise PipelineError("FFmpeg reported success but temporary FLAC is missing")
    stream = probe_audio(ld, temp)
    actual_duration = float(stream.get("duration") or 0.0)
    duration_error_s = actual_duration - target_samples / SAMPLE_RATE
    if stream.get("codec_name") != "flac" or int(stream.get("sample_rate") or 0) != SAMPLE_RATE or int(stream.get("channels") or 0) != 2:
        temp.unlink(missing_ok=True)
        raise PipelineError(f"synced output stream properties failed: {stream}")
    if abs(duration_error_s) > 1.5 / SAMPLE_RATE:
        temp.unlink(missing_ok=True)
        raise PipelineError(f"synced output duration differs from target by {duration_error_s*1000:.3f} ms")
    temp.rename(output)
    record = {
        "schema": "ld-synced-audio-v1", "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(), "episode": ep.code,
        "source": str(source), "source_sha256": sha256(source), "input_pcm": info,
        "skip_s": skip, "skip_source": "command-line override" if args.skip_s is not None else "episode manifest",
        "start_sample": start_sample, "target_video_frames": frames,
        "target_samples": target_samples, "target_duration_s": target_samples / SAMPLE_RATE,
        "filter": filt, "ffmpeg_args": cmd, "output": str(output), "output_sha256": sha256(output),
        "output_stream": stream, "duration_error_ms": duration_error_s * 1000.0,
    }
    report = ld.root("logs") / f"audio-sync-{ep.code}-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.json"
    assert_writable_path(report, ld)
    report.parent.mkdir(parents=True, exist_ok=True)
    report_text = json.dumps(record, indent=2) + "\n"
    report.write_text(redact_shared_text(report_text, ld), encoding="utf-8")
    print(f"created {output}; duration error={duration_error_s*1000:.3f} ms; source packet/audio hash report={report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
