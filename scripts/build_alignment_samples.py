#!/usr/bin/env python3
"""Build short, report-bound A/V comparison clips without approving measurements.

The clips use the measured source-time pairs from the diagnostic reports. Fade fit B
is mapped back to master time only through a visually approved, hash-bound alignment.
The candidate rate is applied to these disposable previews only; no manifest field or
production audio file is written.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fileutil import sha256_file  # noqa: E402
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402

FPS = 30000.0 / 1001.0
VIDEO_FPS = 12
DEFAULT_DURATION_S = 8.0
MAX_DURATION_S = 20.0


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PipelineError(f"{label} must be a finite number")
    return float(value)


def _read_report(path: Path, episode: str, workflow_version: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"alignment evidence report is missing or unsafe: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read alignment evidence report {path}: {exc}") from exc
    report_episode = report.get("episode")
    if not isinstance(report_episode, str) or report_episode.upper() != episode.upper():
        raise PipelineError(f"report episode does not match {episode}: {path}")
    if str(report.get("workflow_version", "")) != workflow_version:
        raise PipelineError(f"report workflow version is stale: {path}")
    return report


def _reviewed_alignment(ep: Any, alignment: Path) -> bool:
    review = (ep.data.get("video") or {}).get("dvd_alignment_review") or {}
    try:
        marker_path = Path(str(review.get("path") or "")).expanduser()
        if not marker_path.is_absolute():
            if not marker_path.parts or marker_path.parts[0].casefold() != "probe" or ".." in marker_path.parts:
                return False
            marker_path = ep.ld.probe() / Path(*marker_path.parts[1:])
        return (review.get("status") == "APPROVED" and
                marker_path.resolve(strict=True) == alignment.resolve(strict=True) and
                sha256_file(alignment) == review.get("sha256"))
    except (OSError, ValueError):
        return False


def _dvd_to_master_map(alignment: Path) -> tuple[np.ndarray, np.ndarray]:
    try:
        values = np.load(alignment, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot load reviewed DVD/LD alignment: {exc}") from exc
    if values.ndim != 2 or values.shape[1] < 2 or len(values) < 2:
        raise PipelineError("reviewed DVD/LD alignment has an invalid shape")
    master = values[:, 0].astype(np.float64) / FPS
    dvd = (values[:, 0].astype(np.float64) + values[:, 1].astype(np.float64)) / FPS
    order = np.argsort(dvd)
    dvd, master = dvd[order], master[order]
    if np.any(np.diff(dvd) <= 0):
        raise PipelineError("reviewed alignment has duplicate/non-increasing DVD coordinates")
    return dvd, master


def _fit_pair_candidates(report: dict[str, Any], source: str,
                         dvd_map: tuple[np.ndarray, np.ndarray] | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for fit_key, label, domain in (
            ("fit_A_master_vs_wav", "Fade fit A", "master"),
            ("fit_B_dvd_vs_wav", "Fade fit B", "dvd")):
        fit = report.get(fit_key) or {}
        pairs = fit.get("pairs") or []
        slope = fit.get("theil_sen_b")
        if fit.get("error") or not pairs:
            continue
        checked: list[tuple[float, float, float]] = []
        for pair in pairs:
            x = _finite(pair.get("wav_s"), f"{label} WAV time")
            y = _finite(pair.get("y_s"), f"{label} reference time")
            residual = _finite(pair.get("resid_s", 0.0), f"{label} residual")
            if x >= 0 and y >= 0:
                checked.append((x, y, residual))
        if not checked:
            continue
        mapped_rows: list[tuple[float, float, float, float]] = []
        for wav_s, ref_s, residual in checked:
            if domain == "dvd":
                if dvd_map is None or ref_s < dvd_map[0][0] or ref_s > dvd_map[0][-1]:
                    continue
                master_s = float(np.interp(ref_s, dvd_map[0], dvd_map[1]))
            else:
                master_s = ref_s
            mapped_rows.append((wav_s, ref_s, master_s, residual))
        if not mapped_rows:
            continue
        if domain == "dvd":
            pairwise_slopes = [
                (mapped_rows[j][2] - mapped_rows[i][2]) /
                (mapped_rows[j][0] - mapped_rows[i][0])
                for i in range(len(mapped_rows)) for j in range(i + 1, len(mapped_rows))
                if mapped_rows[j][0] != mapped_rows[i][0]
            ]
            if not pairwise_slopes:
                continue
            slope = float(np.median(pairwise_slopes))
            intercept = float(np.median([row[2] - slope * row[0] for row in mapped_rows]))
            mapped_rows = [
                (x, ref, master, master - (intercept + slope * x))
                for x, ref, master, _resid in mapped_rows
            ]
        elif not isinstance(slope, (int, float)) or not math.isfinite(slope) or slope <= 0:
            continue
        # Show a typical pairing and the report's largest-residual pairing; do not
        # invent a residual threshold that the runbook does not specify.
        ordered = sorted(mapped_rows, key=lambda row: row[0])
        chosen = [("midpoint-pair", ordered[len(ordered) // 2]),
                  ("largest-residual-pair", max(mapped_rows, key=lambda row: abs(row[2])))]
        seen: set[tuple[float, float]] = set()
        for suffix, (wav_s, ref_s, master_s, residual) in chosen:
            identity = (wav_s, master_s)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append({
                "id": f"{fit_key.removeprefix('fit_').split('_')[0].lower()}-{suffix}",
                "label": f"{label} — {suffix.replace('-', ' ')}",
                "basis": source,
                "wav_time_s": wav_s,
                "reference_time_s": ref_s if domain == "master" else None,
                "dvd_reference_time_s": ref_s if domain == "dvd" else None,
                "master_time_s": master_s,
                "candidate_slope": float(slope),
                "residual_s": residual,
                "reference_domain": domain,
                "candidate_is_approval": False,
            })
    return candidates


def collect_candidates(reports: list[tuple[Path, dict[str, Any]]],
                       dvd_map: tuple[np.ndarray, np.ndarray] | None,
                       max_samples: int) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: list[dict[str, Any]] = []
    deferred: list[str] = []
    for path, report in reports:
        schema = report.get("schema")
        if schema == "fade-midpoint-fit-v1":
            found = _fit_pair_candidates(report, path.name, dvd_map)
            if ((report.get("fit_B_dvd_vs_wav") or {}).get("pairs") and
                    not any(row["id"].startswith("b-") for row in found) and dvd_map is None):
                deferred.append(f"Fade fit B not previewed: no visually approved alignment map ({path.name})")
            for row in found:
                row["id"] = f"{path.stem}-{row['id']}"
            candidates.extend(found)
        elif schema == "dvd-picture-audio-rate-summary-v2":
            if report.get("candidate_is_approval") is not False:
                deferred.append(f"Skipped summary not explicitly marked candidate-only: {path.name}")
                continue
            windows = report.get("windows") or []
            rate_value = ((report.get("theil_sen") or {}).get("slope_master_s_per_wav_s") or
                          (report.get("ols") or {}).get("slope_master_s_per_wav_s") or 1.0)
            try:
                rate = _finite(rate_value, "reported candidate slope")
            except PipelineError as exc:
                deferred.append(f"Skipped malformed rate candidates in {path.name}: {exc}")
                continue
            threshold = report.get("min_corr_threshold")
            weak = []
            for row in windows:
                try:
                    corr = _finite(row.get("corr"), "window correlation")
                    edge = row.get("search_edge") is True
                    low = isinstance(threshold, (int, float)) and corr < float(threshold)
                    if edge or low:
                        weak.append((edge, corr, row))
                except PipelineError:
                    continue
            weak.sort(key=lambda item: (not item[0], item[1]))
            for index, (edge, corr, row) in enumerate(weak[:max_samples]):
                candidates.append({
                    "id": f"picture-audio-window-{index + 1}",
                    "label": f"Picture/audio window — {'search edge' if edge else 'below screen'}, corr {corr:.3f}",
                    "basis": path.name,
                    "wav_time_s": _finite(row.get("ld_wav_time_s"), "LD WAV time"),
                    "reference_time_s": _finite(row.get("master_equivalent_time_s"), "master-equivalent time"),
                    "master_time_s": _finite(row.get("master_equivalent_time_s"), "master-equivalent time"),
                    "candidate_slope": _finite(rate, "candidate slope"),
                    "correlation": corr,
                    "search_edge": edge,
                    "reference_domain": "master",
                    "candidate_is_approval": False,
                })
        else:
            deferred.append(f"Unsupported preview report schema in {path.name}")
    # Keep a compact set while preserving both fit A/B labels and weak-window flags.
    return candidates[:max_samples], deferred


def probe_media(ffprobe: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,start_time,duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise PipelineError(f"ffprobe failed for {path}: {proc.stderr[-600:]}")
    try:
        data = json.loads(proc.stdout)
        duration = float(data["format"]["duration"])
        streams = data.get("streams") or []
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(f"ffprobe returned invalid media metadata for {path}") from exc
    return {"duration_s": duration, "streams": streams}


def _verify_preview(ffprobe: str, path: Path, expected_duration: float) -> dict[str, Any]:
    info = probe_media(ffprobe, path)
    video = next((stream for stream in info["streams"] if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in info["streams"] if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise PipelineError("preview is missing its video or audio stream")
    video_start, audio_start = float(video.get("start_time") or 0), float(audio.get("start_time") or 0)
    video_duration, audio_duration = float(video.get("duration") or 0), float(audio.get("duration") or 0)
    tolerance = 1.0 / VIDEO_FPS + 0.03
    if min(video_duration, audio_duration) <= 0:
        raise PipelineError("preview has a zero-length stream")
    if abs(video_start) > tolerance or abs(audio_start) > tolerance:
        raise PipelineError("preview A/V streams do not start near timestamp zero")
    if abs(video_duration - audio_duration) > tolerance * 2:
        raise PipelineError("preview video/audio durations disagree")
    if abs(info["duration_s"] - expected_duration) > tolerance * 2:
        raise PipelineError("preview container duration differs from its planned duration")
    return {
        "container_duration_s": info["duration_s"],
        "video_start_s": video_start, "video_duration_s": video_duration,
        "audio_start_s": audio_start, "audio_duration_s": audio_duration,
        "duration_tolerance_s": tolerance * 2,
    }


def build_samples(ep: Any, reports: list[Path], output_dir: Path, duration_s: float,
                  max_samples: int, ld: Ld) -> Path:
    if not math.isfinite(duration_s) or not 2 <= duration_s <= MAX_DURATION_S:
        raise PipelineError(f"sample duration must be between 2 and {MAX_DURATION_S:g} seconds")
    if not 1 <= max_samples <= 12:
        raise PipelineError("maximum sample count must be between 1 and 12")
    master, wav = ep.path("stack_master"), ep.path("ld_native")
    if not master.is_file() or not wav.is_file():
        raise PipelineError("verified stack master and selected native LD WAV are required")
    output_dir = assert_writable_path(output_dir, ld)
    output_dir.mkdir(parents=True, exist_ok=False)
    version_path = Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION"
    workflow_version = version_path.read_text(encoding="ascii").strip()
    loaded = [(path, _read_report(path, ep.code, workflow_version)) for path in reports]
    alignment = ep.path("align_npy") if (ep.data.get("files") or {}).get("align_npy") else None
    dvd_map = None
    alignment_hash = None
    alignment_deferred: list[str] = []
    if alignment and alignment.is_file() and _reviewed_alignment(ep, alignment):
        try:
            dvd_map = _dvd_to_master_map(alignment)
            alignment_hash = sha256_file(alignment)
        except PipelineError as exc:
            alignment_deferred.append(f"DVD-domain previews unavailable: {exc}")
    candidates, deferred = collect_candidates(loaded, dvd_map, max_samples)
    deferred[:0] = alignment_deferred
    master_info = probe_media(ld.ffprobe(), master)
    wav_info = probe_media(ld.ffprobe(), wav)
    master_duration, wav_duration = master_info["duration_s"], wav_info["duration_s"]
    samples: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        try:
            video_center = _finite(row["master_time_s"], "master time")
            audio_center = _finite(row["wav_time_s"], "WAV time")
            slope = _finite(row["candidate_slope"], "candidate rate slope")
            if not 0.5 <= slope <= 2.0:
                raise PipelineError("candidate speed is outside the safe preview range 0.5..2.0")
            video_start = video_center - duration_s / 2
            audio_start = audio_center - duration_s / (2 * slope)
            audio_source_duration = duration_s / slope
            if video_start < 0 or audio_start < 0:
                raise PipelineError("candidate anchor is too near the source start for a centered sample")
            if video_start + duration_s > master_duration + 0.001:
                raise PipelineError("master source ends before the requested preview window")
            if audio_start + audio_source_duration > wav_duration + 0.001:
                raise PipelineError("native WAV ends before the requested preview window")
            video_anchor_output = video_center - video_start
            audio_anchor_output = (audio_center - audio_start) * slope
            if abs(video_anchor_output - audio_anchor_output) > 0.5 / VIDEO_FPS:
                raise PipelineError("preview candidate anchor positions do not align")
            safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in row["id"])
            output = output_dir / f"{safe_id}.mp4"
            partial = output_dir / f"{safe_id}.partial.mp4"
            if output.exists() or partial.exists():
                raise PipelineError("preview output already exists")
            command = [
                ld.ffmpeg(), "-n", "-hide_banner", "-v", "error",
                "-ss", f"{video_start:.9f}", "-i", str(master),
                "-ss", f"{audio_start:.9f}", "-i", str(wav),
                "-filter_complex",
                ("[0:v:0]scale=480:270:force_original_aspect_ratio=decrease,"
                 "pad=480:270:(ow-iw)/2:(oh-ih)/2:black,fps=12,setpts=PTS-STARTPTS[v];"
                 f"[1:a:0]atempo={slope:.12g},aresample=48000,apad,"
                 f"atrim=duration={duration_s:.9f},asetpts=PTS-STARTPTS[a]"),
                "-map", "[v]", "-map", "[a]", "-t", f"{duration_s:.9f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", str(partial),
            ]
            proc = subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode:
                raise PipelineError(f"FFmpeg failed: {proc.stderr[-800:]}")
            if not partial.is_file() or partial.stat().st_size <= 0:
                raise PipelineError("FFmpeg returned success but did not create a preview")
            verified = _verify_preview(ld.ffprobe(), partial, duration_s)
            partial.rename(output)
            row.update({
                "status": "READY", "path": str(output),
                "video_source": str(master), "audio_source": str(wav),
                "video_start_s": video_start, "audio_start_s": audio_start,
                "video_anchor_output_s": video_anchor_output,
                "audio_anchor_output_s": audio_anchor_output,
                "duration_s": duration_s, "applied_preview_slope": slope,
                "source_hashes": {"video": sha256_file(master), "audio": sha256_file(wav)},
                "output_sha256": sha256_file(output), "verification": verified,
                "alignment_sha256": alignment_hash,
                "ffmpeg_args": command,
            })
        except (OSError, PipelineError, ValueError) as exc:
            row.update({"status": "DEFERRED", "reason": str(exc)})
            deferred.append(f"{row.get('label', row['id'])}: {exc}")
        samples.append(row)
    index = {
        "schema": "alignment-samples-v1",
        "workflow_version": workflow_version,
        "episode": ep.code,
        "candidate_is_approval": False,
        "manifest_path": str(ep.manifest),
        "manifest_sha256": sha256_file(ep.manifest),
        "video_source": str(master), "video_sha256": sha256_file(master),
        "audio_source": str(wav), "audio_sha256": sha256_file(wav),
        "alignment_path": str(alignment) if alignment_hash else None,
        "alignment_sha256": alignment_hash,
        "reports": [{"path": str(path), "sha256": sha256_file(path),
                     "schema": report.get("schema")} for path, report in loaded],
        "samples": samples,
        "deferred": deferred,
        "warning": ("Decision aids only. Preview rate and alignment candidates are not approved "
                    "measurements and do not modify the manifest."),
    }
    index_path = output_dir / "index.json"
    with index_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(index, stream, indent=2)
        stream.write("\n")
    return index_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--report", action="append", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--max-samples", type=int, default=8)
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    index = build_samples(ep, [Path(value) for value in args.report],
                          Path(args.out_dir), args.duration_s, args.max_samples, ld)
    print(f"alignment samples: {index}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
