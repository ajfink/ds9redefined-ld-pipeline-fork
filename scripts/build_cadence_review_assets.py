#!/usr/bin/env python3
"""Build short side-by-side raw/TFM visual samples for every cadence review interval."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fileutil import sha256_file  # noqa: E402
from ldcommon import Ld, PipelineError, assert_writable_path, episode_manifest_path, load_episode  # noqa: E402

SAMPLE_RADIUS = 36
SAMPLE_STEP = 3
MAX_REVIEW_INTERVALS = 32
WIDTH, HEIGHT = 960, 320


def _read(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"{label} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} root must be a JSON object")
    return value


def _selected_frames(report: dict[str, Any], max_intervals: int) -> list[dict[str, Any]]:
    review = list(report.get("review_intervals") or [])
    clean = list(report.get("clean_samples") or [])
    if len(review) > max_intervals:
        raise PipelineError(
            f"{len(review)} cadence intervals need visual review, exceeding the bounded "
            f"{max_intervals}-sample review set; split/inspect the interval set rather than omitting flags")
    samples = [{**row, "sample_kind": "review-required"} for row in review]
    samples.extend({**row, "sample_kind": "representative-clean"} for row in clean)
    seen: set[tuple[int, str]] = set()
    output = []
    for index, row in enumerate(samples):
        try:
            start = int(row.get("start_frame", row.get("start_frame")))
            end = int(row.get("end_frame_inclusive", row.get("end_frame_inclusive", start)))
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"cadence review interval {index} has invalid frame bounds") from exc
        if start < 0 or end < start:
            raise PipelineError(f"cadence review interval {index} has invalid frame bounds")
        center = int(row.get("representative_frame", (start + end) // 2))
        if not start <= center <= end:
            raise PipelineError(f"cadence review interval {index} has an out-of-range representative frame")
        key = (center, row["sample_kind"])
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "id": f"{row['sample_kind']}-{index + 1:03d}",
            "sample_kind": row["sample_kind"],
            "start_frame": start, "end_frame_inclusive": end,
            "center_frame": center,
            "start_s": float(row.get("start_s", start / (30000 / 1001))),
            "end_s": float(row.get("end_s", end / (30000 / 1001))),
            "reasons": list(row.get("reasons") or []),
        })
    return output


def _extract_frames(ld: Ld, review_avs: Path, frames: list[int], output: Path) -> dict[int, Path]:
    if not frames:
        return {}
    ordered = sorted(set(frames))
    expression = "+".join(f"eq(n,{frame})" for frame in ordered)
    pattern = output / "frame_%06d.png"
    output.mkdir(parents=True, exist_ok=False)
    command = [
        ld.ffmpeg(), "-hide_banner", "-v", "error", "-f", "avisynth", "-i", str(review_avs),
        "-vf", f"select='{expression}',scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
              f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black",
        "-fps_mode", "passthrough", "-start_number", "0", str(pattern),
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise PipelineError(f"TFM visual sample extraction failed: {result.stderr[-800:]}")
    images = sorted(output.glob("frame_*.png"))
    if len(images) != len(ordered):
        raise PipelineError(f"requested {len(ordered)} cadence frames but extracted {len(images)}")
    from PIL import Image
    mapped = {}
    for frame, path in zip(ordered, images):
        with Image.open(path) as image:
            if image.size != (WIDTH, HEIGHT):
                raise PipelineError(f"cadence sample frame has wrong size: {image.size}")
        mapped[frame] = path
    return mapped


def build_assets(ep: Any, metrics_path: Path, verification_path: Path,
                 review_avs: Path, map_script: Path, summary_path: Path, output_dir: Path,
                 max_intervals: int, ld: Ld) -> Path:
    if not 1 <= max_intervals <= MAX_REVIEW_INTERVALS:
        raise PipelineError(f"visual sample limit must be 1..{MAX_REVIEW_INTERVALS}")
    metrics = _read(metrics_path, "cadence measurement report")
    verification = _read(verification_path, "TFM coverage report")
    if (metrics.get("schema") != "cadence-measurements-v1" or
            metrics.get("identity") != ep.identity or
            metrics.get("candidate_is_approval") is not False or
            verification.get("status") != "VERIFIED" or
            verification.get("identity") != ep.identity):
        raise PipelineError("cadence review assets require verified candidate-only evidence")
    manifest = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    if (sha256_file(manifest) != metrics.get("inputs", {}).get("manifest_sha256") or
            sha256_file(manifest) != verification.get("manifest_sha256")):
        raise PipelineError("manifest changed after cadence evidence generation")
    source = ep.path("stack_master")
    if sha256_file(source) != verification.get("stack_master_sha256"):
        raise PipelineError("stack master changed after cadence evidence generation")
    native_audio = Path(str((metrics.get("inputs") or {}).get("native_audio_path") or "")).resolve(strict=True)
    if sha256_file(native_audio) != (metrics.get("inputs") or {}).get("native_audio_sha256"):
        raise PipelineError("native LD audio changed after cadence evidence generation")
    motion_report = Path(str((metrics.get("inputs") or {}).get("motion_report_path") or "")).resolve(strict=True)
    if sha256_file(motion_report) != (metrics.get("inputs") or {}).get("motion_report_sha256"):
        raise PipelineError("motion trace verification report changed after cadence evidence generation")
    tfm_log = Path(str((metrics.get("inputs") or {}).get("tfm_log_path") or "")).resolve(strict=True)
    motion_trace = Path(str((metrics.get("inputs") or {}).get("motion_trace_path") or "")).resolve(strict=True)
    di_map = Path(str((metrics.get("inputs") or {}).get("di_map_path") or "")).resolve(strict=True)
    for path, expected_hash, label in (
            (tfm_log, (metrics.get("inputs") or {}).get("tfm_log_sha256"), "TFM log"),
            (motion_trace, (metrics.get("inputs") or {}).get("motion_trace_sha256"), "motion trace"),
            (di_map, (metrics.get("inputs") or {}).get("di_map_sha256"), "deinterlace map")):
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise PipelineError(f"{label} changed after cadence analysis")
    if sha256_file(tfm_log) != verification.get("tfm_log_sha256"):
        raise PipelineError("TFM log differs from its full-coverage verification report")
    for path in (metrics_path, verification_path, review_avs, map_script, summary_path,
                 motion_report, tfm_log, motion_trace, di_map):
        if path.is_symlink() or not path.is_file():
            raise PipelineError(f"cadence visual input is missing or unsafe: {path}")
    metrics_hash, verification_hash = sha256_file(metrics_path), sha256_file(verification_path)
    map_hash, review_avs_hash = sha256_file(map_script), sha256_file(review_avs)
    summary_hash = sha256_file(summary_path)
    samples = _selected_frames(metrics, max_intervals)
    frame_count = int(metrics["frame_count"])
    for row in samples:
        center = row["center_frame"]
        row["frames"] = list(range(
            max(0, center - SAMPLE_RADIUS), min(frame_count - 1, center + SAMPLE_RADIUS) + 1,
            SAMPLE_STEP))
    unique_frames = sorted({frame for row in samples for frame in row["frames"]})
    output_dir = assert_writable_path(output_dir, ld)
    # The clips are compact PNG/GIF samples; reserve enough space even if PNGs do not compress well.
    worst = len(unique_frames) * WIDTH * HEIGHT * 3 + len(samples) * (2 * SAMPLE_RADIUS + 1) * WIDTH * HEIGHT
    reserve_gib = float((ld.profile.get("limits") or {}).get("min_free_gb", 0) or 0)
    free = shutil.disk_usage(output_dir.parent).free
    if free < worst + int(reserve_gib * 1024 ** 3):
        raise PipelineError(
            f"cadence visual-review space gate FAIL: free={free / 1024**3:.2f} GiB; "
            f"estimated bytes={worst}; reserve={reserve_gib:.2f} GiB")
    output_dir.mkdir(parents=True, exist_ok=False)
    ready: list[dict[str, Any]] = []
    contact_path = output_dir / "cadence-contact-sheet.png"
    if unique_frames:
        with tempfile.TemporaryDirectory(prefix="cadence-frames-", dir=str(output_dir)) as temp:
            frame_map = _extract_frames(ld, review_avs, unique_frames, Path(temp) / "frames")
            from PIL import Image, ImageDraw, ImageFont
            cell_h = HEIGHT + 30
            contact = Image.new("RGB", (WIDTH, max(1, len(samples)) * cell_h), "black")
            draw = ImageDraw.Draw(contact)
            for index, row in enumerate(samples):
                seq = row["frames"]
                center = row["center_frame"]
                gif_frames = []
                for frame in seq:
                    with Image.open(frame_map[frame]) as image:
                        current = image.convert("RGB").copy()
                    canvas = Image.new("RGB", (WIDTH, HEIGHT + 24), "black")
                    canvas.paste(current, (0, 24))
                    ImageDraw.Draw(canvas).text(
                        (4, 4), f"TFM source frame {frame}; raw left / matched right",
                        fill="white", font=ImageFont.load_default())
                    gif_frames.append(canvas)
                first = frame_map[center]
                with Image.open(first) as image:
                    contact.paste(image.convert("RGB"), (0, index * cell_h + 30))
                draw.text(
                    (4, index * cell_h + 5),
                    f"{row['sample_kind']}: {row['start_s']:.2f}..{row['end_s']:.2f}s "
                    f"center f{center}; {', '.join(row['reasons'])}",
                    fill="white", font=ImageFont.load_default())
                gif_path = output_dir / f"{row['id']}.gif"
                gif_frames[0].save(gif_path, save_all=True, append_images=gif_frames[1:],
                                   duration=100, loop=0, optimize=True)
                row.update({"status": "READY", "playback_path": str(gif_path),
                            "playback_sha256": sha256_file(gif_path)})
                ready.append(row)
            contact.save(contact_path, format="PNG", optimize=True)
    current = {
        "manifest": sha256_file(manifest), "metrics": sha256_file(metrics_path),
        "verification": sha256_file(verification_path), "review_avs": sha256_file(review_avs),
        "map": sha256_file(map_script), "source": sha256_file(source),
        "native_audio": sha256_file(native_audio),
        "motion_report": sha256_file(motion_report),
        "summary": sha256_file(summary_path),
    }
    expected = {
        "manifest": metrics["inputs"]["manifest_sha256"], "metrics": metrics_hash,
        "verification": verification_hash, "review_avs": review_avs_hash,
        "map": map_hash, "source": verification["stack_master_sha256"],
        "native_audio": (metrics.get("inputs") or {}).get("native_audio_sha256"),
        "motion_report": (metrics.get("inputs") or {}).get("motion_report_sha256"),
        "summary": summary_hash,
    }
    if current != expected:
        raise PipelineError("cadence visual inputs changed during sample generation")
    index = {
        "schema": "cadence-review-assets-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "identity": ep.identity, "candidate_is_approval": False,
        "manifest_path": str(manifest), "manifest_sha256": expected["manifest"],
        "metrics_path": str(metrics_path), "metrics_sha256": metrics_hash,
        "verification_report_path": str(verification_path), "verification_report_sha256": verification_hash,
        "review_avs_path": str(review_avs), "review_avs_sha256": review_avs_hash,
        "map_script_path": str(map_script), "map_script_sha256": map_hash,
        "legacy_summary_path": str(summary_path), "legacy_summary_sha256": summary_hash,
        "source_path": str(source), "source_sha256": expected["source"],
        "native_audio_path": str(native_audio),
        "native_audio_sha256": (metrics.get("inputs") or {}).get("native_audio_sha256"),
        "motion_report_path": str(motion_report),
        "motion_report_sha256": (metrics.get("inputs") or {}).get("motion_report_sha256"),
        "tfm_log_path": str(tfm_log), "tfm_log_sha256": sha256_file(tfm_log),
        "motion_trace_path": str(motion_trace), "motion_trace_sha256": sha256_file(motion_trace),
        "di_map_path": str(di_map), "di_map_sha256": sha256_file(di_map),
        "frame_count": frame_count, "review_intervals": ready,
        "source_rate_info": metrics.get("source_rate_info"),
        "contact_sheet": str(contact_path) if contact_path.is_file() else None,
        "contact_sheet_sha256": sha256_file(contact_path) if contact_path.is_file() else None,
        "warning": "Cadence measurements remain candidate-only until every flagged visual interval is explicitly reviewed.",
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
    parser.add_argument("--metrics-report", required=True)
    parser.add_argument("--verification-report", required=True)
    parser.add_argument("--review-avs", required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--max-intervals", type=int, default=MAX_REVIEW_INTERVALS)
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    index = build_assets(
        ep, Path(args.metrics_report), Path(args.verification_report),
        Path(args.review_avs), Path(args.map), Path(args.summary), Path(args.out_dir),
        args.max_intervals, ld)
    print(f"candidate cadence visual-review assets: {index}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, PipelineError) as exc:
        print(f"CADENCE REVIEW ASSETS ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
