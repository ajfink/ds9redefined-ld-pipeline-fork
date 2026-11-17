#!/usr/bin/env python3
"""Generate replacements for one episode's confirmed single-frame PAD blips.

  python scripts\\rife_padframes.py --episode S01E05
  python scripts\\rife_padframes.py --episode S01E05 --hold-cut 26160
  python scripts\\rife_padframes.py --episode S01E05 --cut-pad-image 26160=...\\DVD-....png

Inputs (profile probe root):  <code>-pad-audit.tsv, <code>-pad-repair-frames.txt,
                              <code>-pad-fullres.tsv
Outputs: probe/rife-padframes/<code>/{neighbors,interpolated}/*.png
         probe/rife-pad-replacements-local.tsv  (merged by episode+frame)

Non-cut candidates use RIFE. Cut candidates never use RIFE: a DVD-backed cut
requires a reviewed per-device DVD still; the explicit no-DVD path may use a
user-approved still or hold the previous native frame. The full-raster check is
mandatory, and a selective run never clobbers other episodes' report rows.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
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
RIFE_MISIDENTIFICATION_WARNING = (
    "ONLY generate as an interpolated frame if this pad frame was incorrectly identified as preceding a scene cut.")
DVD_IMAGE_RE = re.compile(r"^DVD-(\d{2})-(\d{2})-(\d{2})\.(\d{3})-f(\d{8})\.png$", re.IGNORECASE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_human_classification(episode_data: dict, candidate_frames: set[int],
                                  audit: Path, repair_file: Path, fullres: Path,
                                  hold_frames: set[int], image_frames: set[int],
                                  accepted_residual: set[int], *,
                                  identity: str | None = None,
                                  instance_root: Path | None = None) -> None:
    """Enforce the V16 human review record before any non-empty RIFE run."""
    if not candidate_frames:
        return
    pads = episode_data.get("pads") or {}
    review = pads.get("pad_classification_review") or {}
    if (review.get("schema") != "ld-pipeline-pad-classification-review-v1" or
            not re.fullmatch(r"[0-9a-f]{64}", str(review.get("decision_sha256", ""))) or
            not str(review.get("reviewed_by", "")).strip() or
            len(str(review.get("review_note", "")).strip()) < 8):
        raise PipelineError("human PAD classification must be recorded and reviewed before RIFE")
    if instance_root is not None:
        def read_plan(key: str, status: str) -> dict:
            relative = Path(str(review.get(key, "")))
            if (relative.is_absolute() or relative.parts[:1] != ("plans",) or
                    any(part in ("..", "") for part in relative.parts)):
                raise PipelineError(f"PAD {key} must identify a report under the active device plans folder")
            path = instance_root / relative
            if path.is_symlink():
                raise PipelineError(f"PAD {key} may not be a symlink")
            try:
                path.resolve(strict=True).relative_to((instance_root / "plans").resolve(strict=True))
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PipelineError(f"cannot read committed PAD {key}: {exc}") from exc
            if value.get("schema") != "ld-pipeline-pad-classification-review-v1" or value.get("status") != status:
                raise PipelineError(f"PAD {key} has an unexpected schema/status")
            if identity and value.get("identity") != identity:
                raise PipelineError(f"PAD {key} belongs to a different episode")
            return value

        decision = read_plan("decision_report_path", "HUMAN_REVIEWED")
        committed = read_plan("committed_report_path", "COMMITTED")
        decision_path = instance_root / str(review["decision_report_path"])
        decision_evidence = decision.get("evidence")
        if not isinstance(decision_evidence, dict):
            raise PipelineError("PAD human decision report has no hashed evidence inventory")
        if (_sha256(decision_path) != review.get("decision_sha256") or
                committed.get("decision_sha256") != review.get("decision_sha256") or
                decision.get("identity") != committed.get("identity") or
                decision.get("review_note") != review.get("review_note") or
                decision.get("reviewed_by") != review.get("reviewed_by") or
                decision.get("original_cut_frames") != review.get("original_cut_frames") or
                decision_evidence.get("pad_audit_sha256") != review.get("audit_sha256") or
                decision_evidence.get("repair_frames_sha256") != review.get("repair_sha256") or
                decision_evidence.get("fullres_report_sha256") != review.get("fullres_sha256")):
            raise PipelineError("PAD human decision/commit record does not match the manifest review")
        decision_rows = decision.get("candidates")
        if not isinstance(decision_rows, list):
            raise PipelineError("PAD human decision report has no candidate classifications")
        decision_cut = sorted(row.get("frame") for row in decision_rows
                              if isinstance(row, dict) and row.get("classification") == "cut")
        decision_rife = sorted(row.get("frame") for row in decision_rows
                               if isinstance(row, dict) and row.get("classification") == "rife")
        if (decision_cut != review.get("cut_frames") or
                decision_rife != review.get("rife_frames") or
                committed.get("cut_frames") != review.get("cut_frames") or
                committed.get("rife_frames") != review.get("rife_frames")):
            raise PipelineError("PAD human report and committed classification lists disagree")
        if review.get("misidentified_cut_reclassifications") and any(
                decision.get("rife_misidentification_warning") != RIFE_MISIDENTIFICATION_WARNING or
                row.get("confirmed_misidentified_cut") is not True or
                row.get("rife_warning_acknowledged") is not True
                for row in decision.get("candidates", [])
                if row.get("frame") in set(review["misidentified_cut_reclassifications"])):
            raise PipelineError("reclassified cut PAD is missing the exact RIFE warning acknowledgement")
    for path, key in ((audit, "audit_sha256"), (repair_file, "repair_sha256"),
                      (fullres, "fullres_sha256")):
        if path.is_symlink() or not path.is_file() or _sha256(path) != review.get(key):
            raise PipelineError(f"PAD {key} input changed after human visual classification: {path}")
    try:
        cut = review.get("cut_frames")
        rife = review.get("rife_frames")
        original = review.get("original_cut_frames")
        hold = review.get("hold_frames")
        residual = review.get("accepted_residual_frames")
        images = review.get("cut_images")
        if any(not isinstance(value, list) or any(type(frame) is not int or frame < 0 for frame in value)
               or len(value) != len(set(value)) for value in (cut, rife, original, hold, residual)):
            raise ValueError("frame lists are malformed")
        if not isinstance(images, dict):
            raise ValueError("cut_images is not an object")
        cut_set, rife_set, hold_set = set(cut), set(rife), set(hold)
        residual_set = set(residual)
        image_set = {int(frame) for frame in images}
        current_cuts = pads.get("cut_pad_frames")
        if (cut_set & rife_set or cut_set | rife_set != candidate_frames or
                current_cuts != sorted(cut_set) or pads.get("cut_pads") != len(cut_set) or
                hold_set != hold_frames or image_set != image_frames or
                residual_set != accepted_residual or hold_set & image_set or
                (hold_set | image_set) - cut_set or residual_set - candidate_frames or
                set(review.get("misidentified_cut_reclassifications", [])) !=
                (rife_set & set(original))):
            raise PipelineError("current manifest, CLI cut policy, and human PAD classification decisions disagree")
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"human PAD classification record is malformed: {exc}") from exc


def read_candidates(audit: Path, repair_file: Path, fullres: Path,
                    accept: frozenset[int] = frozenset()) -> list[dict]:
    if not audit.exists():
        raise PipelineError(f"audit missing: {audit} - run pad_audit.py first")
    if not repair_file.exists():
        raise PipelineError(f"confirmed PAD repair list missing: {repair_file} - run pad_audit.py first")
    if not fullres.exists():
        raise PipelineError(f"full-raster report missing: {fullres} - run fullres_check.py first")
    def keyed_rows(path: Path, required: tuple[str, ...], label: str) -> dict[int, dict]:
        result = {}
        try:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream, delimiter="\t")
                if not reader.fieldnames or not set(required).issubset(reader.fieldnames):
                    raise PipelineError(f"{label} has missing columns; expected {required}: {path}")
                for row in reader:
                    try:
                        frame = int(row["frame"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise PipelineError(f"{label} has an invalid frame value: {path}") from exc
                    if frame < 0 or frame in result:
                        raise PipelineError(f"{label} contains a negative or duplicate frame {frame}: {path}")
                    result[frame] = row
        except (OSError, csv.Error) as exc:
            raise PipelineError(f"cannot read {label} {path}: {exc}") from exc
        return result

    try:
        wanted = [int(x) for x in repair_file.read_text(encoding="utf-8-sig").split() if x.strip()]
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot parse repair candidate list {repair_file}: {exc}") from exc
    if len(wanted) != len(set(wanted)) or any(frame < 0 for frame in wanted):
        raise PipelineError(f"repair candidate list has duplicate or negative frame values: {repair_file}")
    wanted.sort()
    rows = keyed_rows(
        audit,
        ("frame", "is_predicted_pad", "motion_score", "time") if wanted else ("frame", "is_predicted_pad"),
        "PAD audit")
    if not wanted:
        # An empty confirmed repair list is an identity repair. Still require the
        # expected full-raster evidence schema, but no per-candidate metrics exist.
        keyed_rows(fullres, ("frame", "rgb_max", "rgb_mean"), "full-raster report")
        return []
    raster_rows = keyed_rows(fullres, ("frame", "rgb_max", "rgb_mean"), "full-raster report")
    checked = {}
    for frame, row in raster_rows.items():
        try:
            rgb_max, rgb_mean = int(row["rgb_max"]), float(row["rgb_mean"])
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"full-raster report has invalid pixel statistics for frame {frame}") from exc
        if not (0 <= rgb_max <= 255 and math.isfinite(rgb_mean) and 0 <= rgb_mean <= 255):
            raise PipelineError(f"full-raster report has out-of-range pixel statistics for frame {frame}")
        checked[frame] = (rgb_max, rgb_mean)
    out = []
    for frame in wanted:
        row = rows.get(frame)
        try:
            is_predicted = int(row.get("is_predicted_pad", 0)) if row else 0
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"repair frame {frame} has an invalid PAD-map flag") from exc
        if row is None or is_predicted != 1:
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
        try:
            time_s = float(row["time"])
            motion = float(row["motion_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(f"repair frame {frame} has invalid audit metrics") from exc
        if not math.isfinite(time_s) or not math.isfinite(motion):
            raise PipelineError(f"repair frame {frame} has non-finite audit metrics")
        out.append({"frame": frame, "time": time_s,
                    "motion": motion,
                    "rgb_max": maxv, "rgb_mean": meanv})
    return sorted(out, key=lambda r: r["frame"])


def parse_shared_cut_image(spec: str, episode: str, bundle: Path,
                           series: str = "DS9") -> tuple[int, Path, str, int]:
    """Validate FRAME=PNG against the curated episode folder and return its DVD timestamp."""
    if "=" not in spec:
        raise PipelineError(f"--cut-pad-image must be FRAME=PNG, got {spec!r}")
    frame_s, path_s = spec.split("=", 1)
    try:
        frame = int(frame_s)
    except ValueError as exc:
        raise PipelineError(f"cut-pad output frame must be an integer: {frame_s!r}") from exc
    source_png = Path(path_s).expanduser().resolve()
    image_root = (bundle / "Cut Pad Images" / (episode if series == "DS9" else series) /
                  ("" if series == "DS9" else episode)).resolve()
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
        workspace_name = episode if series == "DS9" else f"{series}-{episode}"
        # devices/<device-instance>/episodes/<identity-folder>/cut-pad-images/<png>
        allowed = bool(relative and len(relative.parts) == 5 and
                       relative.parts[1] == "episodes" and
                       relative.parts[2].casefold() == workspace_name.casefold() and
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
                str(provenance.get("series", "DS9")).upper() != series.upper() or
                provenance.get("identity", f"DS9:{provenance.get('episode', '')}") != f"{series.upper()}:{episode.upper()}" or
                provenance_frame != frame or
                provenance.get("reviewed_episode_scene_and_pad_mapping") is not True or
                provenance.get("png_sha256") != digest or
                provenance.get("dvd_timestamp") !=
                f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}" or
                not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_dvd_sha256", "")))):
            raise PipelineError(f"cut-PAD provenance does not verify image/frame/mapping: {sidecar}")
    timestamp = f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}"
    return frame, source_png, timestamp, int(match.group(5))


def is_device_instance_cut_pad_image(source: Path, bundle: Path, episode: str,
                                     series: str = "DS9") -> bool:
    """True only for this episode's instance inbox, not the curated source folder."""
    source = source.resolve()
    try:
        relative = source.relative_to((bundle / "devices").resolve())
    except ValueError:
        relative = None
    workspace_name = episode if series == "DS9" else f"{series}-{episode}"
    if (relative and len(relative.parts) == 5 and relative.parts[1] == "episodes" and
            relative.parts[2].casefold() == workspace_name.casefold() and
            relative.parts[3] == "cut-pad-images"):
        return True
    active_root = os.environ.get("LDP_CUT_PAD_IMAGES")
    if active_root:
        active = Path(active_root).expanduser().resolve()
        return (source.parent == active and active.name == "cut-pad-images" and
                active.parent.name.casefold() == workspace_name.casefold())
    return False


