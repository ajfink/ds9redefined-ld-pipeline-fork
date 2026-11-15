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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # sibling modules (vspipe_util)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ldcommon
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402
from vspipe_util import run_source_ffmpeg  # noqa: E402

FPS = 24000.0 / 1001.0
REPORT_HEADER = ("episode", "frame", "time", "method", "motion_score",
                 "fullres_rgb_max", "fullres_rgb_mean", "seconds", "png",
                 "source_dvd_timestamp", "source_dvd_frame", "source_png")
MAX_RGB, MAX_MEAN = 2, 0.0005
DVD_IMAGE_RE = re.compile(r"^DVD-(\d{2})-(\d{2})-(\d{2})\.(\d{3})-f(\d{8})\.png$", re.IGNORECASE)


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


def parse_shared_cut_image(spec: str, episode: str, bundle: Path) -> tuple[int, Path, str, int]:
    """Validate FRAME=PNG against the curated episode folder and return its DVD timestamp."""
    if "=" not in spec:
        raise PipelineError(f"--cut-pad-image must be FRAME=PNG, got {spec!r}")
    frame_s, path_s = spec.split("=", 1)
    try:
        frame = int(frame_s)
    except ValueError as exc:
        raise PipelineError(f"cut-pad output frame must be an integer: {frame_s!r}") from exc
    source_png = Path(path_s).expanduser().resolve()
    image_root = (bundle / "Cut Pad Images" / episode).resolve()
    active_cut_root = os.environ.get("LDP_CUT_PAD_IMAGES")
    active_cut_root = Path(active_cut_root).expanduser().resolve() if active_cut_root else None
    allowed = source_png.is_relative_to(image_root)
    if not allowed and active_cut_root:
        allowed = source_png.is_relative_to(active_cut_root)
    if not allowed:
        device_root = (bundle / "devices").resolve()
        try:
            relative = source_png.relative_to(device_root)
        except ValueError:
            relative = None
        # devices/<device-instance>/episodes/<episode>/cut-pad-images/<png>
        allowed = bool(relative and len(relative.parts) == 5 and
                       relative.parts[1] == "episodes" and
                       relative.parts[2].casefold() == episode.casefold() and
                       relative.parts[3] == "cut-pad-images")
    if not allowed:
        raise PipelineError(f"shared cut PAD image must come from curated {image_root} or a device-instance cut-pad-images/{episode} folder: {source_png}")
    match = DVD_IMAGE_RE.fullmatch(source_png.name)
    if not match:
        raise PipelineError(f"shared image filename must contain the DVD timestamp and decoded frame: {source_png.name}")
    instance_image = False
    for instance_root in ((bundle / "devices").resolve(), active_cut_root):
        if instance_root is None:
            continue
        try:
            source_png.relative_to(instance_root)
            instance_image = True
            break
        except ValueError:
            continue
    if instance_image:
        sidecar = source_png.with_suffix(".provenance.json")
        if not sidecar.is_file():
            raise PipelineError(f"per-instance cut-PAD image requires its provenance sidecar: {sidecar}")
        try:
            provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(f"invalid cut-PAD provenance sidecar {sidecar}: {exc}") from exc
        digest = hashlib.sha256(source_png.read_bytes()).hexdigest()
        try:
            provenance_frame = int(provenance.get("output_pad_frame", -1))
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"cut-PAD provenance has invalid output_pad_frame: {sidecar}") from exc
        if (str(provenance.get("episode", "")).casefold() != episode.casefold() or
                provenance_frame != frame or
                provenance.get("reviewed_episode_scene_and_pad_mapping") is not True or
                provenance.get("png_sha256") != digest or
                provenance.get("dvd_timestamp") !=
                f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}" or
                not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_dvd_sha256", "")))):
            raise PipelineError(f"cut-PAD provenance does not verify image/frame/mapping: {sidecar}")
    timestamp = f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}"
    return frame, source_png, timestamp, int(match.group(5))


