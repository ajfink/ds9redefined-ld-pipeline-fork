#!/usr/bin/env python3
"""Build hash-bound paired-frame and short-playback evidence for an alignment candidate.

DVD frames are decoded sequentially; no timestamp seek is used. Candidate offsets are
decision aids only, and the output never approves or records an alignment.
"""
from __future__ import annotations

import argparse
import csv
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
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from measure_dvd_alignment import FPS, load_desc  # noqa: E402

THUMB_W, THUMB_H = 320, 240
CLIP_RADIUS_FRAMES = 36
CLIP_STEP_FRAMES = 3
MAX_REVIEW_POINTS = 12
DESCRIPTOR_BYTES = 48 * 36


def load_diagnostics(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"alignment diagnostics are missing or unsafe: {path}")
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    required = {"master_frame", "master_time_s", "dvd_minus_master_frames",
                "ncc", "local_margin", "search_edge"}
    if not rows or not required.issubset(rows[0]):
        raise PipelineError("alignment TSV has no rows or is missing required diagnostic columns")
    result = []
    previous = -1
    for index, raw in enumerate(rows):
        try:
            master_frame = int(raw["master_frame"])
            offset = int(raw["dvd_minus_master_frames"])
            master_time = float(raw["master_time_s"])
            ncc = float(raw["ncc"])
            margin = float(raw["local_margin"])
            edge = bool(int(raw["search_edge"]))
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"malformed alignment diagnostic row {index + 2}") from exc
        if (master_frame <= previous or not all(math.isfinite(value)
                                                for value in (master_time, ncc, margin))):
            raise PipelineError(f"non-finite or non-monotonic alignment diagnostic row {index + 2}")
        previous = master_frame
        result.append({
            "row": index, "master_frame": master_frame, "master_time_s": master_time,
            "offset_frames": offset, "dvd_frame": master_frame + offset,
            "ncc": ncc, "local_margin": margin, "search_edge": edge,
        })
    return result


def select_review_points(rows: list[dict[str, Any]], *, warn_ncc: float,
                         transition_threshold: int,
                         anchors: list[tuple[str, int]] | None = None,
                         limit: int = MAX_REVIEW_POINTS) -> tuple[list[dict[str, Any]], list[str]]:
    if not rows:
        raise PipelineError("cannot select visual review points from an empty path")
    chosen: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    deferred: list[str] = []

    def add(row: dict[str, Any], reason: str) -> None:
        key = (row["master_frame"], row["offset_frames"])
        if key in seen:
            existing = next(item for item in chosen
                            if (item["master_frame"], item["offset_frames"]) == key)
            if reason not in existing["reasons"]:
                existing["reasons"].append(reason)
            return
        candidate = {**row, "reasons": [reason]}
        if len(chosen) < limit:
            chosen.append(candidate)
            seen.add(key)
        else:
            deferred.append(f"{reason} at master frame {row['master_frame']} omitted by the {limit}-sample cap")

    add(rows[0], "start of sampled path")
    add(rows[-1], "end of sampled path")
    for label, frame in anchors or []:
        nearest = min(rows, key=lambda row: abs(row["master_frame"] - frame))
        add(nearest, label)
    edges = [row for row in rows if row["search_edge"]]
    if edges:
        add(edges[0], "offset search-range edge")
        if len(edges) > 1:
            add(edges[-1], "offset search-range edge")
    weak = [row for row in rows if row["ncc"] < warn_ncc]
    if weak:
        add(min(weak, key=lambda row: row["ncc"]), "lowest correlation below configured marker")
    add(min(rows, key=lambda row: row["local_margin"]), "lowest local match margin")

    transitions = []
    for before, after in zip(rows, rows[1:]):
        change = abs(after["offset_frames"] - before["offset_frames"])
        if change >= transition_threshold:
            transitions.append((change, before, after))
    transitions.sort(key=lambda item: (-item[0], item[1]["master_frame"]))
    for change, before, after in transitions:
        add(before, f"large offset transition before ({change} frames)")
        add(after, f"large offset transition after ({change} frames)")
    return chosen, deferred


