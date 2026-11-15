#!/usr/bin/env python3
r"""Emit a motion trace for build_map.ps1 (mean |frame - previous| per frame).

Why this exists
---------------
`build_map.ps1` accepts an optional motion trace produced on Windows by
`motion-probe.ps1`, which is **not shipped in this bundle**. Without it the cadence
report cannot run its native-30p gate and ends with
`VERDICT: motion trace not supplied`. This helper is the reusable, parameterized
replacement: nothing here is episode-specific and no measurement is inferred.

Contract expected by `build_map.ps1`
------------------------------------
* one plain decimal per line, matching ``^\s*([\d.]+)\s*$``
* **N-1 values** for an N-frame clip - tblend drops frame 0, and build_map.ps1
  pads the front with 0.0 automatically when it sees exactly N-1
* the motion probe must run on a *trim-only* script: a script that still contains
  TFM would re-run TFM and overwrite the cadence log

Usage
-----
  python scripts/build_motion_trace.py --source <stack-master.mov> --out <motion.txt>
  python scripts/build_motion_trace.py --episode S01E10          # resolves via manifest

Exit codes: 0 written, 2 refused (missing source / measured value / overwrite).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="stack master (.mov)")
    ap.add_argument("--episode", help="resolve --source from the episode manifest")
    ap.add_argument("--out", required=True, help="motion trace file to write")
    ap.add_argument("--trim-head", type=int, default=0,
                    help="audio alignment offset to drop first, so the trace runs on the "
                         "audio-aligned clip like the TFM log (audio.pad_frames)")
    ap.add_argument("--dry-run", action="store_true", help="print stats, do not write")
    args = ap.parse_args()

    ld = Ld()
    if args.episode:
        ep = load_episode(args.episode, ld)
        source = str(ep.path("stack_master"))
    elif args.source:
        source = str(Path(args.source).resolve())
    else:
        raise PipelineError("supply --source or --episode")

    src_path = Path(source)
    if not src_path.is_file():
        raise PipelineError(f"source not found: {src_path}")
    out = Path(args.out)
    assert_writable_path(out, ld)
    if out.exists():
        raise PipelineError(f"refusing to overwrite existing file: {out}")

    import vapoursynth as vs

    core = vs.core
    # Bounded cache: this walks ~83k frames of a 22 GB master.
    core.max_cache_size = 4096
    src = core.bs.VideoSource(source)
    # Run on the AUDIO-ALIGNED clip (runbook §4: "run the TFM log on the audio-aligned
    # clip, .Trim(pad,0)") so the trace and the cadence log share one frame domain.
    if args.trim_head:
        src = core.std.Trim(src, args.trim_head, None)
        print(f"aligned: dropped first {args.trim_head} frame(s) (audio.pad_frames)")
    total = src.num_frames
    if total < 2:
        raise PipelineError(f"source has only {total} frame(s); need at least 2")

    # PlaneStatsDiff is NORMALISED to [0,1] by the plane's max value, but
    # build_map.ps1 thresholds motion against a pixel-difference scale
    # (`$motMean -ge 15.0`). Measured on this master: raw/PlaneStatsDiff = exactly
    # 1023 = 2**10 - 1. Convert back to raw mean |frame-prev| in the clip's own
    # units so the native-30p gate can actually fire.
    fmt = src.format
    bits = fmt.bits_per_sample if fmt is not None else 10
    scale = float((1 << bits) - 1)

    # NOTE: VapourSynth's std.Trim does NOT accept AviSynth-style negative
    # "count from the end" indices - any negative `last` raises
    # "invalid last frame specified". Use absolute indices instead.
    prev = core.std.Trim(src, 0, total - 2)   # frames 0 .. N-2
    cur = core.std.Trim(src, 1, None)         # frames 1 .. N-1
    stats = core.std.PlaneStats(prev, cur, plane=0)

    print(f"source : {src_path}")
    print(f"frames : {total}  ->  expect {total - 1} motion values")
    print(f"scale  : PlaneStatsDiff x {scale:.0f} (bit depth {bits}) -> raw units")
    started = time.time()
    values: list[str] = []
    peak = 0.0
    for i in range(total - 1):
        f = stats.get_frame(i)
        v = float(f.props.get("PlaneStatsDiff", 0.0)) * scale
        if v > peak:
            peak = v
        values.append(f"{v:.6f}")
        if (i + 1) % 20000 == 0:
            print(f"  ... {i + 1}/{total - 1} "
                  f"({(i + 1) / (time.time() - started):.0f} fps)", flush=True)
    elapsed = time.time() - started

    print(f"done   : {len(values)} values in {elapsed:.1f}s "
          f"({(total - 1) / max(elapsed, 0.001):.0f} fps), peak |diff|={peak:.6f}")
    if args.dry_run:
        print("DRY RUN - not written")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(values) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