def extract_neighbors(ld: Ld, video: Path, frame: int, inp: Path) -> tuple[Path, Path]:
    """Take frames ``frame-1`` and ``frame+1``, skipping the black pad itself.

    Windows seeks by time (``-ss``) and keeps ``select`` n=0 and n=2. A y4m
    pipe cannot be seeked, so on macOS vspipe selects the exact frame range
    with ``-s``/``-e`` instead - frame indices are exact where a time-based
    seek is only as accurate as the PTS - and the *same* select expression
    then picks the two neighbours out of the three delivered.
    """
    if frame < 1:
        raise PipelineError(f"cannot take a previous neighbour for frame {frame}")
    prev = inp / f"prev_{frame:06d}.png"
    nxt = inp / f"next_{frame:06d}.png"
    vf = "select='eq(n,0)+eq(n,2)',format=rgb24"
    cmd = [ld.ffmpeg(), "-y", "-hide_banner", "-v", "error"]
    vs_args = None
    if video.suffix.lower() == ".avs":
        cmd += ["-f", "avisynth", "-ss", f"{(frame - 1) / FPS:.9f}",
                "-i", str(video)]
    else:
        cmd += ["-f", "yuv4mpegpipe", "-i", "-"]
        vs_args = ["-s", str(frame - 1), "-e", str(frame + 1)]
    cmd += ["-vf", vf, "-fps_mode", "passthrough", "-frames:v", "2",
            "-start_number", "1", str(inp / f"extract_{frame}_%02d.png")]
    p = run_source_ffmpeg(ld, cmd, video, text=False, vs_args=vs_args)
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
    ap.add_argument("--cut-pad-image", action="append", default=[], metavar="FRAME=PNG",
                    help="use one reviewed Cut Pad Images/<episode>/ DVD still for a confirmed cut PAD; repeatable")
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
    if video.suffix.lower() not in (".avs", ".vpy"):
        raise PipelineError(
            f"replacement neighbors must come from the source-derived script "
            f"(.avs/.vpy), not encoded video: {video}")
    repair_file = probe / f"{ep.code}-pad-repair-frames.txt"
    cands = read_candidates(probe / f"{ep.code}-pad-audit.tsv", repair_file,
                            probe / f"{ep.code}-pad-fullres.tsv",
                            accept=frozenset(args.accept_residual))
    # shared report/legacy dirs are keyed by pad_report_label so historical rows match
    label = ep.data.get("pad_report_label") or ep.code
    hold = set(args.hold_cut)
    shared_images: dict[int, tuple[Path, str, int]] = {}
    for spec in args.cut_pad_image:
        frame, source_png, timestamp, dvd_frame = parse_shared_cut_image(
            spec, ep.code, Path(__file__).resolve().parent.parent)
        if frame in shared_images:
            raise PipelineError(f"multiple shared DVD stills were assigned to cut PAD frame {frame}")
        shared_images[frame] = (source_png, timestamp, dvd_frame)
    unknown = hold - {c["frame"] for c in cands}
    if unknown:
        raise PipelineError(f"--hold-cut frames are not confirmed pads: {sorted(unknown)}")
    unknown_images = set(shared_images) - {c["frame"] for c in cands}
    if unknown_images:
        raise PipelineError(f"shared-image frames are not confirmed PADs: {sorted(unknown_images)}")
    if shared_images and ep.data.get("dvd_reference_available") is not False:
        raise PipelineError("shared cut-pad still substitution is reserved for the explicit no-DVD workflow")
    if hold & set(shared_images):
        raise PipelineError(f"a cut PAD cannot be both held and image-filled: {sorted(hold & set(shared_images))}")

    no_dvd = ep.data.get("dvd_reference_available") is False
    if no_dvd:
        if ep.data.get("no_dvd_proceed_approved") is not True:
            raise PipelineError("no-DVD PAD handling requires the user's explicit no-DVD choice")
        cuts = set(int(x) for x in (ep.data.get("pads") or {}).get("cut_pad_frames", []))
        if len(cuts) != int((ep.data.get("pads") or {}).get("cut_pads") or 0):
            raise PipelineError("record the exact measured pads.cut_pad_frames list before no-DVD cut-PAD repair")
        if not cuts.issubset({c["frame"] for c in cands}):
            raise PipelineError(f"manifest cut-pad frames are not all confirmed candidates: {sorted(cuts)}")
        image_choice = ep.data.get("cut_pad_images_choice")
        if shared_images and image_choice != "use":
            raise PipelineError("shared DVD stills were supplied but the manifest does not record user choice 'use'")
        if image_choice == "skip" and shared_images:
            raise PipelineError("user declined shared cut-pad images; remove --cut-pad-image entries")
        unresolved = cuts - set(shared_images) - hold
        if unresolved:
            raise PipelineError(f"no-DVD cut PADs need a reviewed shared image or --hold-cut: {sorted(unresolved)}")
    unknown_acc = set(args.accept_residual) - {c["frame"] for c in cands}
    if unknown_acc:
        raise PipelineError(f"--accept-residual frames are not candidates: {sorted(unknown_acc)}")

    work = probe / "rife-padframes-local" / label
    inp, outd = work / "neighbors", work / "interpolated"
    for d in (inp, outd):
        d.mkdir(parents=True, exist_ok=True)
    # Resolve engine/model BEFORE the header print: the original order printed
    # `engine=` first and crashed with UnboundLocalError (S01E06 V5 rerun, 15:1x).
    if any(c["frame"] not in hold for c in cands):
        try:
            engine = str(ld.tool("rife_engine"))
            model = str(ld.tool("rife_model_dir"))
        except PipelineError as e:
            raise PipelineError(f"{e}\nRIFE is required to fill non-cut PADs.")
    else:
        engine, model = "not-used", "not-used"
    print(f"{label}: candidates={len(cands)} cut-hold={len(hold)} "
          f"CPU-RIFE={len(cands)-len(hold)}  engine={engine}", flush=True)
    if not cands:
        print("No confirmed isolated PADs; build_padfix_avs.py will create an identity AVS wrapper.")
        return 0
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
            source_dvd_timestamp = ""
            source_dvd_frame = ""
            source_png = ""
            if frame in shared_images:
                shared_png, source_dvd_timestamp, source_dvd_frame = shared_images[frame]
                from PIL import Image
                with Image.open(shared_png) as im:
                    shared_size = im.size
                with Image.open(prev) as source_im:
                    source_size = source_im.size
                if shared_size != source_size:
                    raise PipelineError(f"shared cut PAD image size {shared_size} does not match source-neighbor PNG {source_size}")
                shutil.copy2(shared_png, dst)
                method, elapsed, err = f"DVD-shared-cut-image:{source_dvd_timestamp}", 0.0, ""
                source_png = str(shared_png.relative_to(Path(__file__).resolve().parent.parent))
            elif frame in hold:
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
                          "png": str(dst.relative_to(probe)),
                          "source_dvd_timestamp": source_dvd_timestamp,
                          "source_dvd_frame": source_dvd_frame,
                          "source_png": source_png})
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
    print(f"next: python scripts/build_padfix_vpy.py --episode {ep.code} "
          f"(macOS; Windows uses build_padfix_avs.py)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