def extract_exact_frames(ld: Ld, source: Path, frames: list[int],
                         descriptors: np.ndarray, output: Path) -> dict[int, Path]:
    if not frames or len(frames) != len(set(frames)):
        raise PipelineError("paired-frame extraction list is empty or contains duplicate indices")
    if any(frame < 0 or frame >= len(descriptors) for frame in frames):
        raise PipelineError(f"requested frame is outside descriptor coverage for {source.name}")
    ordered = sorted(frames)
    output.mkdir(parents=True, exist_ok=False)
    expression = "+".join(f"eq(n,{frame})" for frame in ordered)
    filt = (
        f"[0:v:0]select='{expression}',split=2[full][small];"
        f"[full]scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
        f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:black,format=rgb24[hi];"
        "[small]scale=48:36:flags=area,format=gray[lo]"
    )
    pattern = output / "frame_%06d.png"
    command = [
        ld.ffmpeg(), "-hide_banner", "-v", "error", "-i", str(source),
        "-filter_complex", filt,
        "-map", "[hi]", "-fps_mode", "passthrough", "-start_number", "0", str(pattern),
        "-map", "[lo]", "-fps_mode", "passthrough", "-f", "rawvideo",
        "-pix_fmt", "gray", "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise PipelineError(
            f"sequential selected-frame decode failed for {source.name}: "
            f"{result.stderr.decode('utf-8', 'replace')[-1000:]}")
    import PIL.Image  # required for verifying/displaying source-derived stills
    images = sorted(output.glob("frame_*.png"))
    if len(images) != len(ordered):
        raise PipelineError(f"selected {len(ordered)} frames but FFmpeg produced {len(images)} images")
    raw = result.stdout
    if len(raw) != len(ordered) * DESCRIPTOR_BYTES:
        raise PipelineError("selected-frame descriptor stream has an unexpected byte count")
    low = np.frombuffer(raw, dtype=np.uint8).reshape(len(ordered), DESCRIPTOR_BYTES)
    paths = {}
    for index, (frame, image_path) in enumerate(zip(ordered, images)):
        with PIL.Image.open(image_path) as image:
            if image.size != (THUMB_W, THUMB_H):
                raise PipelineError(f"selected frame {frame} has unexpected dimensions {image.size}")
        # Matching this frame's low-resolution grayscale descriptor proves the selected
        # source decode index agrees with the candidate path's descriptor grid.
        if not np.array_equal(low[index], descriptors[frame]):
            raise PipelineError(
                f"source frame index {frame} does not match the descriptor grid; "
                "refusing to display potentially mispaired evidence")
        paths[frame] = image_path
    return paths


def _paint_label(image, text: str) -> None:
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image.width, 24), fill=(0, 0, 0))
    draw.text((5, 5), text, fill=(255, 255, 255), font=ImageFont.load_default())


