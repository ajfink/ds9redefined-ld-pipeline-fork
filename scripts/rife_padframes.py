#!/usr/bin/env python3
"""Generate CPU/TTA RIFE midpoint PNGs for one episode's confirmed PAD blips.

  python scripts\\rife_padframes.py --episode S01E05
  python scripts\\rife_padframes.py --episode S01E05 --hold-cut 26160

Inputs (profile probe root):  <code>-pad-audit.tsv, <code>-pad-repair-frames.txt,
                              <code>-pad-fullres.tsv
Outputs: probe/rife-padframes/<code>/{neighbors,interpolated}/*.png
         probe/rife-pad-replacements-local.tsv  (merged by episode+frame)

The full-raster check is mandatory: a candidate that is not near-black aborts
the run rather than being silently skipped, and a selective run never clobbers
other episodes' rows in the shared report.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

FPS = 24000.0 / 1001.0
REPORT_HEADER = ("episode", "frame", "time", "method", "motion_score",
                 "fullres_rgb_max", "fullres_rgb_mean", "seconds", "png")
MAX_RGB, MAX_MEAN = 2, 0.0005


def read_candidates(audit: Path, repair_file: Path, fullres: Path,
                    accept: frozenset[int] = frozenset()) -> list[dict]:
    if not audit.exists():
        raise PipelineError(f"audit missing: {audit} - run pad_audit.py first")
    if not repair_file.exists():
        raise PipelineError(f"confirmed PAD repair list missing: {repair_file} - run pad_audit.py first")
    if not fullres.exists():
        raise PipelineError(f"full-raster report missing: {fullres} - run fullres_check.py first")
    with audit.open(newline="", encoding="utf-8") as f:
        rows = {int(r["frame"]): r for r in csv.DictReader(f, delimiter="\t")}
    with fullres.open(newline="", encoding="utf-8") as f:
        checked = {int(r["frame"]): (int(r["rgb_max"]), float(r["rgb_mean"]))
                   for r in csv.DictReader(f, delimiter="\t")}
    wanted = sorted({int(x) for x in repair_file.read_text(encoding="utf-8").split() if x.strip()})
    out = []
    for frame in wanted:
        row = rows.get(frame)
        if row is None or int(row.get("is_predicted_pad", 0)) != 1:
            raise PipelineError(f"repair frame {frame} is not a PAD-map-matched audit row")
        if frame not in checked:
            raise PipelineError(f"candidate {frame} has no full-raster check - "
                                f"run fullres_check.py before generating replacements")
        maxv, meanv = checked[frame]
        if (maxv > MAX_RGB or meanv > MAX_MEAN) and frame not in accept:
            raise PipelineError(f"candidate {frame} is not near-black: "
                                f"rgb_max={maxv} rgb_mean={meanv} "
                                f"(classify per runbook section 6 step 2, then pass "
                                f"--accept-residual {frame} if it is a PAD)")
        out.append({"frame": frame, "time": float(row["time"]),
                    "motion": float(row.get("motion_score", 0) or 0),
                    "rgb_max": maxv, "rgb_mean": meanv})
    return sorted(out, key=lambda r: r["frame"])


def extract_neighbors(ld: Ld, video: Path, frame: int, inp: Path) -> tuple[Path, Path]:
    """Seek to frame-1 and keep frames 0 and 2 (skipping the black pad itself)."""
    prev = inp / f"prev_{frame:06d}.png"
    nxt = inp / f"next_{frame:06d}.png"
    seek = (frame - 1) / FPS
    vf = "select='eq(n,0)+eq(n,2)',format=rgb24"
    cmd = [ld.ffmpeg(), "-y", "-hide_banner", "-v", "error"]
    if video.suffix.lower() == ".avs":
        cmd += ["-f", "avisynth"]
    cmd += ["-ss", f"{seek:.9f}", "-i", str(video), "-vf", vf,
            "-fps_mode", "passthrough", "-frames:v", "2",
            "-start_number", "1", str(inp / f"extract_{frame}_%02d.png")]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode:
        raise PipelineError(f"neighbor extraction failed at frame {frame}: "
                            + p.stderr.decode("utf-8", "replace")[-1500:])
    a, b = inp / f"extract_{frame}_01.png", inp / f"extract_{frame}_02.png"
    if not a.exists() or not b.exists():
        raise PipelineError(f"seek at frame {frame} did not yield two neighbors")
    a.replace(prev)
    b.replace(nxt)
    return prev, nxt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--encode", help="override source-derived video path (defaults to avs_encode)")
    ap.add_argument("--hold-cut", type=int, action="append", default=[],
                    help="frame to hold instead of interpolating (pad sits on a shot cut); repeatable")
    ap.add_argument("--accept-residual", type=int, action="append", default=[],
                    help="frame explicitly classified as a PAD despite full-raster residue above the "
                         "near-black threshold (record the section 6 step 2 evidence first; the "
                         "measured rgb_max/rgb_mean stay in the replacement report); repeatable")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    probe = ld.probe()
    video = Path(args.encode) if args.encode else ep.path("avs_encode")
    if not video.exists():
        raise PipelineError(f"source-derived video missing: {video}")
    if video.suffix.lower() != ".avs":
        raise PipelineError(f"replacement neighbors must come from the source-derived .avs, not encoded video: {video}")
    repair_file = probe / f"{ep.code}-pad-repair-frames.txt"
    cands = read_candidates(probe / f"{ep.code}-pad-audit.tsv", repair_file,
                            probe / f"{ep.code}-pad-fullres.tsv",
                            accept=frozenset(args.accept_residual))
    # shared report/legacy dirs are keyed by pad_report_label so historical rows match
    label = ep.data.get("pad_report_label") or ep.code
    hold = set(args.hold_cut)
    unknown = hold - {c["frame"] for c in cands}
    if unknown:
        raise PipelineError(f"--hold-cut frames are not confirmed pads: {sorted(unknown)}")
    unknown_acc = set(args.accept_residual) - {c["frame"] for c in cands}
    if unknown_acc:
        raise PipelineError(f"--accept-residual frames are not candidates: {sorted(unknown_acc)}")

    work = probe / "rife-padframes-local" / label
    inp, outd = work / "neighbors", work / "interpolated"
    for d in (inp, outd):
        d.mkdir(parents=True, exist_ok=True)
    print(f"{label}: candidates={len(cands)} cut-hold={len(hold)} "
          f"CPU-RIFE={len(cands)-len(hold)}  engine={engine}", flush=True)
    if not cands:
        print("No confirmed isolated PADs; build_padfix_avs.py will create an identity AVS wrapper.")
        return 0
    if any(c["frame"] not in hold for c in cands):
        try:
            engine = str(ld.tool("rife_engine"))
            model = str(ld.tool("rife_model_dir"))
        except PipelineError as e:
            raise PipelineError(f"{e}\nRIFE is required to fill non-cut PADs.")
    else:
        engine, model = "not-used", "not-used"
    model_name = Path(model).name
    version = model_name.removeprefix("rife-v")
    method_name = f"RIFE-{version}-CPU-TTA"
    print(f"  model={model}", flush=True)

    rows, logpath = [], work / "rife-cpu-tta.log"
    with logpath.open("w", encoding="utf-8") as log:
        log.write(f"case={ep.code} engine={engine} model={model} step=0.5 "
                  "spatial_tta=true temporal_tta=true\n")
        for j, c in enumerate(cands, 1):
            frame = c["frame"]
            dst = outd / f"pad_{frame:06d}.png"
            prev, nxt = extract_neighbors(ld, video, frame, inp)
            t0 = time.time()
            if frame in hold:
                dst.write_bytes(prev.read_bytes())
                method, elapsed, err = "hold-prev-scene-cut", 0.0, ""
            else:
                p = subprocess.run([engine, "-0", str(prev), "-1", str(nxt), "-o", str(dst),
                                    "-s", "0.5", "-g", "-1", "-x", "-z", "-m", model,
                                    "-j", "1:4:1"], capture_output=True)
                elapsed = time.time() - t0
                err = (p.stdout + p.stderr).decode("utf-8", "replace")
                if p.returncode or not dst.exists():
                    log.write(f"ERROR frame={frame} rc={p.returncode}\n{err}\n")
                    log.flush()
                    raise PipelineError(f"RIFE failed at frame {frame}: {err[-400:]}")
                method = method_name
            rows.append({"episode": label, "frame": frame, "time": f"{frame/FPS:.6f}",
                         "method": method, "motion_score": f"{c['motion']:.6f}",
                         "fullres_rgb_max": c["rgb_max"],
                         "fullres_rgb_mean": f"{c['rgb_mean']:.9f}",
                         "seconds": f"{elapsed:.3f}",
                         # Keep shared TSVs portable when the probe tree is copied.
                         "png": str(dst.relative_to(probe))})
            log.write(f"{j}/{len(cands)} frame={frame} time={frame/FPS:.6f} method={method} "
                      f"motion={c['motion']:.6f} max={c['rgb_max']} "
                      f"mean={c['rgb_mean']:.9f} elapsed={elapsed:.3f}s file={dst}\n")
            log.flush()
            if j % 10 == 0 or j == len(cands):
                print(f"  {ep.code}: {j}/{len(cands)}; last frame {frame}; {elapsed:.1f}s", flush=True)

    report = probe / "rife-pad-replacements-local.tsv"
    kept = ld.merge_tsv(report, rows, key_fields=("episode", "frame"), header=REPORT_HEADER)
    print(f"  log: {logpath}")
    print(f"  report: {report} (kept {kept} other-episode rows, wrote {len(rows)})")
    print("next: python scripts\\build_padfix_avs.py --episode", ep.code)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
