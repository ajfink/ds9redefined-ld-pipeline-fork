#!/usr/bin/env python3
"""Between-track sync gate: retimed DVD 5.1 vs LD stereo on a delivered file.

Positive lag = LD event later than the 5.1 event (5.1 leads by lag).
A constant offset between the two tracks is expected - each has its own fade
relationship to video - so only drift is a defect.

  python scripts\\sync_check.py --episode S01E05

Sample windows (60 s) are placed at the midpoints of the output timeline's
content segments, automatically avoiding the act-break gaps so no inserted
silence enters a correlation window.

Gates: rms <= 40 ms, |slope| <= 30 ppm.
Writes <code>-sync-check.tsv.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

SR = 48000
HOP = 0.005
WIN = 60.0
MAX_WINDOWS = 7
GATE_RMS_MS, GATE_SLOPE_PPM = 40.0, 30.0


def grab(ld: Ld, path: Path, selector: str, t: float):
    import numpy as np
    cmd = [ld.ffmpeg(), "-hide_banner", "-v", "error", "-ss", f"{t:.3f}", "-t", f"{WIN}",
           "-i", str(path), "-map", selector, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode:
        raise PipelineError(f"audio grab failed {path} {selector}@{t}: "
                            + p.stderr.decode("utf-8", "replace")[:600])
    return np.frombuffer(p.stdout, np.int16).astype(np.float32)


def env(x):
    import numpy as np
    n = int(SR * HOP)
    m = len(x) // n
    if m < 100:
        raise PipelineError(f"window too short ({len(x)} samples)")
    return np.sqrt((x[: m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)


def sample_points(ep) -> list[float]:
    """Midpoints between act-break gaps on the output timeline."""
    delta = ep.delta_s
    gaps = sorted((b["ld_black"][0] - delta, b["ld_black"][1] - delta)
                  for b in (ep.data.get("retime", {}).get("breaks") or [])
                  if b.get("ld_black"))
    dur = ep.duration_s
    segs = []
    cursor = 0.0
    for g0, g1 in gaps:
        if g0 - cursor > 2 * WIN + 10:
            segs.append((cursor, g0))
        cursor = g1
    if dur - cursor > 2 * WIN + 10:
        segs.append((cursor, dur))
    pts = [(lo + hi) / 2 for lo, hi in segs if hi - lo >= WIN + 10]
    if not pts:
        raise PipelineError(f"{ep.code}: no clear segment long enough for a {WIN}s window")
    if len(pts) > MAX_WINDOWS:
        step = len(pts) / MAX_WINDOWS
        pts = [pts[int(i * step)] for i in range(MAX_WINDOWS)]
    return sorted(p for p in pts if 0 <= p <= dur - WIN)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--video", help="override the delivered file")
    ap.add_argument("--points", type=float, nargs="+")
    args = ap.parse_args()

    import numpy as np
    ld = Ld()
    ep = load_episode(args.episode, ld)
    dkey = ep.data.get("deliverable") or "pre_upscale"
    video = Path(args.video) if args.video else ep.path(dkey)
    if not video.exists():
        raise PipelineError(f"missing deliverable: {video}")
    # the sync gate runs on the delivered file, so it follows deliverable_tracks
    tracks = list(ep.data.get("deliverable_tracks") or ep.data.get("mux_tracks") or [])
    if "51" not in tracks:
        print(f"{ep.code} has no DVD 5.1 track ({tracks}) - between-track check not applicable")
        return 0
    pts = args.points or sample_points(ep)
    print(f"sample points (60 s windows, away from act breaks): {pts}")

    pts_out = []
    for t in pts:
        a = env(grab(ld, video, "0:a:0", t))
        b = env(grab(ld, video, "0:a:1", t))
        n = min(len(a), len(b))
        a, b = a[:n], b[:n]
        a = (a - a.mean()) / a.std()
        b = (b - b.mean()) / b.std()
        maxlag = int(1.0 / HOP)
        corr = np.empty(2 * maxlag + 1)
        base_b = b[maxlag:n - maxlag]
        for j, l in enumerate(range(-maxlag, maxlag + 1)):
            corr[j] = float(np.mean(a[maxlag + l:n - maxlag + l] * base_b))
        k = int(np.argmax(corr))
        if 0 < k < len(corr) - 1:
            y0, y1, y2 = corr[k - 1], corr[k], corr[k + 1]
            den = y0 - 2 * y1 + y2
            off = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        else:
            off = 0.0
        lag = (k - maxlag + off) * HOP
        pts_out.append((t, lag, float(corr[k])))
        print(f"  t={t:7.1f}s  lag {lag*1000:+7.1f} ms  corr {corr[k]:.3f}", flush=True)

    x = np.array([p[0] for p in pts_out])
    y = np.array([p[1] for p in pts_out])
    slope, const = np.polyfit(x, y, 1)
    res = y - (slope * x + const)
    rms = float(res.std(ddof=2)) if len(y) > 2 else 0.0
    ppm = slope * 1e6
    out = ld.probe() / f"{ep.code}-sync-check-{Path(video).stem}.tsv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(("time_s", "lag_s", "corr"))
        for t, lag, c in pts_out:
            w.writerow((f"{t:.3f}", f"{lag:.6f}", f"{c:.6f}"))
    print(f"\nfit: lag(t) = {const*1000:+.1f} ms + {ppm:+.1f} ppm * t  "
          f"rms {rms*1000:.1f} ms  range {y.min()*1000:+.0f}..{y.max()*1000:+.0f} ms")
    print(f"report: {out}")
    ok = rms * 1000 <= GATE_RMS_MS and abs(ppm) <= GATE_SLOPE_PPM
    print(f"GATES rms<={GATE_RMS_MS:.0f}ms: {rms*1000 <= GATE_RMS_MS}  "
          f"|slope|<={GATE_SLOPE_PPM:.0f}ppm: {abs(ppm) <= GATE_SLOPE_PPM}")
    if not ok:
        print("FAIL - between-track drift outside gate", file=sys.stderr)
        return 1
    print("PASS - between-track offset is constant (drift is the defect, not offset)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
