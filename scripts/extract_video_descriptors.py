#!/usr/bin/env python3
"""Sequentially decode a master/DVD to a 48x36 luma descriptor grid.

DVD PTS can be non-monotonic, so this tool never seeks or rewrites timestamps. It
matches the validated legacy extraction behavior: FFmpeg's default rawvideo output
sync creates the descriptor grid; no `setpts`, `fps` filter, or explicit
`-fps_mode` is used. Do not add those options casually: they changed E05's descriptor
count from the reference 81,654 to 68,555. Check the resulting count and time coverage
before using any map. The common alignment grid is 30000/1001 fps.

  python scripts\\extract_video_descriptors.py --episode S01E06 --source master
  python scripts\\extract_video_descriptors.py --episode S01E06 --source dvd --input-fps 30000/1001

Review the probed `r_frame_rate`/`avg_frame_rate` and verify the output descriptor
count/coverage. The `--input-fps` value records the measured source nominal rate; it
does not alter decoding or output synchronization.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

W, H = 48, 36


def rate(text: str) -> Fraction:
    try:
        v = Fraction(text)
        if v <= 0:
            raise ValueError
        return v
    except (ValueError, ZeroDivisionError) as e:
        raise PipelineError(f"invalid frame rate {text!r}") from e


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--source", choices=("master", "dvd"), required=True)
    ap.add_argument("--input-fps", help="measured/verified decoded-frame rate; default uses FFprobe r_frame_rate")
    ap.add_argument("--crop", help="optional FFmpeg crop filter, e.g. crop=720:480:0:0")
    ap.add_argument("--out", help="override descriptor .npy path")
    ap.add_argument("--force", action="store_true", help="replace existing descriptor and metadata after preserving them")
    args = ap.parse_args()

    import numpy as np
    ld = Ld()
    ep = load_episode(args.episode, ld)
    source_key = "stack_master" if args.source == "master" else "dvd"
    video = ep.path(source_key)
    if not video.is_file():
        raise PipelineError(f"input video missing: {video}")

    probe = subprocess.run([ld.ffprobe(), "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate,avg_frame_rate,width,height,duration,nb_frames",
                            "-of", "json", str(video)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode:
        raise PipelineError(f"ffprobe failed on {video}: {probe.stderr[-500:]}")
    streams = json.loads(probe.stdout or "{}").get("streams", [])
    if len(streams) != 1:
        raise PipelineError(f"could not identify one video stream in {video}")
    stream = streams[0]
    input_rate = rate(args.input_fps or stream.get("r_frame_rate", ""))
    avg_rate = stream.get("avg_frame_rate")
    if not args.input_fps and avg_rate and avg_rate != stream.get("r_frame_rate"):
        print(f"WARN: r_frame_rate={stream.get('r_frame_rate')} differs from avg_frame_rate={avg_rate}; "
              "review the DVD frame cadence. --input-fps only records a verified source rate; it does not change output sync.", file=sys.stderr)

    if args.out:
        out = Path(args.out)
    else:
        key = "master_descriptors" if args.source == "master" else "dvd_descriptors"
        try:
            out = ep.path(key)
        except PipelineError:
            out = ld.probe() / f"{ep.code}-desc-{args.source}.npy"
    meta = out.with_suffix(out.suffix + ".meta.json")
    existing = [p for p in (out, meta) if p.exists()]
    if existing and not args.force:
        raise PipelineError(f"refusing to overwrite descriptor artifacts: {out} / {meta}")
    if existing:
        backups = [p.with_name(p.name + ".bak") for p in existing]
        collision = [p for p in backups if p.exists()]
        if collision:
            raise PipelineError(f"refusing to overwrite existing descriptor backups: {collision}")
        for src, backup in zip(existing, backups):
            shutil.copy2(src, backup)
            print(f"preserved prior artifact: {backup}")

    filters = []
    if args.crop:
        filters.append(args.crop)
    filters.extend((f"scale={W}:{H}:flags=area", "format=gray"))
    vf = ",".join(filters)
    cmd = [ld.ffmpeg(), "-hide_banner", "-v", "error", "-i", str(video), "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    grid_rate = rate(stream.get("r_frame_rate", ""))
    print(f"source={video} nominal_input_fps={input_rate} expected_descriptor_grid_fps={grid_rate}")
    print(f"filter={vf or '<none>'}; sequential decode using FFmpeg default rawvideo output sync (no -ss, setpts, fps filter, or explicit -fps_mode)", flush=True)
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode:
        raise PipelineError(f"FFmpeg descriptor decode failed ({p.returncode}): {p.stderr.decode('utf-8','replace')[-1000:]}")
    stride = W * H
    if len(p.stdout) % stride:
        raise PipelineError(f"partial descriptor frame: {len(p.stdout)} bytes is not divisible by {stride}")
    count = len(p.stdout) // stride
    if count < 1000:
        raise PipelineError(f"descriptor output is unexpectedly short: {count} frames")
    coverage_s = count / float(grid_rate)
    descriptors = np.frombuffer(p.stdout, dtype=np.uint8).reshape(count, stride).copy()
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, descriptors)
    stat = video.stat()
    metadata = {
        "episode": ep.code, "role": args.source, "input_path": str(video.resolve()),
        "input_size": stat.st_size, "input_mtime_ns": stat.st_mtime_ns,
        "input_fps": str(input_rate), "ffprobe_r_frame_rate": stream.get("r_frame_rate"),
        "ffprobe_avg_frame_rate": avg_rate, "output_grid_fps": str(grid_rate),
        "stream_duration_s": stream.get("duration"), "stream_nb_frames": stream.get("nb_frames"),
        "descriptor_coverage_s": coverage_s,
        "crop": args.crop or "none", "descriptor_size": [W, H], "frames": count,
    }
    meta.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"saved {count} descriptors ({W}x{H}), nominal coverage={coverage_s:.3f}s at {grid_rate} fps; "
          f"stream duration={stream.get('duration')}s; metadata {meta}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