def require_color_match_note(source: Path) -> str:
    sidecar = source.with_suffix(".provenance.json")
    try:
        provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid cut-PAD provenance sidecar {sidecar}: {exc}") from exc
    note = str(provenance.get("color_match_note", "")).strip()
    if not note:
        raise PipelineError(f"DVD-backed cut-PAD image needs a reviewed color_match_note: {sidecar}")
    return note


def validate_cut_replacement_policy(episode_data: dict, candidates: set[int],
                                    hold_frames: set[int],
                                    image_frames: set[int]) -> set[int]:
    """Keep cut-PAD inventory complete and select policy by DVD availability."""
    pads = episode_data.get("pads") or {}
    raw_count = pads.get("cut_pads") or 0
    raw_frames = pads.get("cut_pad_frames") or []
    if type(raw_count) is not int or not isinstance(raw_frames, list) or any(
            type(frame) is not int for frame in raw_frames):
        raise PipelineError("pads.cut_pad_frames/cut_pads must contain integer frame values")
    cut_count = raw_count
    cuts = set(raw_frames)
    if cut_count < 0 or len(cuts) != cut_count:
        raise PipelineError("record the exact, unique measured pads.cut_pad_frames list before cut-PAD repair")
    if not cuts.issubset(candidates):
        raise PipelineError(f"manifest cut-pad frames are not all confirmed candidates: {sorted(cuts - candidates)}")
    if (hold_frames | image_frames) - cuts:
        raise PipelineError("cut-PAD hold/image substitutions may target only manifest-confirmed cut pads")
    if hold_frames & image_frames:
        raise PipelineError(f"a cut PAD cannot be both held and image-filled: {sorted(hold_frames & image_frames)}")

    dvd_available = episode_data.get("dvd_reference_available")
    if dvd_available is None:  # same legacy inference as validate_episode.py
        dvd_available = bool((episode_data.get("files") or {}).get("dvd"))
    no_dvd = not bool(dvd_available)
    if not no_dvd:
        if hold_frames:
            raise PipelineError("--hold-cut is only a no-DVD fallback; DVD-backed cut PADs need matched DVD stills")
        missing = cuts - image_frames
        if missing:
            raise PipelineError(f"DVD-backed cut PADs need reviewed per-device DVD stills: {sorted(missing)}")
        return cuts

    if episode_data.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("no-DVD PAD handling requires the user's explicit no-DVD choice")
    image_choice = episode_data.get("cut_pad_images_choice")
    if image_frames and image_choice != "use":
        raise PipelineError("shared DVD stills were supplied but the manifest does not record user choice 'use'")
    if image_choice == "skip" and image_frames:
        raise PipelineError("user declined shared cut-pad images; remove --cut-pad-image entries")
    unresolved = cuts - image_frames - hold_frames
    if unresolved:
        raise PipelineError(f"no-DVD cut PADs need a reviewed shared image or --hold-cut: {sorted(unresolved)}")
    return cuts