def create_review_visuals(output_dir: Path, points: list[dict[str, Any]],
                          master_frames: dict[int, Path],
                          dvd_frames: dict[int, Path]) -> tuple[Path | None, list[dict[str, Any]]]:
    from PIL import Image, ImageDraw, ImageFont

    contact_path = output_dir / "paired-contact-sheet.png"
    sheet_width = THUMB_W * 2
    cell_height = THUMB_H + 30
    sheet = Image.new("RGB", (sheet_width, max(1, len(points)) * cell_height), "black")
    draw = ImageDraw.Draw(sheet)
    clip_rows: list[dict[str, Any]] = []
    for index, point in enumerate(points, 1):
        center = int(point["master_frame"])
        offset = int(point["offset_frames"])
        master_seq = list(range(center - CLIP_RADIUS_FRAMES,
                                center + CLIP_RADIUS_FRAMES + 1, CLIP_STEP_FRAMES))
        dvd_seq = [frame + offset for frame in master_seq]
        if any(frame not in master_frames for frame in master_seq) or any(
                frame not in dvd_frames for frame in dvd_seq):
            point["asset_status"] = "DEFERRED"
            point["asset_reason"] = "one or more paired frames are outside verified decode coverage"
            continue
        master_center = master_frames[center]
        dvd_center = dvd_frames[center + offset]
        with Image.open(master_center) as image:
            sheet.paste(image.convert("RGB"), (0, (index - 1) * cell_height + 30))
        with Image.open(dvd_center) as image:
            sheet.paste(image.convert("RGB"), (THUMB_W, (index - 1) * cell_height + 30))
        reason = ", ".join(point["reasons"])
        draw.text((4, (index - 1) * cell_height + 5),
                  f"{center / FPS:.2f}s f{center}  offset {offset:+d}  "
                  f"NCC {point['ncc']:.3f}  margin {point['local_margin']:.3f}  {reason}",
                  fill=(255, 255, 255), font=ImageFont.load_default())
        gif_path = output_dir / f"pair-{index:02d}.gif"
        frames = []
        for master_frame, dvd_frame in zip(master_seq, dvd_seq):
            canvas = Image.new("RGB", (THUMB_W * 2, THUMB_H + 26), "black")
            with Image.open(master_frames[master_frame]) as image:
                canvas.paste(image.convert("RGB"), (0, 26))
            with Image.open(dvd_frames[dvd_frame]) as image:
                canvas.paste(image.convert("RGB"), (THUMB_W, 26))
            ImageDraw.Draw(canvas).text(
                (4, 5), f"master f{master_frame}  |  DVD f{dvd_frame}  |  "
                        f"local candidate offset {offset:+d}",
                fill=(255, 255, 255), font=ImageFont.load_default())
            frames.append(canvas)
        frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                       duration=100, loop=0, optimize=True)
        point.update({
            "asset_status": "READY", "contact_master_frame": center,
            "contact_dvd_frame": center + offset, "contact_sheet": str(contact_path),
            "playback_path": str(gif_path),
            "playback_sha256": sha256_file(gif_path),
            "master_frame_sequence": master_seq, "dvd_frame_sequence": dvd_seq,
        })
        clip_rows.append(point)
    if clip_rows:
        sheet.save(contact_path, format="PNG", optimize=True)
        return contact_path, clip_rows
    return None, clip_rows


