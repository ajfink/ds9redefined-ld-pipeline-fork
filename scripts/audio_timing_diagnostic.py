#!/usr/bin/env python3
"""DVD-free, read-only timing estimate from the native EFM WAV and stack timeline.

By default this is a read-only candidate report. It compares exact PCM duration
with the measured full stack timeline and reports frame-map PAD counts separately.
`--promote-dvd-free` is the explicit V6 production path: it is permitted only for
an episode initialized after the user chose no-DVD mode, only after the endpoint
and PAD checks pass, and it updates the manifest atomically with a preserved backup.
The analysis itself never opens DVD media or a DVD picture-alignment path.

  python scripts/audio_timing_diagnostic.py --episode S01E07
   python scripts/audio_timing_diagnostic.py --episode S01E07 --candidate-out <new.flac>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ldcommon import (BUNDLE, Ld, PipelineError, assert_writable_path,
                     episode_manifest_path, load_episode,
                     redact_shared_text)  # noqa: E402
from pad_audit import reference_map_ranges, source_id_for_capture  # noqa: E402

FPS = 30000.0 / 1001.0
BASE_RATE = 44100
INTERMEDIATE_FACTOR = 2
NO_DVD_PAD_TOLERANCE_FRAMES = 1.0
GIB = 1024 ** 3


def no_dvd_audio_plan_gib(source_bytes: int) -> float:
    """Conservative peak estimate for corrected + synced FLACs and one temp output."""
    if source_bytes <= 0:
        raise PipelineError("native EFM WAV size must be positive for the no-DVD audio space estimate")
    return max(0.5, 3.0 * source_bytes / GIB)


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


def supports_soxr(ld: Ld) -> bool:
    """Return whether this FFmpeg build advertises libsoxr in its configure flags."""
    p = subprocess.run([ld.ffmpeg(), "-hide_banner", "-buildconf"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode == 0 and "--enable-libsoxr" in (p.stdout + p.stderr)


def rate_filter(rate_2x: int, use_soxr: bool) -> tuple[str, str]:
    if rate_2x == BASE_RATE * INTERMEDIATE_FACTOR:
        return "anull", "none (rate unchanged)"
    if use_soxr:
        return (f"aresample={BASE_RATE * INTERMEDIATE_FACTOR}:resampler=soxr:precision=28,"
                f"asetrate={rate_2x},aresample={BASE_RATE}:resampler=soxr:precision=28"), "soxr precision=28"
    # macOS's tested static FFmpeg 9.0.2 build has no libsoxr. FFmpeg's default
    # swr path is the established fallback; record it rather than failing late.
    return (f"aresample={BASE_RATE * INTERMEDIATE_FACTOR},asetrate={rate_2x},"
            f"aresample={BASE_RATE}"), "FFmpeg default resampler (swr)"


def make_candidate(ld: Ld, source: Path, destination: Path, rate_2x: int,
                   use_soxr: bool) -> dict:
    assert_writable_path(destination, ld)
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
    filt, resampler = rate_filter(rate_2x, use_soxr)
    if filt == "anull":
        filt = "anull"
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
    return {"path": str(destination), "filter": filt, "resampler": resampler,
            "ffmpeg_args": cmd,
            "output_media": meta, "output_sha256": sha256_file(destination)}


def make_synced_candidate(ld: Ld, ep, source: Path, destination: Path,
                          rate_2x: int, use_soxr: bool,
                          wav0_master_frame: float | None = None) -> dict:
    """Build a disposable DVD-free-rate + measured/geometric-skip FLAC."""
    assert_writable_path(destination, ep.ld)
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
    rate_filt, resampler = rate_filter(rate_2x, use_soxr)
    filt = (f"{rate_filt},atrim=start_sample={start_sample},asetpts=PTS-STARTPTS,"
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
            "resampler": resampler,
            "source_rate_intermediate_hz": rate_2x, "effective_rate_hz": rate_2x/2,
            "skip_s": skip_s, "skip_method": skip_method,
            "wav0_master_frame": wav0_master_frame, "start_sample": start_sample,
            "target_samples": target_samples, "target_duration_s": target_duration,
            "output_stream": meta, "duration_error_ms": (duration-target_duration)*1000,
            "output_sha256": sha256_file(destination)}


def promote_no_dvd_manifest(ep, report: dict, corrected: Path, synced: Path,
                            report_path: Path, endpoint_residual: float,
                            expected_manifest_sha256: str) -> Path:
    """Atomically record the explicitly selected, no-DVD duration/PAD production route."""
    if ep.data.get("dvd_reference_available") is not False:
        raise PipelineError("DVD-free promotion requires dvd_reference_available=false in the manifest")
    if ep.data.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("DVD-free promotion requires the user's recorded --no-dvd choice")
    if set(ep.data.get("deliverable_tracks") or ["ld"]) != {"ld"}:
        raise PipelineError("DVD-free audio mode can deliver LD stereo only; DVD 5.1/SRT tracks are unavailable")
    manifest_path = episode_manifest_path(ep.code, ep.ld, series=getattr(ep, "series", "DS9"))
    assert_writable_path(manifest_path, ep.ld)
    original = manifest_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_manifest_sha256:
        raise PipelineError("episode manifest changed during the DVD-free build; preserve outputs and reconcile before retrying")
    current = json.loads(original.decode("utf-8"))
    if current.get("dvd_reference_available") is not False or current.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("episode manifest changed during the DVD-free build; preserve outputs and reconcile before retrying")
    if not corrected.is_file() or not synced.is_file():
        raise PipelineError("DVD-free promotion requires both corrected and sample-synced audio outputs")
    if endpoint_residual > NO_DVD_PAD_TOLERANCE_FRAMES or endpoint_residual < -NO_DVD_PAD_TOLERANCE_FRAMES:
        raise PipelineError(f"duration/PAD endpoint residual {endpoint_residual:+.3f} frames exceeds the "
                            f"±{NO_DVD_PAD_TOLERANCE_FRAMES:.1f}-frame no-DVD limit")

    sync = report.get("experimental_synced_candidate") or {}
    full = report.get("experimental_candidate") or {}
    audio = current.setdefault("audio", {})
    files = current.setdefault("files", {})
    audio.update({
        "rate_method": "dvd-free-duration-pad",
        "sync_method": "duration/PAD rate plus measured capture-pad geometry",
        "duration_candidate_rate_hz": report["duration_candidate_effective_rate_hz"],
        "intermediate_asetrate_hz": sync.get("source_rate_intermediate_hz"),
        "resampler": sync.get("resampler"),
        "skip_s": sync.get("skip_s"),
        "skip_method": sync.get("skip_method"),
        "wav0_master_frame": audio.get("pad_frames"),
        "endpoint_residual_frames": endpoint_residual,
        "endpoint_tolerance_frames": NO_DVD_PAD_TOLERANCE_FRAMES,
        "origin_verified_by_duration": True,
        "absolute_av_reference_available": False,
        "dvd_free_approved": True,
        "corrected_duration_s": float((full.get("output_media") or {}).get("duration") or 0.0),
        "asetrate": (sync.get("source_rate_intermediate_hz") or 0) / INTERMEDIATE_FACTOR,
        "rate_fit_ppm": None,
        "rate_fit_stderr_ppm": None,
    })
    files["ld_corrected"] = {"root": "audio", "name": corrected.name}
    files["ld_synced"] = {"root": "audio", "name": synced.name}
    shared = current.get("shared") or {}
    instance_value = os.environ.get("LDP_INSTANCE_ROOT")
    if shared.get("enabled") is True and instance_value:
        report_resolved = report_path.resolve()
        instance_root = Path(instance_value).resolve()
        try:
            report_resolved.relative_to(instance_root)
            files["audio_timing_report"] = {"path": str(report_resolved)}
        except ValueError:
            files["audio_timing_report"] = {"root": "probe", "name": report_path.name}
    else:
        files["audio_timing_report"] = {"root": "probe", "name": report_path.name}
    current["ld_audio_source"] = "ld_synced"
    current["deliverable_tracks"] = ["ld"]
    current["mux_tracks"] = ["ld"]

    from datetime import datetime
    backup = manifest_path.with_name(
        f"{manifest_path.stem}.backup-v6-no-dvd-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}{manifest_path.suffix}")
    if backup.exists():
        raise PipelineError(f"manifest backup already exists: {backup}")
    if hashlib.sha256(manifest_path.read_bytes()).digest() != hashlib.sha256(original).digest():
        raise PipelineError("episode manifest changed while preparing DVD-free promotion")
    backup.write_bytes(original)
    temp = manifest_path.with_name(manifest_path.name + ".tmp")
    if temp.exists():
        raise PipelineError(f"manifest temp file exists; inspect before retrying: {temp}")
    try:
        temp.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if hashlib.sha256(manifest_path.read_bytes()).digest() != hashlib.sha256(original).digest():
            raise PipelineError("episode manifest changed before commit; staged temp preserved")
        temp.replace(manifest_path)
    except Exception:
        raise
    return backup


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
    ap.add_argument("--promote-dvd-free", action="store_true",
                    help="build corrected+synced audio and record the approved no-DVD method in a manifest initialized with --no-dvd")
    ap.add_argument("--report-out", help="override report path (default: roots.probe/<episode>-dvd-free-audio-timing.json)")
    args = ap.parse_args()
    if args.candidate_out and args.synced_candidate_out:
        raise PipelineError("choose either --candidate-out or --synced-candidate-out, not both")
    if args.wav0_master_frame is not None and not args.synced_candidate_out:
        raise PipelineError("--wav0-master-frame is only valid with --synced-candidate-out")
    if args.promote_dvd_free and (args.candidate_out or args.synced_candidate_out or
                                  args.rate_hz is not None or args.audio or
                                  args.wav0_master_frame is not None):
        raise PipelineError("--promote-dvd-free selects the measured duration/PAD path and its manifest-derived inputs/outputs; "
                            "do not combine it with experimental overrides")

    ld = Ld()
    ep = load_episode(args.episode, ld)
    manifest_path = episode_manifest_path(ep.code, ld, series=getattr(ep, "series", "DS9"))
    manifest_sha256 = sha256_file(manifest_path) if args.promote_dvd_free else ""
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
    use_soxr = supports_soxr(ld)
    report["resampler_backend"] = "soxr precision=28" if use_soxr else "FFmpeg default resampler (swr)"
    default_report = ep.optional_path("audio_timing_report") or (ld.probe() / f"{ep.code}-dvd-free-audio-timing.json")
    out = Path(args.report_out) if args.report_out else default_report
    assert_writable_path(out, ld)
    if out.exists():
        raise PipelineError(f"refusing to overwrite report: {out}")
    report_tmp = out.with_name(out.name + ".tmp")
    if args.promote_dvd_free and report_tmp.exists():
        raise PipelineError(f"timing report temp exists; inspect before retrying: {report_tmp}")
    corrected_out = None
    synced_out = None
    if args.promote_dvd_free:
        if ep.data.get("dvd_reference_available") is not False or ep.data.get("no_dvd_proceed_approved") is not True:
            raise PipelineError("--promote-dvd-free requires an episode initialized with --no-dvd after the user's choice")
        if set(ep.data.get("deliverable_tracks") or ["ld"]) != {"ld"}:
            raise PipelineError("DVD-free audio promotion only supports LD stereo; DVD 5.1/SRT tracks are unavailable")
        residual = report.get("audio_minus_projected_content_frames")
        if residual is None:
            raise PipelineError("DVD-free promotion requires PAD positions projected into the measured output timeline")
        if abs(float(residual)) > NO_DVD_PAD_TOLERANCE_FRAMES:
            raise PipelineError(f"duration/PAD endpoint residual {float(residual):+.3f} frames exceeds the "
                                f"±{NO_DVD_PAD_TOLERANCE_FRAMES:.1f}-frame no-DVD limit")
        planned_gib = no_dvd_audio_plan_gib(source.stat().st_size)
        gate = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("check_space_budget.py")),
             "--stage", "dvd-free-audio", "--planned-gib", f"{planned_gib:.6f}",
             "--root", "audio"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print((gate.stdout or "").strip())
        if gate.returncode != 0:
            raise PipelineError("DVD-free audio space gate did not pass; no audio outputs were started")
        report["dvd_free_audio_space_plan_gib"] = planned_gib
        report["dvd_free_audio_space_gate"] = (gate.stdout or "").strip()
        corrected_out = ep.optional_path("ld_corrected") or (
            ld.root("audio") / f"{ep.code}-LD20-dvd-free-{chosen_rate_2x}-corrected.flac")
        synced_out = ep.optional_path("ld_synced") or (
            ld.root("audio") / f"{ep.code}-LD20-synced.flac")
        report["experimental_candidate"] = make_candidate(
            ld, source, corrected_out, chosen_rate_2x, use_soxr)
        report["experimental_synced_candidate"] = make_synced_candidate(
            ld, ep, source, synced_out, chosen_rate_2x, use_soxr)
        report["experimental_synced_candidate"]["skip_method"] = (
            "measured LD output-frame delta minus selected-capture Orc pad_frames/FPS; "
            "no DVD reference")
        report["experimental_synced_candidate"]["dvd_free_sync_caveat"] = (
            "The duration/PAD and capture-origin model sets the audio timeline and exact endpoint; "
            "an external absolute A/V cross-correlation is unavailable without a DVD/reference.")
        report["is_approval"] = True
        report["approval_method"] = "user-selected no-DVD production route; duration/PAD rate and capture-pad geometry"
        report["external_absolute_av_reference_available"] = False
        report["promotion_requested"] = True
        report["promoted_to_manifest"] = False
        report["caveat"] = ("User selected the no-DVD V6 route; duration/PAD endpoints and capture-pad geometry "
                            "were within the 1-frame bound. No external absolute A/V cross-correlation was possible.")
    elif args.candidate_out:
        report["experimental_candidate"] = make_candidate(
            ld, source, Path(args.candidate_out), chosen_rate_2x, use_soxr)
    if args.synced_candidate_out:
        report["experimental_synced_candidate"] = make_synced_candidate(
            ld, ep, source, Path(args.synced_candidate_out), chosen_rate_2x,
            use_soxr, args.wav0_master_frame)
    out.parent.mkdir(parents=True, exist_ok=True)
    report_text = json.dumps(report, indent=2) + "\n"
    instance_root = os.environ.get("LDP_INSTANCE_ROOT")
    if instance_root and (ep.data.get("shared") or ld.profile.get("shared") or {}).get("enabled") is True:
        try:
            Path(out).resolve().relative_to(Path(instance_root).resolve())
            report_text = redact_shared_text(report_text, ld)
        except ValueError:
            pass
    out.write_text(report_text, encoding="utf-8")

    backup = None
    if args.promote_dvd_free:
        backup = promote_no_dvd_manifest(ep, report, corrected_out, synced_out,
                                         out, float(report["audio_minus_projected_content_frames"]),
                                         manifest_sha256)
        report["promoted_to_manifest"] = True
        report_text = json.dumps(report, indent=2) + "\n"
        if instance_root and (ld.profile.get("shared") or {}).get("enabled") is True:
            try:
                out.resolve().relative_to(Path(instance_root).resolve())
                report_text = redact_shared_text(report_text, ld)
            except ValueError:
                pass
        report_tmp.write_text(report_text, encoding="utf-8")
        report_tmp.replace(out)

    print(f"DVD-free timing diagnostic for {ep.code} (no DVD/alignment read)")
    print(f"  native WAV: {audio['frames']} samples/frame-count units at {audio['sample_rate']} Hz; {audio['duration_s']:.9f}s")
    print(f"  stack: {ep.stack_frames} frames / {report['stack_duration_s']:.9f}s")
    print(f"  frame-map PAD_1: {len(pads)} total; projected in output: {projected_pads}; "
          f"trimmed before head: {pads_trimmed_before_head}; beyond tail: {pads_after_output_tail}")
    print(f"  stack-minus-audio duration: {report['duration_delta_frames_stack_minus_audio']:.3f} frames")
    print(f"  effective duration candidate: {report['duration_candidate_effective_rate_hz']:.4f} Hz; "
          f"2x integer asetrate={report['duration_candidate_two_x_asetrate_hz']} "
          f"(effective {report['duration_candidate_effective_rate_rounded_hz']:.2f} Hz)")
    print(f"  selected effective rate: {chosen_rate:.4f} Hz from {report['experimental_candidate_rate_source']} "
          f"(2x asetrate {chosen_rate_2x})")
    if args.promote_dvd_free:
        print("  PROMOTED: user-selected no-DVD route; audio is aligned by duration/PAD and capture-pad geometry.")
        print("  LIMIT: no external DVD cross-correlation was possible; absolute lip-sync offset is not independently scored.")
        print(f"  corrected audio: {corrected_out}; synced audio: {synced_out}; manifest backup: {backup}")
    else:
        print("  CANDIDATE ONLY: review PAD/endpoints and A/V evidence before promotion.")
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
