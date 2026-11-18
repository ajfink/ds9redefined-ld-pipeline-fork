#!/usr/bin/env python3
"""Create short, sequentially decoded DVD audio audition clips for retime candidates."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fileutil import sha256_file  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from prepare_retime_breaks import resolve_dvd_audio  # noqa: E402
from retime_break_candidates import CANDIDATE_SCHEMA  # noqa: E402


def _load_candidates(path: Path, identity: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PipelineError("retime candidate report is missing or unsafe")
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot load retime candidate report: {exc}") from exc
    if (report.get("schema") != CANDIDATE_SCHEMA or report.get("status") != "REPORT_ONLY" or
            report.get("identity") != identity or report.get("manifest_updated") is not False):
        raise PipelineError("retime candidate report is not report-only evidence for this episode")
    return report


def _audio_intervals(report: dict[str, Any], max_clips: int) -> list[dict[str, Any]]:
    intervals = {str(row.get("id")): row for row in report.get("dvd_silence_intervals", [])
                 if isinstance(row, dict) and row.get("id")}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in report.get("candidates", []):
        projected = candidate.get("projected_dvd_interval_s")
        label = str(candidate.get("label") or "")
        if (label and isinstance(projected, (list, tuple)) and len(projected) == 2):
            sample_id = f"LD-{label}"
            selected.append({
                "id": sample_id, "start_s": projected[0], "end_s": projected[1],
                "sample_kind": "projected DVD audio context", "candidate_label": label,
            })
            seen.add(sample_id)
        for pair in candidate.get("pair_candidates") or []:
            if pair.get("id"):
                sample_id = str(pair["id"])
                if sample_id not in seen and sample_id in intervals:
                    selected.append({**intervals[sample_id], "sample_kind": "DVD audio-silence interval"})
                    seen.add(sample_id)
    rows = selected
    if len(rows) > max_clips:
        raise PipelineError(
            f"{len(rows)} DVD silence candidates exceed the preview cap ({max_clips}); "
            "increase the explicit cap or split review into smaller sets")
    return rows


def _stream_metadata(ffprobe: str, path: Path) -> tuple[dict, float]:
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,sample_rate,channels,duration:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise PipelineError(f"ffprobe failed for {path}: {proc.stderr[-600:]}")
    try:
        payload = json.loads(proc.stdout)
        stream = next(row for row in payload.get("streams", [])
                      if row.get("codec_type") == "audio")
        duration = float((payload.get("format") or {}).get("duration") or stream.get("duration") or 0)
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(f"DVD audio sample metadata is invalid: {path}") from exc
    if duration <= 0:
        raise PipelineError(f"DVD audio sample has no measurable duration: {path}")
    return stream, duration


def build_audio_samples(ep: Any, report_path: Path, output_dir: Path,
                        *, max_clips: int, padding_s: float, max_clip_s: float,
                        ld: Ld) -> Path:
    if not 1 <= max_clips <= 32 or not 0 <= padding_s <= 10 or not 2 <= max_clip_s <= 30:
        raise PipelineError("DVD audio audition limits are outside the supported range")
    report_path = report_path.resolve(strict=True)
    probe = ld.probe().resolve(strict=True)
    try:
        report_path.relative_to(probe)
    except ValueError as exc:
        raise PipelineError("candidate report must remain in the episode probe folder") from exc
    report = _load_candidates(report_path, ep.identity)
    report_hash_before = sha256_file(report_path)
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    if sha256_file(manifest_path) != report.get("manifest_sha256"):
        raise PipelineError("manifest changed since retime candidate preparation")
    dvd = ep.path("dvd")
    if dvd.is_symlink() or not dvd.is_file():
        raise PipelineError("verified DVD source is missing or unsafe")
    dvd_hash_before = sha256_file(dvd)
    if (report.get("inputs") or {}).get("dvd_sha256") != dvd_hash_before:
        raise PipelineError("DVD source hash does not match the candidate report")
    selector, dvd_duration = resolve_dvd_audio(
        ld, dvd, (report.get("inputs") or {}).get("dvd_audio_selector"))
    stream = selector.split(":")
    stream_index = int(stream[-1])
    rows = _audio_intervals(report, max_clips)
    output_dir = assert_writable_path(output_dir, ld)
    estimated_bytes = sum(
        int(min(max_clip_s, max(2.0, float(row["end_s"]) - float(row["start_s"]) +
                                2 * padding_s)) * 48000 * 2 * 2 + 64 * 1024)
        for row in rows
    )
    reserve_gib = float((getattr(ld, "profile", {}) or {}).get("limits", {}).get("min_free_gb", 0) or 0)
    free_bytes = shutil.disk_usage(output_dir.parent).free
    required = estimated_bytes + int(reserve_gib * 1024 ** 3)
    if free_bytes < required:
        raise PipelineError(
            f"DVD audio review space gate FAIL: free={free_bytes / 1024**3:.2f} GiB; "
            f"estimated sample bytes={estimated_bytes}; configured reserve={reserve_gib:.2f} GiB")
    output_dir.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    filter_rows: list[str] = []
    out_paths: list[Path] = []
    for index, interval in enumerate(rows):
        try:
            start = float(interval["start_s"])
            end = float(interval["end_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError("DVD silence interval has invalid bounds") from exc
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= dvd_duration + 0.05):
            raise PipelineError(f"{interval.get('id')} has invalid DVD audio interval bounds")
        center = (start + end) / 2
        clip_duration = min(max_clip_s, max(2.0, end - start + 2 * padding_s))
        clip_start = max(0.0, center - clip_duration / 2)
        clip_end = min(dvd_duration, clip_start + clip_duration)
        clip_start = max(0.0, clip_end - clip_duration)
        output = output_dir / f"{interval['id']}.wav"
        if output.exists() or output.is_symlink():
            raise PipelineError(f"refusing to overwrite DVD audition sample: {output}")
        filter_rows.append(
            f"[src{index}]atrim=start={clip_start:.9f}:end={clip_end:.9f},"
            f"asetpts=PTS-STARTPTS,aresample=48000[out{index}]")
        out_paths.append(output)
        results.append({
            "id": str(interval["id"]), "start_s": start, "end_s": end,
            "sample_kind": interval.get("sample_kind", "DVD audio-silence interval"),
            "candidate_label": interval.get("candidate_label"),
            "clip_start_s": clip_start, "clip_end_s": clip_end,
            "duration_s": clip_end - clip_start,
            "path": str(output), "status": "PLANNED",
        })
    if rows:
        split = (f"[0:a:{stream_index}]asplit={len(rows)}" +
                 "".join(f"[src{index}]" for index in range(len(rows))))
        filter_graph = ";".join([split, *filter_rows])
        command: list[str] = [ld.ffmpeg(), "-n", "-hide_banner", "-v", "error",
                              "-i", str(dvd), "-filter_complex", filter_graph]
        for index, output in enumerate(out_paths):
            command += ["-map", f"[out{index}]", "-c:a", "pcm_s16le", "-ar", "48000",
                        "-ac", "2", "-f", "wav", str(output)]
        decoded = subprocess.run(command, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
        if decoded.returncode:
            raise PipelineError(
                f"sequential DVD audio audition decode failed: {decoded.stderr[-1000:]}")
        for row, path in zip(results, out_paths):
            if not path.is_file() or path.stat().st_size <= 44:
                raise PipelineError(f"DVD audio audition sample was not created: {path}")
            metadata, actual_duration = _stream_metadata(ld.ffprobe(), path)
            if (metadata.get("sample_rate") != "48000" or
                    int(metadata.get("channels") or 0) != 2 or
                    abs(actual_duration - row["duration_s"]) > 0.1):
                raise PipelineError(f"DVD audio audition sample failed duration/format verification: {path}")
            row.update({"status": "READY", "sha256": sha256_file(path),
                        "verified_duration_s": actual_duration,
                        "sample_rate": 48000, "channels": 2})
    if (sha256_file(dvd) != dvd_hash_before or
            sha256_file(report_path) != report_hash_before or
            sha256_file(manifest_path) != report.get("manifest_sha256")):
        raise PipelineError("DVD, candidate report, or manifest changed during audio sample generation")
    index = {
        "schema": "retime-dvd-audio-review-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "identity": ep.identity, "candidate_is_approval": False,
        "manifest_path": str(manifest_path), "manifest_sha256": sha256_file(manifest_path),
        "candidate_report_path": str(report_path),
        "candidate_report_sha256": report_hash_before,
        "dvd_source": str(dvd), "dvd_source_sha256": dvd_hash_before,
        "dvd_audio_selector": selector,
        "parameters": {"padding_s": padding_s, "max_clip_s": max_clip_s},
        "samples": results,
        "warning": "DVD audio excerpts are detector evidence only; they do not assign LD audio origin or retime offsets.",
    }
    index_path = output_dir / "index.json"
    with index_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(index, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return index_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--candidate-report", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--max-clips", type=int, default=16)
    parser.add_argument("--padding-s", type=float, default=2.0)
    parser.add_argument("--max-clip-s", type=float, default=20.0)
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    path = build_audio_samples(
        ep, Path(args.candidate_report), Path(args.out_dir),
        max_clips=args.max_clips, padding_s=args.padding_s,
        max_clip_s=args.max_clip_s, ld=ld)
    print(f"DVD retime-review audio samples: {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"RETIME AUDIO REVIEW ASSET ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