def build_assets(ep: Any, alignment: Path, report: Path,
                 master_desc_path: Path, dvd_desc_path: Path,
                 output_dir: Path, warn_ncc: float,
                 transition_threshold: int, max_points: int, ld: Ld,
                 min_offset: int = -3000, max_offset: int = 500,
                 sample_step: int = 15) -> Path:
    if not -1 <= warn_ncc <= 1 or transition_threshold < 1 or not 1 <= max_points <= MAX_REVIEW_POINTS:
        raise PipelineError("alignment review marker, transition size, or point limit is invalid")
    if (type(min_offset) is not int or type(max_offset) is not int or
            max_offset <= min_offset or max_offset - min_offset > 5000 or
            type(sample_step) is not int or sample_step <= 0):
        raise PipelineError("alignment review search-range metadata is invalid")
    for path in (alignment, report, master_desc_path, dvd_desc_path):
        if path.is_symlink() or not path.is_file():
            raise PipelineError(f"alignment review input is missing or unsafe: {path}")
    output_dir = assert_writable_path(output_dir, ld)
    frames_per_sample = (2 * CLIP_RADIUS_FRAMES) // CLIP_STEP_FRAMES + 1
    worst_case = (
        MAX_REVIEW_POINTS * frames_per_sample * 2 * THUMB_W * THUMB_H * 3 +
        MAX_REVIEW_POINTS * frames_per_sample * THUMB_W * 2 * (THUMB_H + 26) * 3 +
        MAX_REVIEW_POINTS * 2 * THUMB_W * (THUMB_H + 30) * 3
    )
    reserve_gib = float((getattr(ld, "profile", {}) or {}).get("limits", {}).get("min_free_gb", 0) or 0)
    free_bytes = shutil.disk_usage(output_dir.parent).free
    required = worst_case + int(reserve_gib * 1024 ** 3)
    if free_bytes < required:
        raise PipelineError(
            f"alignment-review space gate FAIL: free={free_bytes / 1024**3:.2f} GiB; "
            f"worst-case temporary/output bytes={worst_case}; configured reserve={reserve_gib:.2f} GiB")
    output_dir.mkdir(parents=True, exist_ok=False)
    rows = load_diagnostics(report)
    master = load_desc(master_desc_path, "master", ep.code, ep.path("stack_master"))
    dvd = load_desc(dvd_desc_path, "dvd", ep.code, ep.path("dvd"))
    master_meta = master_desc_path.with_suffix(master_desc_path.suffix + ".meta.json")
    dvd_meta = dvd_desc_path.with_suffix(dvd_desc_path.suffix + ".meta.json")
    try:
        master_meta_value = json.loads(master_meta.read_text(encoding="utf-8-sig"))
        dvd_meta_value = json.loads(dvd_meta.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"descriptor metadata is invalid: {exc}") from exc
    source_master, source_dvd = ep.path("stack_master"), ep.path("dvd")
    manifest_path = episode_manifest_path(ep.code, ep.ld, series=ep.series)
    inputs = {
        "manifest": sha256_file(manifest_path),
        "alignment": sha256_file(alignment), "diagnostics": sha256_file(report),
        "master_descriptors": sha256_file(master_desc_path),
        "master_descriptors_meta": sha256_file(master_meta),
        "dvd_descriptors": sha256_file(dvd_desc_path),
        "dvd_descriptors_meta": sha256_file(dvd_meta),
        "master_source": sha256_file(source_master), "dvd_source": sha256_file(source_dvd),
    }
    candidate_path = np.load(alignment, allow_pickle=False)
    if (candidate_path.ndim != 2 or candidate_path.shape[1] < 2 or
            len(candidate_path) != len(rows)):
        raise PipelineError("alignment NPY and TSV row counts/shapes disagree")
    for row, candidate in zip(rows, candidate_path):
        if int(candidate[0]) != row["master_frame"] or int(candidate[1]) != row["offset_frames"]:
            raise PipelineError("alignment NPY and diagnostic TSV contain different candidate paths")
    video = ep.data.get("video") or {}
    anchors: list[tuple[str, int]] = []
    delta = video.get("delta_s")
    if isinstance(delta, (int, float)) and not isinstance(delta, bool) and math.isfinite(float(delta)):
        anchors.append(("measured DVD-origin neighborhood", round(float(delta) * FPS)))
    duration = video.get("duration_s")
    if (isinstance(delta, (int, float)) and not isinstance(delta, bool) and
            isinstance(duration, (int, float)) and not isinstance(duration, bool) and
            math.isfinite(float(delta)) and math.isfinite(float(duration)) and duration > 0):
        anchors.append(("measured content-end neighborhood", round((float(delta) + float(duration)) * FPS)))
    points, deferred = select_review_points(
        rows, warn_ncc=warn_ncc, transition_threshold=transition_threshold,
        anchors=anchors, limit=max_points)
    for point in points:
        center = point["master_frame"]
        offset = point["offset_frames"]
        point["master_sequence"] = list(range(
            center - CLIP_RADIUS_FRAMES, center + CLIP_RADIUS_FRAMES + 1, CLIP_STEP_FRAMES))
        point["dvd_sequence"] = [frame + offset for frame in point["master_sequence"]]
        if (min(point["master_sequence"]) < 0 or max(point["master_sequence"]) >= len(master) or
                min(point["dvd_sequence"]) < 0 or max(point["dvd_sequence"]) >= len(dvd)):
            point["asset_status"] = "DEFERRED"
            point["asset_reason"] = "short playback window would leave source/descriptor coverage"
            deferred.append(
                f"sample at master frame {center} omitted; its local DVD/master window leaves source coverage")
    ready = [point for point in points if point.get("asset_status") != "DEFERRED"]
    contact_path = None
    clips: list[dict[str, Any]] = []
    if ready:
        master_indexes = sorted({frame for point in ready for frame in point["master_sequence"]})
        dvd_indexes = sorted({frame for point in ready for frame in point["dvd_sequence"]})
        with tempfile.TemporaryDirectory(prefix="alignment-frames-", dir=str(output_dir)) as temporary:
            temporary_root = Path(temporary)
            master_frame_paths = extract_exact_frames(
                ld, ep.path("stack_master"), master_indexes, master,
                temporary_root / "master")
            dvd_frame_paths = extract_exact_frames(
                ld, ep.path("dvd"), dvd_indexes, dvd,
                temporary_root / "dvd")
            contact_path, clips = create_review_visuals(
                output_dir, ready, master_frame_paths, dvd_frame_paths)
    current_inputs = {
        "manifest": sha256_file(manifest_path),
        "alignment": sha256_file(alignment), "diagnostics": sha256_file(report),
        "master_descriptors": sha256_file(master_desc_path),
        "master_descriptors_meta": sha256_file(master_meta),
        "dvd_descriptors": sha256_file(dvd_desc_path),
        "dvd_descriptors_meta": sha256_file(dvd_meta),
        "master_source": sha256_file(source_master), "dvd_source": sha256_file(source_dvd),
    }
    if current_inputs != inputs:
        raise PipelineError("an alignment-review input changed during asset generation; no review index was written")
    index = {
        "schema": "dvd-alignment-review-assets-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code, "series": ep.series, "identity": ep.identity,
        "candidate_is_approval": False,
        "manifest_path": str(manifest_path), "manifest_sha256": inputs["manifest"],
        "alignment_path": str(alignment), "alignment_sha256": inputs["alignment"],
        "diagnostics_path": str(report), "diagnostics_sha256": inputs["diagnostics"],
        "master_source": str(source_master),
        "master_source_sha256": inputs["master_source"],
        "dvd_source": str(source_dvd), "dvd_source_sha256": inputs["dvd_source"],
        "master_descriptors": str(master_desc_path),
        "master_descriptors_sha256": inputs["master_descriptors"],
        "master_descriptors_meta": str(master_meta),
        "master_descriptors_meta_sha256": inputs["master_descriptors_meta"],
        "dvd_descriptors": str(dvd_desc_path),
        "dvd_descriptors_sha256": inputs["dvd_descriptors"],
        "dvd_descriptors_meta": str(dvd_meta),
        "dvd_descriptors_meta_sha256": inputs["dvd_descriptors_meta"],
        "descriptor_summary": {
            "master": {key: master_meta_value.get(key) for key in (
                "frames", "input_fps", "ffprobe_r_frame_rate", "ffprobe_avg_frame_rate",
                "output_grid_fps", "stream_duration_s", "descriptor_coverage_s")},
            "dvd": {key: dvd_meta_value.get(key) for key in (
                "frames", "input_fps", "ffprobe_r_frame_rate", "ffprobe_avg_frame_rate",
                "output_grid_fps", "stream_duration_s", "descriptor_coverage_s")},
        },
        "parameters": {"warn_ncc": warn_ncc, "transition_threshold_frames": transition_threshold,
                       "min_offset_frames": min_offset, "max_offset_frames": max_offset,
                       "sample_step_frames": sample_step,
                       "playback_radius_frames": CLIP_RADIUS_FRAMES,
                       "playback_frame_step": CLIP_STEP_FRAMES},
        "path": rows, "review_points": clips,
        "contact_sheet": str(contact_path) if contact_path else None,
        "contact_sheet_sha256": sha256_file(contact_path) if contact_path else None,
        "deferred": deferred,
        "warning": ("Candidate path only. The short GIFs use the row's local constant offset for display; "
                    "they do not approve/extrapolate the full path or record retime offsets."),
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
    parser.add_argument("--alignment", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--master-desc", required=True)
    parser.add_argument("--dvd-desc", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--warn-ncc", type=float, default=0.50)
    parser.add_argument("--transition-threshold", type=int, default=20)
    parser.add_argument("--max-points", type=int, default=MAX_REVIEW_POINTS)
    parser.add_argument("--min-offset", type=int, default=-3000)
    parser.add_argument("--max-offset", type=int, default=500)
    parser.add_argument("--sample-step", type=int, default=15)
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    index = build_assets(
        ep, Path(args.alignment), Path(args.report), Path(args.master_desc),
        Path(args.dvd_desc), Path(args.out_dir), args.warn_ncc,
        args.transition_threshold, args.max_points, ld,
        args.min_offset, args.max_offset, args.sample_step)
    print(f"alignment review assets: {index}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ALIGNMENT REVIEW ASSETS ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
