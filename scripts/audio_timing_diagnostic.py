#!/usr/bin/env python3
"""DVD-free, read-only timing estimate from the native EFM WAV and stack timeline.

This is an experimental candidate generator, not an automatic rate approval. It
compares exact PCM duration with the measured full stack timeline and reports the
frame-map PAD counts separately (including those trimmed from the final output).
It never opens the DVD, alignment path, or manifest for writing.

  python scripts/audio_timing_diagnostic.py --episode S01E07
   python scripts/audio_timing_diagnostic.py --episode S01E07 --candidate-out <new.flac>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402
from pad_audit import reference_map_ranges, source_id_for_capture  # noqa: E402

FPS = 30000.0 / 1001.0
BASE_RATE = 44100
INTERMEDIATE_FACTOR = 2


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wav_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as w:
            if w.getcomptype() != "NONE":
                raise PipelineError(f"native EFM input must be uncompressed PCM WAV: {path}")
            return {
                "frames": w.getnframes(),
                "sample_rate": w.getframerate(),
                "channels": w.getnchannels(),
                "sample_width_bytes": w.getsampwidth(),
                "duration_s": w.getnframes() / w.getframerate(),
            }
    except (wave.Error, OSError) as e:
        raise PipelineError(f"cannot read exact PCM WAV sample count from {path}: {e}") from e


def summarize_timing(sample_rate: int, sample_count: int, stack_frames: int,
                     map_pad_count: int, projected_output_pad_count: int | None = None) -> dict:
    if sample_rate <= 0 or sample_count <= 0 or stack_frames <= 0:
        raise PipelineError("sample rate, sample count, and stack frame count must be positive")
    raw_duration = sample_count / sample_rate
    stack_duration = stack_frames / FPS
    raw_equiv_frames = raw_duration * FPS
    rate = sample_rate * raw_duration / stack_duration
    intermediate_rate = round(INTERMEDIATE_FACTOR * rate)
    effective_rate = intermediate_rate / INTERMEDIATE_FACTOR
    model_content_frames = stack_frames - map_pad_count
    result = {
        "native_audio_duration_s": raw_duration,
        "native_audio_equiv_video_frames": raw_equiv_frames,
        "stack_frames": stack_frames,
        "stack_duration_s": stack_duration,
        "duration_delta_frames_stack_minus_audio": stack_frames - raw_equiv_frames,
        "map_pad_count_full": map_pad_count,
        "map_content_frames_if_all_map_pads_inserted": model_content_frames,
        "audio_minus_map_content_frames": raw_equiv_frames - model_content_frames,
        "duration_candidate_effective_rate_hz": rate,
        "duration_candidate_two_x_asetrate_hz": intermediate_rate,
        "duration_candidate_effective_rate_rounded_hz": effective_rate,
        "duration_candidate_quantization_error_hz": effective_rate - rate,
        "dvd_opened": False,
        "is_approval": False,
        "caveat": "Duration/PAD estimate only. Verify matching endpoints, EFM structural trim, map counts, and origin; do not auto-commit.",
    }
    if projected_output_pad_count is not None:
        result["map_pad_count_projected_into_output"] = projected_output_pad_count
        result["audio_minus_projected_content_frames"] = raw_equiv_frames - (stack_frames - projected_output_pad_count)
    return result


def probe_candidate(ld: Ld, path: Path) -> dict:
    p = subprocess.run(
        [ld.ffprobe(), "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,channels,bits_per_sample,duration,duration_ts,time_base",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise PipelineError(f"ffprobe failed on candidate {path}: {p.stderr[-800:]}")
    try:
        streams = json.loads(p.stdout).get("streams", [])
    except json.JSONDecodeError as e:
        raise PipelineError(f"invalid ffprobe JSON for {path}: {e}") from e
    if len(streams) != 1:
        raise PipelineError(f"expected one candidate audio stream in {path}; found {len(streams)}")
    return streams[0]


def make_candidate(ld: Ld, source: Path, destination: Path, rate_2x: int) -> dict:
    ext = destination.suffix.lower()
    if ext not in (".wav", ".flac"):
        raise PipelineError(f"candidate output must end in .wav or .flac: {destination}")
    if destination.exists():
        raise PipelineError(f"refusing to overwrite candidate output: {destination}")
    temp = destination.with_name(destination.stem + (".tmp.wav" if ext == ".wav" else ".tmp.flac"))
    if temp.exists():
        raise PipelineError(f"temporary candidate exists; inspect it before retrying: {temp}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if rate_2x == BASE_RATE * INTERMEDIATE_FACTOR and ext == ".wav":
        shutil.copy2(source, temp)
        temp.rename(destination)
        return {"path": str(destination), "filter": "bit-copy (no rate correction)",
                "ffmpeg_args": [], "output_wav": wav_info(destination),
                "output_sha256": sha256_file(destination)}
    if rate_2x == BASE_RATE * INTERMEDIATE_FACTOR:
        filt = "anull"
    else:
        filt = (f"aresample={BASE_RATE * INTERMEDIATE_FACTOR}:resampler=soxr:precision=28,"
                f"asetrate={rate_2x},aresample={BASE_RATE}:resampler=soxr:precision=28")
    codec_args = ["-c:a", "pcm_s16le", "-f", "wav"] if ext == ".wav" else ["-c:a", "flac", "-sample_fmt", "s16", "-f", "flac"]
    cmd = [ld.ffmpeg(), "-n", "-hide_banner", "-v", "error", "-i", str(source),
           "-map", "0:a:0", "-af", filt, "-ar", str(BASE_RATE)] + codec_args + [str(temp)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        try:
            temp.unlink(missing_ok=True)
        finally:
            raise PipelineError(f"DVD-free candidate audio generation failed ({p.returncode}): {p.stderr[-1200:]}")
    if not temp.exists():
        raise PipelineError("FFmpeg reported success but candidate WAV is missing")
    try:
        meta = wav_info(temp) if ext == ".wav" else probe_candidate(ld, temp)
        if ext == ".flac" and (meta.get("sample_rate") != str(BASE_RATE) or int(meta.get("channels") or 0) != 2):
            raise PipelineError(f"experimental FLAC has unexpected stream format: {meta}")
        temp.rename(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {"path": str(destination), "filter": filt, "ffmpeg_args": cmd,
            "output_media": meta, "output_sha256": sha256_file(destination)}


def make_synced_candidate(ld: Ld, ep, source: Path, destination: Path,
                          rate_2x: int, wav0_master_frame: float | None = None) -> dict:
    """Build a disposable DVD-free-rate + measured/geometric-skip FLAC."""
    if destination.suffix.lower() != ".flac":
        raise PipelineError(f"synced candidate output must end in .flac: {destination}")
    if destination.exists():
        raise PipelineError(f"refusing to overwrite synced candidate: {destination}")
    temp = destination.with_name(destination.stem + ".tmp.flac")
    if temp.exists():
        raise PipelineError(f"temporary synced candidate exists; inspect it: {temp}")
    video = ep.data.get("video") or {}
    if video.get("frames") is None or video.get("delta_s") is None:
        raise PipelineError("DVD-free synced candidate needs measured video.frames and video.delta_s")
    pad_frames = ep.data.get("audio", {}).get("pad_frames")
    if pad_frames is None:
        raise PipelineError("DVD-free synced candidate needs the measured selected-capture audio.pad_frames")
    if wav0_master_frame is not None:
        skip_s = float(video["delta_s"]) - float(wav0_master_frame) / FPS
        skip_method = "shared LD frame/timecode WAV0"
    else:
        skip_s = float(video["delta_s"]) - int(pad_frames) / FPS
        skip_method = "Orc alignment-map geometric fallback (not absolute-sync proof)"
    if skip_s < 0:
        raise PipelineError(f"DVD-free AudioSkip is negative ({skip_s:.6f}s); inspect the origin convention")
    start_sample = int(skip_s * BASE_RATE)
    target_samples = round(int(video["frames"]) * 1001 * BASE_RATE / 24000)
    if rate_2x == BASE_RATE * INTERMEDIATE_FACTOR:
        rate_filter = "anull"
    else:
        rate_filter = (f"aresample={BASE_RATE * INTERMEDIATE_FACTOR}:resampler=soxr:precision=28,"
                       f"asetrate={rate_2x},aresample={BASE_RATE}:resampler=soxr:precision=28")
    filt = (f"{rate_filter},atrim=start_sample={start_sample},asetpts=PTS-STARTPTS,"
            f"apad=whole_len={target_samples},atrim=end_sample={target_samples}")
    cmd = [ld.ffmpeg(), "-n", "-hide_banner", "-v", "error", "-i", str(source),
           "-map", "0:a:0", "-af", filt, "-c:a", "flac", "-compression_level", "8",
           "-sample_fmt", "s16", "-ar", str(BASE_RATE), "-f", "flac", str(temp)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode or not temp.is_file():
        temp.unlink(missing_ok=True)
        raise PipelineError(f"DVD-free synced-candidate build failed ({p.returncode}): {p.stderr[-1200:]}")
    meta = probe_candidate(ld, temp)
    if meta.get("sample_rate") != str(BASE_RATE) or int(meta.get("channels") or 0) != 2:
        temp.unlink(missing_ok=True)
        raise PipelineError(f"synced candidate has unexpected stream format: {meta}")
    duration = float(meta.get("duration") or 0)
    target_duration = target_samples / BASE_RATE
    if abs(duration - target_duration) > 1.5 / BASE_RATE:
        temp.unlink(missing_ok=True)
        raise PipelineError(f"synced candidate duration differs from target by {(duration-target_duration)*1000:.3f} ms")
    temp.rename(destination)
    return {"path": str(destination), "filter": filt, "ffmpeg_args": cmd,
            "source_rate_intermediate_hz": rate_2x, "effective_rate_hz": rate_2x/2,
            "skip_s": skip_s, "skip_method": skip_method,
            "wav0_master_frame": wav0_master_frame, "start_sample": start_sample,
            "target_samples": target_samples, "target_duration_s": target_duration,
            "output_stream": meta, "duration_error_ms": (duration-target_duration)*1000,
            "output_sha256": sha256_file(destination)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--audio", help="override native PCM EFM WAV (defaults to files.ld_native)")
    ap.add_argument("--rate-hz", type=float,
                    help="optional independently selected effective asetrate; default is the duration candidate")
    ap.add_argument("--wav0-master-frame", type=float,
                    help="measured DVD-free WAV0 on the master frame/timebase; otherwise use only the geometric pad offset")
    ap.add_argument("--candidate-out", help="optionally write an experimental duration-rate WAV/FLAC; never overwrites")
    ap.add_argument("--synced-candidate-out", help="optionally write an experimental DVD-free rate+geometric-skip synced FLAC; never overwrites")
    ap.add_argument("--report-out", help="override report path (default: roots.probe/<episode>-dvd-free-audio-timing.json)")
    args = ap.parse_args()
    if args.candidate_out and args.synced_candidate_out:
        raise PipelineError("choose either --candidate-out or --synced-candidate-out, not both")
    if args.wav0_master_frame is not None and not args.synced_candidate_out:
        raise PipelineError("--wav0-master-frame is only valid with --synced-candidate-out")

    ld = Ld()
    ep = load_episode(args.episode, ld)
    source = Path(args.audio) if args.audio else ep.path("ld_native")
    project = ep.path("stack_project")
    for path in (source, project):
        if not path.is_file():
            raise PipelineError(f"required DVD-free input missing: {path}")
    audio = wav_info(source)
    if audio["sample_rate"] != BASE_RATE or audio["channels"] != 2 or audio["sample_width_bytes"] != 2:
        raise PipelineError(f"expected native 44.1 kHz/16-bit stereo EFM WAV, got {audio['sample_rate']} Hz, "
                            f"{audio['sample_width_bytes'] * 8}-bit, {audio['channels']}ch")
    source_id = source_id_for_capture(project, ep.reference)
    pads = reference_map_ranges(project, source_id=source_id)
    if not pads:
        raise PipelineError("frame map contains no PAD_1 positions; this tool is not a generic audio clock estimator")

    projected_pads = None
    pads_trimmed_before_head = None
    pads_after_output_tail = None
    video = ep.data.get("video") or {}
    if video.get("frames") and video.get("head_trim") is not None:
        source_pad = ep.data.get("audio", {}).get("pad_frames")
        if source_pad is not None:
            head_trim = int(video["head_trim"])
            out_frames = int(video["frames"])
            mapped = [((int(m) - int(source_pad)) * 4 // 5 - head_trim) for m in pads]
            pads_trimmed_before_head = sum(i < 0 for i in mapped)
            pads_after_output_tail = sum(i >= out_frames for i in mapped)
            projected_pads = sum(0 <= i < out_frames for i in mapped)

    report = summarize_timing(BASE_RATE, audio["frames"], ep.stack_frames,
                              len(pads), projected_pads)
    chosen_rate = args.rate_hz if args.rate_hz is not None else report["duration_candidate_effective_rate_hz"]
    if not BASE_RATE * 0.98 <= chosen_rate <= BASE_RATE * 1.02:
        raise PipelineError(f"selected rate {chosen_rate} Hz is outside the broad 2% sanity range")
    chosen_rate_2x = round(INTERMEDIATE_FACTOR * chosen_rate)
    report.update({
        "schema": "dvd-free-audio-timing-v1",
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": ep.code,
        "reference_capture": ep.reference,
        "selected_audio_capture": ep.data.get("audio", {}).get("capture"),
        "selected_capture_alignment_pad_frames": ep.data.get("audio", {}).get("pad_frames"),
        "experimental_candidate_rate_hz": chosen_rate,
        "experimental_candidate_rate_source": "user-supplied" if args.rate_hz is not None else "duration-only",
        "experimental_candidate_intermediate_asetrate_hz": chosen_rate_2x,
        "source_audio": str(source),
        "source_audio_sha256": sha256_file(source),
        "source_audio_pcm": audio,
        "stack_project": str(project),
        "reference_frame_map_source_id": source_id,
        "full_map_pad_positions_master_frames": pads,
        "map_pads_trimmed_before_output_head": pads_trimmed_before_head,
        "map_pads_after_output_tail": pads_after_output_tail,
        "measured_head_trim_output_frames": video.get("head_trim"),
        "measured_output_frames": video.get("frames"),
        "measured_delta_s": video.get("delta_s"),
        "dvd_opened": False,
    })
    default_report = ep.optional_path("audio_timing_report") or (ld.probe() / f"{ep.code}-dvd-free-audio-timing.json")
    out = Path(args.report_out) if args.report_out else default_report
    if out.exists():
        raise PipelineError(f"refusing to overwrite report: {out}")
    if args.candidate_out:
        report["experimental_candidate"] = make_candidate(ld, source, Path(args.candidate_out), chosen_rate_2x)
    if args.synced_candidate_out:
        report["experimental_synced_candidate"] = make_synced_candidate(
            ld, ep, source, Path(args.synced_candidate_out), chosen_rate_2x,
            args.wav0_master_frame)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"DVD-free timing diagnostic for {ep.code} (no DVD/alignment read)")
    print(f"  native WAV: {audio['frames']} samples/frame-count units at {audio['sample_rate']} Hz; {audio['duration_s']:.9f}s")
    print(f"  stack: {ep.stack_frames} frames / {report['stack_duration_s']:.9f}s")
    print(f"  frame-map PAD_1: {len(pads)} total; projected in output: {projected_pads}; "
          f"trimmed before head: {pads_trimmed_before_head}; beyond tail: {pads_after_output_tail}")
    print(f"  stack-minus-audio duration: {report['duration_delta_frames_stack_minus_audio']:.3f} frames")
    print(f"  effective duration candidate: {report['duration_candidate_effective_rate_hz']:.4f} Hz; "
          f"2x integer asetrate={report['duration_candidate_two_x_asetrate_hz']} "
          f"(effective {report['duration_candidate_effective_rate_rounded_hz']:.2f} Hz)")
    print(f"  experimental candidate selection: {chosen_rate:.4f} Hz from {report['experimental_candidate_rate_source']} "
          f"(2x asetrate {chosen_rate_2x}; manifest unchanged)")
    print("  NOT APPROVED: reconcile PAD counts/endpoints and compare with the preserved DVD-based estimate before use.")
    if args.candidate_out:
        print(f"  experimental candidate audio: {args.candidate_out}")
    if args.synced_candidate_out:
        print(f"  experimental synced candidate: {args.synced_candidate_out} "
              f"(skip method={report['experimental_synced_candidate']['skip_method']}; exact-frame duration; approval still required)")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
