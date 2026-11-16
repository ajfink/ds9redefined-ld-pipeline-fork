#!/usr/bin/env python3
"""Replacement-image identity gate for the delivered pad-fixed encode.

For each sampled PAD frame: the rendered frame's descriptor must match ITS OWN
replacement PNG (NCC >= 0.99) and be clearly lower against every other PAD's
PNG (separation >= 0.10). This is the check that catches a silently drifted
ReplaceFramesSimple layout - a single-pad preview once passed while 43 pads
held the wrong image.

  python scripts\\verify_pad_identities.py --episode S01E05
  python scripts\\verify_pad_identities.py --episode S01E05 --frames 239 --frames 37679

Writes <code>-pad-identity-samples.tsv (merged by episode+frame so a selective
run never clobbers other episodes' rows).
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

FPS = 24000.0 / 1001.0
MIN_OWN = 0.99          # own-image NCC floor
MIN_SEPARATION = 0.10   # own minus best-unrelated NCC
HEADER = ("episode", "frame", "time", "method", "own_ncc", "best_other_ncc",
          "best_other_frame", "separation")


def norm(a):
    import numpy as np
    x = a.astype(np.float32).ravel()
    x -= x.mean()
    n = np.linalg.norm(x)
    return x / n if n else x


def image_desc(path: Path):
    import cv2
    bgr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if bgr is None:
        raise PipelineError(f"replacement image missing/unreadable: {path}")
    small = cv2.resize(bgr, (48, 36), interpolation=cv2.INTER_AREA)
    if small.ndim == 3:
        small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY if small.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
    return norm(small)


def frame_desc(ld: Ld, video: Path, frame: int):
    import numpy as np
    cmd = [ld.ffmpeg(), "-hide_banner", "-v", "error", "-ss", f"{frame / FPS:.9f}",
           "-i", str(video), "-frames:v", "1", "-vf", "scale=48:36:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode or len(p.stdout) != 48 * 36:
        raise PipelineError(f"frame extraction failed at {frame}: "
                            + p.stderr.decode("utf-8", "replace")[-500:])
    return norm(np.frombuffer(p.stdout, np.uint8).copy())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--encode", help="override the delivered pad-fixed file")
    ap.add_argument("--frames", type=int, action="append", default=[],
                    help="sample frame (repeatable); default = first/middle/last of the repair set")
    args = ap.parse_args()

    try:
        import cv2  # noqa: F401
    except Exception as e:
        distribution = ("opencv-python-headless" if sys.platform in ("darwin", "linux")
                        else "opencv-python")
        env_note = (" DYLD_LIBRARY_PATH is set; it can make OpenCV load an incompatible "
                    "native library." if sys.platform == "darwin" and
                    os.environ.get("DYLD_LIBRARY_PATH") else "")
        raise PipelineError(
            f"OpenCV import failed under {sys.executable}; expected the selected "
            f"environment's {distribution} package. {type(e).__name__}: {e}{env_note}"
        ) from e

    ld = Ld()
    ep = load_episode(args.episode, ld)
    probe = ld.probe()
    dkey = ep.data.get("deliverable") or "padfixed"
    video = Path(args.encode) if args.encode else ep.path(dkey)
    repair_file = probe / f"{ep.code}-pad-repair-frames.txt"
    report = probe / "rife-pad-replacements-local.tsv"
    # shared report and legacy PNG dir are keyed by pad_report_label
    label = ep.data.get("pad_report_label") or ep.code
    repdir = probe / "rife-padframes-local" / label / "interpolated"
    for p in (video, repair_file):
        if not p.exists():
            raise PipelineError(f"missing input: {p}")

    candidates = sorted({int(x) for x in repair_file.read_text(encoding="utf-8").split() if x.strip()})
    if not candidates:
        out = probe / f"{ep.code}-pad-identity-samples.tsv"
        if not out.exists():
            with out.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f, delimiter="\t").writerow(HEADER)
        print("PASS - no PAD frames were repaired; identity sampling is not applicable")
        return 0
    if not report.exists():
        raise PipelineError(f"replacement report missing: {report}")
    with report.open(newline="", encoding="utf-8") as f:
        rec = {int(r["frame"]): r for r in csv.DictReader(f, delimiter="\t")
               if r.get("episode") == label}
    missing = [n for n in candidates if n not in rec]
    if missing:
        raise PipelineError(f"{label}: no recorded replacement for frames "
                            f"{missing[:5]}{'...' if len(missing) > 5 else ''} - run rife_padframes.py")
    pool = {}
    for n in candidates:
        recorded_png = Path(rec[n]["png"])
        png = recorded_png if recorded_png.is_absolute() else probe / recorded_png
        # Backward compatibility for older rows that only recorded a filename.
        if not png.exists() and not recorded_png.is_absolute():
            png = repdir / recorded_png.name
        pool[n] = image_desc(png)

    picks = args.frames or sorted({candidates[0], candidates[len(candidates) // 2], candidates[-1]})
    print(f"{label}: {len(candidates)} pads, sampling {picks}")
    rows, failures = [], []
    for frame in picks:
        if frame not in pool:
            raise PipelineError(f"sample {frame} is not a confirmed pad candidate")
        a = frame_desc(ld, video, frame)
        own = float(a @ pool[frame])
        others = [(float(a @ pool[n]), n) for n in pool if n != frame]
        best_other, other_frame = max(others)
        sep = own - best_other
        method = rec[frame]["method"]
        ok = own >= MIN_OWN and sep >= MIN_SEPARATION
        if not ok:
            failures.append(frame)
        print(f"  frame {frame} t={frame/FPS:.3f}s method={method} own={own:.5f} "
              f"best-other={best_other:.5f} (frame {other_frame}) sep={sep:.5f} "
              f"{'PASS' if ok else 'FAIL'}")
        rows.append({"episode": label, "frame": frame, "time": f"{frame/FPS:.6f}",
                     "method": method, "own_ncc": f"{own:.6f}",
                     "best_other_ncc": f"{best_other:.6f}", "best_other_frame": other_frame,
                     "separation": f"{sep:.6f}"})

    out = probe / f"{ep.code}-pad-identity-samples.tsv"
    kept = ld.merge_tsv(out, rows, key_fields=("episode", "frame"), header=HEADER)
    print(f"report: {out} (kept {kept} other-episode rows, wrote {len(rows)})")
    if failures:
        print(f"FAIL - {len(failures)} sample(s) below own>={MIN_OWN} / separation>={MIN_SEPARATION}: "
              f"{failures}", file=sys.stderr)
        return 1
    print(f"PASS - every sample matches its own replacement "
          f"(own>={MIN_OWN}, separation>={MIN_SEPARATION})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