def extract_neighbors(ld: Ld, video: Path, frame: int, inp: Path) -> tuple[Path, Path]:
    """Extract exact-index real flanks around a black PAD frame.

    Both backends deliver exactly frame-1..frame+1 before selecting n=0 and n=2.
    AviSynth uses a trimmed wrapper rather than a timestamp seek, so dim-scene
    review and RIFE cannot silently use an adjacent frame after seek rounding.
    """
    if frame < 1:
        raise PipelineError(f"cannot take a previous neighbour for frame {frame}")
    prev = inp / f"prev_{frame:06d}.png"
    nxt = inp / f"next_{frame:06d}.png"
    vf = "select='eq(n,0)+eq(n,2)',format=rgb24"
    cmd = [ld.ffmpeg(), "-y", "-hide_banner", "-v", "error"]
    vs_args = None
    input_video = video
    if video.suffix.lower() == ".avs":
        avs_range = inp / f"neighbor_{frame:06d}.avs"
        avs_range.write_text(
            f"src = Import({json.dumps(str(video.resolve(strict=True)))})\n"
            f"src.Trim({frame - 1}, {frame + 1})\n",
            encoding="utf-8")
        cmd += ["-f", "avisynth", "-i", str(avs_range)]
        input_video = avs_range
    else:
        cmd += ["-f", "yuv4mpegpipe", "-i", "-"]
        vs_args = ["-s", str(frame - 1), "-e", str(frame + 1)]
    cmd += ["-vf", vf, "-fps_mode", "passthrough", "-frames:v", "2",
            "-start_number", "1", str(inp / f"extract_{frame}_%02d.png")]
    p = run_source_ffmpeg(ld, cmd, input_video, text=False, vs_args=vs_args)
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
    ap.add_argument("--audit", help="override PAD audit TSV (defaults to <episode>-pad-audit.tsv)")
    ap.add_argument("--repair-frames", help="override confirmed repair list")
    ap.add_argument("--fullres", help="override full-raster report")
    ap.add_argument("--run-id", help="new per-run workspace token to preserve prior partial RIFE outputs")
    ap.add_argument("--hold-cut", type=int, action="append", default=[],
                    help="no-DVD fallback: hold the previous native frame at a confirmed cut PAD; repeatable")
    ap.add_argument("--cut-pad-image", action="append", default=[], metavar="FRAME=PNG",
                    help="use one reviewed timestamped/provenanced DVD still for a confirmed cut PAD; repeatable")
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
    repair_file = Path(args.repair_frames).expanduser() if args.repair_frames else probe / f"{ep.code}-pad-repair-frames.txt"
    audit_file = Path(args.audit).expanduser() if args.audit else probe / f"{ep.code}-pad-audit.tsv"
    fullres_file = Path(args.fullres).expanduser() if args.fullres else probe / f"{ep.code}-pad-fullres.tsv"
    cands = read_candidates(audit_file, repair_file, fullres_file,
                            accept=frozenset(args.accept_residual))
    # shared report/legacy dirs are keyed by pad_report_label so historical rows match
    label = ep.data.get("pad_report_label") or ep.code
    hold = set(args.hold_cut)
    shared_images: dict[int, tuple[Path, str, int]] = {}
    for spec in args.cut_pad_image:
        frame, source_png, timestamp, dvd_frame = parse_shared_cut_image(
            spec, ep.code, Path(__file__).resolve().parent.parent, ep.series)
        if frame in shared_images:
            raise PipelineError(f"multiple shared DVD stills were assigned to cut PAD frame {frame}")
        shared_images[frame] = (source_png, timestamp, dvd_frame)
    instance_value = os.environ.get("LDP_INSTANCE_ROOT")
    if not instance_value:
        raise PipelineError("RIFE requires the active device workspace containing the committed human PAD review")
    instance_root = Path(instance_value).expanduser().resolve(strict=True)
    validate_human_classification(
        ep.data, {row["frame"] for row in cands}, audit_file, repair_file, fullres_file,
        hold, set(shared_images), set(args.accept_residual),
        identity=ep.identity, instance_root=instance_root)
    unknown = hold - {c["frame"] for c in cands}
    if unknown:
        raise PipelineError(f"--hold-cut frames are not confirmed pads: {sorted(unknown)}")
    unknown_images = set(shared_images) - {c["frame"] for c in cands}
    if unknown_images:
        raise PipelineError(f"shared-image frames are not confirmed PADs: {sorted(unknown_images)}")
    dvd_available = ep.data.get("dvd_reference_available")
    if dvd_available is None:
        dvd_available = bool((ep.data.get("files") or {}).get("dvd"))
    no_dvd = not bool(dvd_available)
    cuts = validate_cut_replacement_policy(
        ep.data, {c["frame"] for c in cands}, hold, set(shared_images))
    if not no_dvd:
        bundle = Path(__file__).resolve().parent.parent
        for frame, (source_png, _timestamp, _dvd_frame) in shared_images.items():
            if not is_device_instance_cut_pad_image(source_png, bundle, ep.code, ep.series):
                raise PipelineError(
                    "DVD-backed cut PAD stills must be published in this episode's "
                    "devices/<device>/episodes/<episode>/cut-pad-images folder")
            require_color_match_note(source_png)
    unknown_acc = set(args.accept_residual) - {c["frame"] for c in cands}
    if unknown_acc:
        raise PipelineError(f"--accept-residual frames are not candidates: {sorted(unknown_acc)}")

    work_root = probe / "rife-padframes-local" / label
    if args.run_id:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.run_id):
            raise PipelineError("--run-id must be a safe 1..64 character path component")
        work = work_root / args.run_id
        if work.exists() or work.is_symlink():
            raise PipelineError(f"refusing to reuse a prior PAD replacement run directory: {work}")
    else:
        work = work_root
    inp, outd = work / "neighbors", work / "interpolated"
    for d in (inp, outd):
        d.mkdir(parents=True, exist_ok=True)
    # Resolve engine/model BEFORE the header print: the original order printed
    # `engine=` first and crashed with UnboundLocalError (S01E06 V5 rerun, 15:1x).
    rife_frames = {c["frame"] for c in cands} - hold - set(shared_images)
    if rife_frames:
        try:
            engine = str(ld.tool("rife_engine"))
            model = str(ld.tool("rife_model_dir"))
        except PipelineError as e:
            raise PipelineError(f"{e}\nRIFE is required to fill non-cut PADs.")
    else:
        engine, model = "not-used", "not-used"
    print(f"{label}: candidates={len(cands)} cut-hold={len(hold)} "
          f"cut-image={len(shared_images)} CPU-RIFE={len(rife_frames)}  engine={engine}", flush=True)
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
