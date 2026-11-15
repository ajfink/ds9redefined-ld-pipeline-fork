#!/usr/bin/env python3
"""Full-raster RGB check for a repair set.

48x36 luma max==0 is evidence, not proof: a frame can carry chroma with Y=0.
Decode every candidate at full resolution as RGB24 and require near-black.

  python scripts\\fullres_check.py --episode S01E05

Writes <code>-pad-fullres.tsv with columns frame, rgb_max, rgb_mean.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # sibling modules (vspipe_util)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ldcommon
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402
from vspipe_util import run_source_ffmpeg  # noqa: E402

W, H = 768, 484
CHUNK = 27          # keep select='eq(...)' well below the 32-term expression limit
MAX_RGB = 2         # HEVC may leave a couple of codes on an otherwise black frame
MAX_MEAN = 0.0005


def load_frames(ld: Ld, video: Path, frames: list[int]) -> dict[int, tuple[int, float]]:
    import numpy as np
    out: dict[int, tuple[int, float]] = {}
    for start in range(0, len(frames), CHUNK):
        part = frames[start:start + CHUNK]
        # n is counted from the first frame FFmpeg sees. On macOS that is the
        # first frame vspipe delivers, and vspipe is asked for the whole chain,
        # so n still equals the script's absolute frame index - no offset.
        expr = "+".join(f"eq(n,{f})" for f in part)
        cmd = [ld.ffmpeg(), "-hide_banner", "-v", "error"]
        if video.suffix.lower() == ".avs":
            cmd += ["-f", "avisynth", "-i", str(video)]
        else:
            cmd += ["-f", "yuv4mpegpipe", "-i", "-"]
        cmd += ["-vf", f"select='{expr}',format=rgb24",
                "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        p = run_source_ffmpeg(ld, cmd, video, text=False)
        if p.returncode:
            raise PipelineError(f"RGB extraction failed: {p.stderr.decode('utf-8', 'replace')[-800:]}")
        stride = W * H * 3
        n = len(p.stdout) // stride
        if n != len(part):
            raise PipelineError(f"chunk {start}: got {n} frames, expected {len(part)}")
        a = np.frombuffer(p.stdout[:n * stride], np.uint8).reshape(n, H, W, 3)
        for f, fr in zip(part, a):
            out[int(f)] = (int(fr.max()), float(fr.mean()))
        print(f"  {start + len(part)}/{len(frames)}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--list", help="frame list file (default: <code>-pad-repair-frames.txt)")
    ap.add_argument("--encode", help="override source-derived video path (defaults to avs_encode)")
    args = ap.parse_args()

    import numpy as np
    ld = Ld()
    ep = load_episode(args.episode, ld)
    probe = ld.probe()
    video = Path(args.encode) if args.encode else ep.path("avs_encode")
    list_path = Path(args.list) if args.list else probe / f"{ep.code}-pad-repair-frames.txt"
    if not list_path.exists():
        raise PipelineError(f"frame list missing: {list_path} - run pad_audit.py first")
    if not video.exists():
        raise PipelineError(f"source-derived video missing: {video}")
    if video.suffix.lower() not in (".avs", ".vpy"):
        raise PipelineError(
            f"full-raster PAD check must read the source-derived script "
            f"(.avs/.vpy), not encoded video: {video}")

    frames = [int(x) for x in list_path.read_text(encoding="utf-8").split() if x.strip()]
    if not frames:
        out = probe / f"{ep.code}-pad-fullres.tsv"
        with out.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, delimiter="\t").writerow(("frame", "rgb_max", "rgb_mean"))
        print(f"PASS - no confirmed PAD repair frames; wrote empty evidence table: {out}")
        return 0
    print(f"{ep.code}: checking {len(frames)} candidates at full raster {W}x{H}", flush=True)
    res = load_frames(ld, video, frames)

    bad = {f: v for f, v in res.items() if v[0] > MAX_RGB or v[1] > MAX_MEAN}
    mx = max(v[0] for v in res.values())
    mm = max(v[1] for v in res.values())
    print(f"full-raster: {len(res)} frames, observed max rgb_max={mx}, max rgb_mean={mm:.9f}, "
          f"failures={len(bad)}")
    out = probe / f"{ep.code}-pad-fullres.tsv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(("frame", "rgb_max", "rgb_mean"))
        for f in sorted(res):
            w.writerow((f, res[f][0], f"{res[f][1]:.9f}"))
    if bad:
        print("FAIL - these are not near-black; do not build replacements for them:", file=sys.stderr)
        for f, v in sorted(bad.items()):
            print(f"  frame {f}: rgb_max={v[0]} rgb_mean={v[1]:.6f}", file=sys.stderr)
        return 1
    print(f"PASS - all {len(res)} candidates are near-black. report: {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
