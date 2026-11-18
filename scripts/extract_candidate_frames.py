#!/usr/bin/env python3
"""Safely extract an explicit, bounded set of source-derived frames as PNGs.

This helper is for frame inspection/cut classification, not PAD measurement or
repair. Pass exact post-IVTC output-frame indices; it never decodes one broad
Trim(min_frame, max_frame) span to PNG. Each FFmpeg selection expression is
limited to 32 indices and output is capped at 128 frames per invocation.

    python scripts/extract_candidate_frames.py --episode S01E09 --frames-file frames.txt
    python scripts/extract_candidate_frames.py --episode S01E09 --frames 3818 --frames 61421

Writes a new directory under the probe root (or an explicit --out-dir), refuses
existing output, and records exact indices, sizes, and hashes in frames.tsv.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import (Ld, PipelineError, assert_writable_path, load_episode,
                      redact_shared_text)  # noqa: E402
from fileutil import sha256_file  # noqa: E402
from vspipe_util import run_source_ffmpeg  # noqa: E402

WIDTH, HEIGHT = 768, 484
MAX_FRAMES_PER_RUN = 128
SELECT_TERMS_PER_CALL = 32
RGB24_FRAME_BYTES = WIDTH * HEIGHT * 3


def parse_frame_numbers(values: list[int], frame_file: Path | None,
                        frame_count: int, max_frames: int = MAX_FRAMES_PER_RUN) -> list[int]:
    if (values and frame_file) or (not values and not frame_file):
        raise PipelineError("supply exactly one of --frames or --frames-file")
    if frame_file:
        if not frame_file.is_file():
            raise PipelineError(f"frame list not found: {frame_file}")
        tokens = []
        for line in frame_file.read_text(encoding="utf-8").splitlines():
            tokens.extend(line.split("#", 1)[0].split())
        try:
            numbers = [int(token) for token in tokens]
        except ValueError as exc:
            raise PipelineError(f"frame list contains a non-integer index: {frame_file}") from exc
    else:
        numbers = list(values)
    if not numbers:
        raise PipelineError("frame list is empty")
    if len(numbers) > max_frames or len(numbers) > MAX_FRAMES_PER_RUN:
        raise PipelineError(
            f"requested {len(numbers)} frames; maximum per run is "
            f"{min(max_frames, MAX_FRAMES_PER_RUN)}; split the explicit index list")
    if len(set(numbers)) != len(numbers):
        raise PipelineError("frame list contains duplicate indices")
    if any(frame < 0 or frame >= frame_count for frame in numbers):
        raise PipelineError(f"frame indices must be within 0..{frame_count - 1}")
    return sorted(numbers)


def selection_filter(frames: list[int]) -> str:
    if not frames or len(frames) > SELECT_TERMS_PER_CALL:
        raise PipelineError(f"one FFmpeg select call must contain 1..{SELECT_TERMS_PER_CALL} indices")
    expression = "+".join(f"eq(n,{frame})" for frame in frames)
    return f"select='{expression}',format=rgb24"


def frame_chunks(frames: list[int], size: int = SELECT_TERMS_PER_CALL) -> list[list[int]]:
    if size <= 0 or size > SELECT_TERMS_PER_CALL:
        raise PipelineError(f"chunk size must be 1..{SELECT_TERMS_PER_CALL}")
    return [frames[i:i + size] for i in range(0, len(frames), size)]


file_sha256 = sha256_file


def extract_frames(ld: Ld, video: Path, frames: list[int], out_dir: Path) -> Path:
    """Run bounded exact-index selections and publish a no-overwrite extraction set."""
    out_dir = assert_writable_path(out_dir, ld)
    if out_dir.exists():
        raise PipelineError(f"refusing to overwrite existing candidate-frame directory: {out_dir}")
    if video.suffix.lower() not in (".avs", ".vpy"):
        raise PipelineError(f"candidate frames must come from a source-derived .avs/.vpy: {video}")
    if not video.is_file():
        raise PipelineError(f"source-derived video missing: {video}")

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    reserve_gib = float((ld.profile.get("limits") or {}).get("min_free_gb", 0) or 0)
    worst_case = len(frames) * RGB24_FRAME_BYTES
    free = shutil.disk_usage(out_dir.parent).free
    required = worst_case + int(reserve_gib * 1024 ** 3)
    if free < required:
        raise PipelineError(
            f"candidate-frame space gate FAIL: free={free / 1024**3:.2f} GiB; "
            f"worst-case PNG bytes={worst_case}; configured reserve={reserve_gib:.2f} GiB")

    out_dir.mkdir()
    rows = []
    for chunk_index, part in enumerate(frame_chunks(frames), 1):
        with tempfile.TemporaryDirectory(prefix=f"candidate-frame-{chunk_index}-",
                                         dir=str(out_dir)) as temp:
            temp_dir = Path(temp)
            pattern = temp_dir / "selected_%08d.png"
            command = [ld.ffmpeg(), "-y", "-hide_banner", "-v", "error"]
            if video.suffix.lower() == ".avs":
                command += ["-f", "avisynth", "-i", str(video)]
            else:
                command += ["-f", "yuv4mpegpipe", "-i", "-"]
            command += ["-vf", selection_filter(part), "-fps_mode", "passthrough",
                        "-frames:v", str(len(part)), "-start_number", "0", str(pattern)]
            result = run_source_ffmpeg(ld, command, video, text=True)
            if result.returncode:
                raise PipelineError(
                    f"exact candidate-frame extraction failed for {part}: "
                    f"{(result.stderr or '')[-1000:]}")
            generated = sorted(temp_dir.glob("selected_*.png"))
            if len(generated) != len(part):
                raise PipelineError(f"select requested {len(part)} frames but produced {len(generated)}")
            try:
                from PIL import Image
            except ImportError as exc:
                raise PipelineError("Pillow is required to verify extracted PNG dimensions") from exc
            for frame, png in zip(part, generated):
                with Image.open(png) as image:
                    if image.size != (WIDTH, HEIGHT):
                        raise PipelineError(f"frame {frame} extracted at {image.size}, expected {(WIDTH, HEIGHT)}")
                destination = out_dir / f"frame_{frame:08d}.png"
                if destination.exists():
                    raise PipelineError(f"refusing to overwrite extracted frame: {destination}")
                png.replace(destination)
                rows.append((frame, destination.name, destination.stat().st_size,
                             file_sha256(destination)))

    manifest = out_dir / "frames.tsv"
    with manifest.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(("output_frame", "filename", "size_bytes", "sha256"))
        writer.writerows(sorted(rows))
    print(f"extracted {len(rows)} exact frames to {redact_shared_text(str(out_dir), ld)}")
    print(f"index/hash manifest: {redact_shared_text(str(manifest), ld)}")
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--frames", type=int, action="append", help="exact output frame index; repeatable")
    group.add_argument("--frames-file", help="whitespace-delimited exact frame indices")
    parser.add_argument("--max-frames", type=int, default=MAX_FRAMES_PER_RUN,
                        help=f"lower per-run cap (hard maximum {MAX_FRAMES_PER_RUN})")
    parser.add_argument("--out-dir", help="new output directory; default is episode-scoped probe folder")
    args = parser.parse_args()
    if not 1 <= args.max_frames <= MAX_FRAMES_PER_RUN:
        raise PipelineError(f"--max-frames must be 1..{MAX_FRAMES_PER_RUN}")

    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.frame_count is None or int(ep.frame_count) <= 0:
        raise PipelineError("measured video.frames is required before extracting candidate frames")
    video = ep.path("avs_encode")
    frame_file = Path(args.frames_file).expanduser() if args.frames_file else None
    frames = parse_frame_numbers(args.frames or [], frame_file, ep.frame_count,
                                 max_frames=args.max_frames)
    output = (Path(args.out_dir).expanduser() if args.out_dir else
              ld.probe() / f"{ep.code}-candidate-frames")
    extract_frames(ld, video, frames, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
