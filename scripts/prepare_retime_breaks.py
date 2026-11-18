#!/usr/bin/env python3
"""Prepare a report-only, episode-agnostic retime-break pairing proposal.

This helper combines a reviewed-threshold LD luma-run report, the measured DVD/LD alignment
path, and DVD 5.1 silence intervals. It never writes the manifest and never chooses offsets.
All detector and matching parameters are explicit; multiple/overlapping matches, offset steps,
and missing alignment coverage remain visible as non-applicable statuses.

    python scripts/measure_ld_black.py --episode S01E12 --thresholds 5 10 15
    python scripts/prepare_retime_breaks.py --episode S01E12 \
        --ld-black-report <probe-report.json> --ld-threshold 15 \
        --dvd-silence-noise-db -40 --dvd-silence-min-duration 0.015 \
        --pair-tolerance-s 4 --max-offset-spread-frames 4

With no --report-out the JSON preview is written to stdout only. Supplying --report-out
creates one new report under the profile's probe root and refuses overwrite.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from dependency_lock import LockError  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from retime_break_candidates import (CANDIDATE_SCHEMA, pair_ld_intervals,
                                     sha256_file, validate_visual_alignment_approval)  # noqa: E402
from retime_dvd_assets import parse_silence_intervals  # noqa: E402

LD_BLACK_SCHEMA = "ld-pipeline-ld-black-intervals-v2"


def threshold_intervals(report: dict, threshold: float) -> list[dict]:
    if report.get("schema") != LD_BLACK_SCHEMA or report.get("status") != "CANDIDATE_ONLY":
        raise PipelineError("LD-black report schema/status is unsupported; regenerate with measure_ld_black.py")
    rows = report.get("thresholds_luma")
    if not isinstance(rows, dict):
        raise PipelineError("LD-black report has no thresholds_luma map")
    try:
        matching = [value for key, value in rows.items()
                    if math.isclose(float(key), threshold, rel_tol=0.0, abs_tol=1e-9)]
    except (TypeError, ValueError) as exc:
        raise PipelineError("LD-black report contains a malformed threshold key") from exc
    if len(matching) != 1 or not isinstance(matching[0], list):
        raise PipelineError(f"LD-black report has no unique interval list for threshold {threshold:g}")
    if any(not isinstance(row, dict) for row in matching[0]):
        raise PipelineError("LD-black threshold intervals must be objects")
    return matching[0]


def resolve_dvd_audio(ld: Ld, dvd: Path,
                      selector_override: str | None = None) -> tuple[str, float]:
    proc = subprocess.run(
        [ld.ffprobe(), "-v", "error", "-show_entries",
         "stream=codec_type,channels,channel_layout:format=duration", "-of", "json", str(dvd)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise PipelineError(f"ffprobe failed on DVD {dvd}: {proc.stderr[-800:]}")
    try:
        info = json.loads(proc.stdout or "{}")
        audio = [stream for stream in info.get("streams", [])
                 if stream.get("codec_type") == "audio"]
        duration = float((info.get("format") or {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PipelineError(f"invalid DVD stream metadata for {dvd}: {exc}") from exc
    six = [index for index, stream in enumerate(audio) if int(stream.get("channels") or 0) == 6]
    if selector_override:
        match = re.fullmatch(r"0:a:(\d+)", selector_override)
        if not match:
            raise PipelineError("--dvd-audio-selector must have form 0:a:N")
        index = int(match.group(1))
        if index >= len(audio) or int(audio[index].get("channels") or 0) != 6:
            raise PipelineError(f"DVD selector {selector_override} does not identify a six-channel audio stream")
    elif len(six) == 1:
        index = six[0]
    else:
        raise PipelineError(f"expected one six-channel DVD audio stream, found {len(six)}; inspect streams and pass --dvd-audio-selector explicitly")
    if duration <= 0:
        raise PipelineError(f"DVD duration unavailable: {dvd}")
    return f"0:a:{index}", duration


def detect_dvd_silences(ld: Ld, dvd: Path, noise_db: float,
                        minimum_duration_s: float,
                        selector_override: str | None = None) -> tuple[str, float, list[tuple[float, float]]]:
    if not math.isfinite(noise_db) or noise_db >= 0:
        raise PipelineError("DVD silence threshold must be a finite negative dB value")
    if not math.isfinite(minimum_duration_s) or minimum_duration_s <= 0:
        raise PipelineError("DVD silence minimum duration must be finite and positive")
    selector, duration = resolve_dvd_audio(ld, dvd, selector_override)
    detector = f"silencedetect=noise={noise_db:g}dB:d={minimum_duration_s:.6f}"
    command = [ld.ffmpeg(), "-hide_banner", "-nostats", "-i", str(dvd), "-map", selector,
               "-af", detector, "-f", "null", "-"]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise PipelineError(f"DVD silence scan failed (exit {proc.returncode}): {proc.stderr[-1000:]}")
    intervals = parse_silence_intervals(proc.stderr or "")
    return selector, duration, intervals


def _load_ld_report(path: Path, ep, master_descriptors: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"LD-black report must be an existing regular file, not a symlink: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read LD-black report {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise PipelineError(f"LD-black report root must be a JSON object: {path}")
    if (report.get("schema") != LD_BLACK_SCHEMA or report.get("episode") != ep.code or
            report.get("series") != ep.series or report.get("identity") != ep.identity):
        raise PipelineError(f"LD-black report identity/schema does not match {ep.identity}")
    if report.get("source_descriptors_sha256") != sha256_file(master_descriptors):
        raise PipelineError("LD descriptors changed since the LD-black threshold report; regenerate it")
    return report


def build_candidate_report(ep, ld_report_path: Path, ld_report: dict, ld_threshold: float,
                           dvd_silence_intervals: list[tuple[float, float]],
                           dvd_selector: str, dvd_duration_s: float, *,
                           dvd_silence_noise_db: float,
                           dvd_silence_minimum_duration_s: float,
                           pair_tolerance_s: float,
                           max_offset_spread_frames: float) -> dict:
    intervals = threshold_intervals(ld_report, ld_threshold)
    alignment_path = ep.path("align_npy")
    dvd_path = ep.path("dvd")
    master_path = ep.path("stack_master")
    if not alignment_path.is_file() or not dvd_path.is_file():
        raise PipelineError("candidate preparation needs the measured alignment path and verified DVD")
    validate_visual_alignment_approval(ep, alignment_path, dvd_path, master_path)
    probe_root = ep.ld.probe().resolve()
    try:
        ld_report_relative_path = ld_report_path.resolve().relative_to(probe_root).as_posix()
    except ValueError as exc:
        raise PipelineError("LD-black report must be under this episode's profile probe root") from exc
    alignment_sha256 = sha256_file(alignment_path)
    dvd_sha256 = sha256_file(dvd_path)
    dvd_stat_before = dvd_path.stat()
    import numpy as np
    alignment = np.load(alignment_path)
    if sha256_file(alignment_path) != alignment_sha256:
        raise PipelineError("alignment path changed while candidate pairing was being prepared")
    labels = [{**row, "label": f"B{index}"}
              for index, row in enumerate(intervals, 1)]
    try:
        ordered_dvd_silences = sorted((float(start), float(end))
                                      for start, end in dvd_silence_intervals)
    except (TypeError, ValueError) as exc:
        raise PipelineError("DVD silence detector returned malformed interval bounds") from exc
    candidates = pair_ld_intervals(
        labels, ordered_dvd_silences, alignment,
        pair_tolerance_s=pair_tolerance_s,
        max_offset_spread_frames=max_offset_spread_frames)
    if (sha256_file(alignment_path) != alignment_sha256 or
            sha256_file(dvd_path) != dvd_sha256):
        raise PipelineError("alignment or DVD changed while retime candidates were being prepared")
    manifest_path = episode_manifest_path(ep.code, ep.ld, series=ep.series)
    status = "REPORT_ONLY" if candidates else "NO_CANDIDATES"
    return {
        "schema": CANDIDATE_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "series": ep.series,
        "episode": ep.code,
        "identity": ep.identity,
        "status": status,
        "requires_human_review": True,
        "manifest_sha256": sha256_file(manifest_path),
        "inputs": {
            "ld_black_report_relative_path": ld_report_relative_path,
            "ld_black_report_sha256": sha256_file(ld_report_path),
            "alignment_sha256": alignment_sha256,
            "dvd_name": dvd_path.name,
            "dvd_sha256": dvd_sha256,
            "dvd_size_bytes": dvd_stat_before.st_size,
            "dvd_mtime_ns": dvd_stat_before.st_mtime_ns,
            "dvd_duration_s": dvd_duration_s,
            "dvd_audio_selector": dvd_selector,
        },
        "parameters": {
            "ld_luma_threshold": ld_threshold,
            "ld_minimum_run_duration_s": ld_report.get("minimum_run_duration_s"),
            "ld_thresholds_available": sorted(
                float(value) for value in (ld_report.get("thresholds_luma") or {})),
            "ld_run_count_by_threshold": {
                str(value): len(rows) for value, rows in
                sorted((ld_report.get("thresholds_luma") or {}).items(),
                       key=lambda item: float(item[0]))
            },
            "dvd_silence_noise_db": dvd_silence_noise_db,
            "dvd_silence_minimum_duration_s": dvd_silence_minimum_duration_s,
            "pair_tolerance_s": pair_tolerance_s,
            "max_alignment_offset_spread_frames": max_offset_spread_frames,
            "alignment_grid_fps": "30000/1001",
        },
        "dvd_silence_intervals": [
            {"id": f"D{index}", "start_s": start, "end_s": end}
            for index, (start, end) in enumerate(ordered_dvd_silences, 1)
        ],
        "candidates": candidates,
        "manifest_updated": False,
        "caveat": "Intervals/pairings are candidates only; no offsets or retime decisions were inferred.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True, help="SxxExx (DS9 legacy) or SERIES:SxxExx")
    parser.add_argument("--ld-black-report", help="report from measure_ld_black.py; defaults to the profile probe report")
    parser.add_argument("--ld-threshold", required=True, type=float,
                        help="explicit luma threshold to inspect; no threshold is selected automatically")
    parser.add_argument("--dvd-silence-noise-db", required=True, type=float,
                        help="episode-reviewed silencedetect noise threshold")
    parser.add_argument("--dvd-silence-min-duration", required=True, type=float,
                        help="episode-reviewed minimum silence duration")
    parser.add_argument("--dvd-audio-selector",
                        help="explicit 0:a:N override when the DVD has multiple six-channel streams")
    parser.add_argument("--pair-tolerance-s", required=True, type=float,
                        help="maximum distance for listing a possible DVD-silence pairing")
    parser.add_argument("--max-offset-spread-frames", required=True, type=float,
                        help="maximum alignment-path spread for a unique pairing; larger spreads are flagged")
    parser.add_argument("--report-out", help="new report path under roots.probe; omitted means stdout preview only")
    args = parser.parse_args()
    try:
        ld = Ld()
        ep = load_episode(args.episode, ld)
        probe = ld.probe().resolve()
        raw_ld_report = (Path(args.ld_black_report).expanduser() if args.ld_black_report
                         else probe / f"{ep.code}-ld-black-intervals.json")
        if raw_ld_report.is_symlink():
            raise PipelineError(f"LD-black report may not be a symlink: {raw_ld_report}")
        ld_report_path = raw_ld_report.resolve()
        try:
            ld_report_path.relative_to(probe)
        except ValueError as exc:
            raise PipelineError("LD-black report must be under this episode's profile probe root") from exc
        master_descriptors = ep.path("master_descriptors")
        ld_report_sha256 = sha256_file(ld_report_path)
        ld_report = _load_ld_report(ld_report_path, ep, master_descriptors)
        dvd_path = ep.path("dvd")
        dvd_stat_before = dvd_path.stat()
        dvd_selector, dvd_duration_s, dvd_silences = detect_dvd_silences(
            ld, dvd_path, args.dvd_silence_noise_db,
            args.dvd_silence_min_duration, args.dvd_audio_selector)
        if (sha256_file(ld_report_path) != ld_report_sha256 or
                (dvd_path.stat().st_size, dvd_path.stat().st_mtime_ns) !=
                (dvd_stat_before.st_size, dvd_stat_before.st_mtime_ns)):
            raise PipelineError("an input changed while the DVD silence scan was running; rerun preparation")
        report = build_candidate_report(
            ep, ld_report_path, ld_report, args.ld_threshold, dvd_silences,
            dvd_selector, dvd_duration_s,
            dvd_silence_noise_db=args.dvd_silence_noise_db,
            dvd_silence_minimum_duration_s=args.dvd_silence_min_duration,
            pair_tolerance_s=args.pair_tolerance_s,
            max_offset_spread_frames=args.max_offset_spread_frames)
        if (report["inputs"]["ld_black_report_sha256"] != ld_report_sha256 or
                report["inputs"]["dvd_size_bytes"] != dvd_stat_before.st_size or
                report["inputs"]["dvd_mtime_ns"] != dvd_stat_before.st_mtime_ns):
            raise PipelineError("an input changed during candidate preparation; rerun and review a fresh report")
        if args.report_out:
            raw_out = Path(args.report_out).expanduser()
            if raw_out.is_symlink():
                raise PipelineError(f"candidate report output may not be a symlink: {raw_out}")
            out = raw_out.resolve()
            try:
                out.relative_to(probe)
            except ValueError as exc:
                raise PipelineError("candidate report output must be under this episode's roots.probe") from exc
            assert_writable_path(out, ld)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            print(f"REPORT ONLY: wrote new candidate report {out}")
            for row in report["candidates"]:
                print(f"{row['label']}: {row['pair_status']} ({len(row['pair_candidates'])} possible DVD silence(s))")
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report["candidates"]:
            print("NO CANDIDATES: retained the report for in-app review; no manifest changes made")
        return 0
    except (OSError, json.JSONDecodeError, ValueError, LockError, PipelineError) as exc:
        print(f"RETIME CANDIDATE ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
