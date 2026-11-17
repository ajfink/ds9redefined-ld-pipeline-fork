#!/usr/bin/env python3
"""Record explicitly reviewed PAD cut/non-cut classifications in one device manifest."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)
from dependency_lock import LockError
from rife_padframes import (MAX_MEAN, MAX_RGB, RIFE_MISIDENTIFICATION_WARNING,
                            parse_shared_cut_image,
                            require_color_match_note)

SCHEMA = "ld-pipeline-pad-classification-review-v1"
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"{label} must be an existing regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} must contain a JSON object: {path}")
    return value


def _exclusive_backup(path: Path, payload: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak-pad-classifications-{stamp}")
    with backup.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if backup.read_bytes() != payload:
        raise PipelineError(f"manifest backup verification failed: {backup}")
    return backup


def _atomic_replace(path: Path, payload: bytes, expected_original: bytes) -> None:
    if path.is_symlink() or path.read_bytes() != expected_original:
        raise PipelineError("manifest changed during PAD classification; refusing replacement")
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != expected_original:
            raise PipelineError("manifest changed before atomic PAD classification replace")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _manifest_lock(path: Path):
    class Lock:
        def __enter__(self):
            self.stream = path.open("x", encoding="utf-8", newline="\n")
            self.stream.write(f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            return self

        def __exit__(self, exc_type, exc, tb):
            self.stream.close()
            path.unlink(missing_ok=True)
            return False
    return Lock()


def _report_inputs(report: dict, report_path: Path, probe: Path,
                   expected_identity: str) -> tuple[Path, Path, Path, str]:
    if (report.get("schema") != SCHEMA or report.get("status") != "HUMAN_REVIEWED" or
            report.get("identity") != expected_identity):
        raise PipelineError(f"PAD classification report is not a reviewed report for {expected_identity}")
    declared = report.get("evidence") or {}
    if not isinstance(declared, dict):
        raise PipelineError("PAD classification report evidence must be an object")
    paths: dict[str, Path] = {}
    for role, key in (("audit", "pad_audit"), ("repair list", "repair_frames"),
                      ("full-raster report", "fullres_report")):
        raw = declared.get(key)
        if not isinstance(raw, str) or not raw:
            raise PipelineError(f"PAD classification report is missing its {role} path")
        path = Path(raw).expanduser()
        if path.is_symlink():
            raise PipelineError(f"PAD {role} path may not be a symlink: {path}")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(probe.resolve(strict=True))
        except ValueError as exc:
            raise PipelineError(f"PAD {role} must be in this episode's probe root: {resolved}") from exc
        if not resolved.is_file():
            raise PipelineError(f"PAD {role} is missing: {resolved}")
        if _sha256(resolved) != declared.get(key + "_sha256"):
            raise PipelineError(f"PAD {role} changed after human review")
        paths[key] = resolved
    report_digest = _sha256(report_path)
    return paths["pad_audit"], paths["repair_frames"], paths["fullres_report"], report_digest


def record_reviewed_classifications(decision_path: Path, ep, ld: Ld,
                                    manifest_path: Path, expected_sha256: str,
                                    review_note: str, *, apply: bool) -> tuple[Path, Path | None]:
    decision_path = decision_path.expanduser().resolve(strict=True)
    report = _read_json(decision_path, "PAD classification report")
    expected_identity = ep.identity
    actual_sha = _sha256(decision_path)
    if actual_sha != expected_sha256:
        raise PipelineError("PAD classification report changed after review")
    if (report.get("schema") != SCHEMA or report.get("status") != "HUMAN_REVIEWED" or
            report.get("identity") != expected_identity or
            len(review_note.strip()) < 8 or review_note.strip() != report.get("review_note") or
            report.get("reviewed_by") != getpass.getuser()):
        raise PipelineError("PAD review report identity, reviewer, or required note is invalid")
    probe = ld.probe().resolve(strict=True)
    audit_path, repair_path, fullres_path, report_digest = _report_inputs(
        report, decision_path, probe, expected_identity)
    committed_path = decision_path.with_name(decision_path.stem + "-committed.json")
    if committed_path.exists() or committed_path.is_symlink():
        raise PipelineError(f"committed PAD decision record already exists: {committed_path}")
    if _sha256(manifest_path) != report.get("manifest_sha256"):
        current = _read_json(manifest_path, "episode manifest")
        previous = (current.get("pads") or {}).get("pad_classification_review") or {}
        if previous.get("decision_sha256") == report_digest:
            committed = dict(report)
            committed.update({"status": "COMMITTED",
                              "decision_sha256": previous.get("decision_sha256"),
                              "manifest_sha256_after": _sha256(manifest_path),
                              "committed_utc": datetime.now(timezone.utc).isoformat(),
                              "cut_frames": previous.get("cut_frames", []),
                              "rife_frames": previous.get("rife_frames", []),
                              "hold_frames": previous.get("hold_frames", []),
                              "cut_images": previous.get("cut_images", {}),
                              "accept_residual": previous.get("accepted_residual_frames", [])})
            with committed_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(committed, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            return manifest_path, committed_path
        raise PipelineError("manifest changed after PAD review; regenerate and review classifications again")

    import csv
    with audit_path.open(newline="", encoding="utf-8-sig") as stream:
        audit_rows = {int(row["frame"]): row for row in csv.DictReader(stream, delimiter="\t")}
    with fullres_path.open(newline="", encoding="utf-8-sig") as stream:
        fullres_rows = {int(row["frame"]): row for row in csv.DictReader(stream, delimiter="\t")}
    try:
        candidate_frames = sorted({int(value) for value in repair_path.read_text(encoding="utf-8").split()})
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot parse confirmed PAD repair list: {exc}") from exc
    decisions = report.get("candidates")
    if not isinstance(decisions, list) or any(not isinstance(row, dict) for row in decisions):
        raise PipelineError("reviewed PAD candidates must be an array of objects")
    frames = [row.get("frame") for row in decisions]
    if (any(type(frame) is not int or frame < 0 for frame in frames) or
            len(frames) != len(set(frames)) or sorted(frames) != candidate_frames):
        raise PipelineError("human PAD classifications must cover every unique repair candidate exactly once")

    original_manifest = _read_json(manifest_path, "episode manifest")
    pads = original_manifest.get("pads") or {}
    old_cut_frames = pads.get("cut_pad_frames") or []
    if (not isinstance(old_cut_frames, list) or
            any(type(frame) is not int or frame < 0 for frame in old_cut_frames) or
            len(old_cut_frames) != len(set(old_cut_frames))):
        raise PipelineError("manifest pads.cut_pad_frames must be an integer array before review")
    old_cut_set = set(old_cut_frames)
    original_cut_frames = report.get("original_cut_frames") or []
    if (not isinstance(original_cut_frames, list) or
            any(type(frame) is not int or frame < 0 for frame in original_cut_frames) or
            len(original_cut_frames) != len(set(original_cut_frames)) or
            set(original_cut_frames) != old_cut_set):
        raise PipelineError("reviewed original cut-PAD list does not match the current manifest")
    dvd_flag = original_manifest.get("dvd_reference_available")
    current_dvd_available = (dvd_flag is True if dvd_flag is not None else
                             bool((original_manifest.get("files") or {}).get("dvd")))
    if bool(report.get("dvd_reference_available")) != current_dvd_available:
        raise PipelineError("DVD-backed/no-DVD classification mode changed after visual review")
    if report.get("cut_pad_images_choice") != original_manifest.get("cut_pad_images_choice"):
        raise PipelineError("cut-PAD image policy changed after visual review")
    cut_frames: list[int] = []
    rife_frames: list[int] = []
    hold_frames: list[int] = []
    cut_images: dict[int, str] = {}
    accept_residual: list[int] = []
    dvd_available = current_dvd_available
    image_choice = original_manifest.get("cut_pad_images_choice")
    if not dvd_available and original_manifest.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("no-DVD PAD review requires the manifest's explicit no_dvd_proceed_approved decision")
    for row in decisions:
        frame = row["frame"]
        audit = audit_rows.get(frame)
        raster = fullres_rows.get(frame)
        if not audit or int(audit.get("is_predicted_pad", 0)) != 1 or not raster:
            raise PipelineError(f"frame {frame} is not a complete PAD audit/full-raster candidate")
        try:
            rgb_max, rgb_mean = int(raster["rgb_max"]), float(raster["rgb_mean"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(f"frame {frame} has invalid full-raster evidence") from exc
        residual = rgb_max > MAX_RGB or rgb_mean > MAX_MEAN
        if residual and row.get("accept_residual") is not True:
            raise PipelineError(f"frame {frame} is above the full-raster near-black threshold; explicitly accept its residual after review")
        if residual:
            accept_residual.append(frame)
        classification = row.get("classification")
        if classification == "rife":
            if frame in old_cut_set and (
                    row.get("confirmed_misidentified_cut") is not True or
                    row.get("rife_warning_acknowledged") is not True or
                    report.get("rife_misidentification_warning") != RIFE_MISIDENTIFICATION_WARNING):
                raise PipelineError(
                    f"frame {frame} was previously classified as a cut; show and acknowledge the required RIFE warning")
            rife_frames.append(frame)
            continue
        if classification != "cut":
            raise PipelineError(f"frame {frame} must be classified as 'cut' or 'rife'")
        cut_frames.append(frame)
        replacement = row.get("cut_replacement")
        if replacement == "hold":
            if dvd_available:
                raise PipelineError(f"DVD-backed cut PAD {frame} cannot use a previous-frame hold")
            if row.get("confirmed_hold") is not True:
                raise PipelineError(f"no-DVD cut PAD {frame} needs explicit hold confirmation")
            hold_frames.append(frame)
        elif replacement == "image":
            image = str(row.get("cut_image_path") or "")
            if not image:
                raise PipelineError(f"cut PAD {frame} needs a reviewed cut image")
            parsed_frame, source_png, _timestamp, _dvd_frame = parse_shared_cut_image(
                f"{frame}={image}", ep.code, ROOT, ep.series)
            if parsed_frame != frame:
                raise PipelineError(f"cut image frame does not match reviewed cut PAD {frame}")
            if dvd_available:
                require_color_match_note(source_png)
            if not dvd_available and image_choice == "skip":
                raise PipelineError("manifest records cut-pad-images choice 'skip'; remove selected stills")
            cut_images[frame] = str(source_png)
        else:
            raise PipelineError(f"cut PAD {frame} needs a reviewed still or (no-DVD only) hold")

    if not apply:
        print(f"PAD classifications ready for {expected_identity}: cut={len(cut_frames)} RIFE={len(rife_frames)}")
        return manifest_path, None

    lock_path = manifest_path.with_name(manifest_path.name + ".pad-classifications.lock")
    assert_writable_path(lock_path, ld)
    instance_root = Path(os.environ.get("LDP_INSTANCE_ROOT", "")).expanduser().resolve(strict=True)
    try:
        decision_relpath = decision_path.relative_to(instance_root).as_posix()
        committed_relpath = committed_path.relative_to(instance_root).as_posix()
        if not decision_relpath.startswith("plans/") or not committed_relpath.startswith("plans/"):
            raise ValueError("decision records must be inside plans/")
    except ValueError as exc:
        raise PipelineError(f"PAD decision records must remain in the active device plans folder: {exc}") from exc
    with _manifest_lock(lock_path):
        original = manifest_path.read_bytes()
        if hashlib.sha256(original).hexdigest() != report.get("manifest_sha256"):
            raise PipelineError("manifest changed while waiting to record PAD classifications")
        manifest = _read_json(manifest_path, "episode manifest")
        current_pads = manifest.setdefault("pads", {})
        current_pads["cut_pad_frames"] = sorted(cut_frames)
        current_pads["cut_pads"] = len(cut_frames)
        current_pads["pad_classification_review"] = {
            "schema": SCHEMA,
            "decision_sha256": report_digest,
            "decision_report_path": decision_relpath,
            "committed_report_path": committed_relpath,
            "audit_sha256": report["evidence"]["pad_audit_sha256"],
            "repair_sha256": report["evidence"]["repair_frames_sha256"],
            "fullres_sha256": report["evidence"]["fullres_report_sha256"],
            "reviewed_by": getpass.getuser(),
            "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
            "review_note": review_note.strip(),
            "original_cut_frames": sorted(old_cut_set),
            "cut_frames": sorted(cut_frames),
            "rife_frames": sorted(rife_frames),
            "hold_frames": sorted(hold_frames),
            "cut_images": {str(frame): path for frame, path in sorted(cut_images.items())},
            "accepted_residual_frames": sorted(accept_residual),
            "misidentified_cut_reclassifications": sorted(
                frame for frame in rife_frames if frame in old_cut_set),
        }
        backup = _exclusive_backup(manifest_path, original)
        updated_payload = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        _atomic_replace(manifest_path, updated_payload, original)
        committed = dict(report)
        committed.update({"status": "COMMITTED", "decision_sha256": report_digest,
                          "manifest_sha256_after": _sha256(manifest_path),
                          "committed_utc": datetime.now(timezone.utc).isoformat(),
                          "backup_name": backup.name,
                          "cut_frames": sorted(cut_frames), "rife_frames": sorted(rife_frames),
                          "hold_frames": sorted(hold_frames),
                          "cut_images": {str(frame): path for frame, path in sorted(cut_images.items())},
                          "accept_residual": sorted(accept_residual)})
        with committed_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(committed, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(f"recorded reviewed PAD classifications for {expected_identity}: cut={len(cut_frames)} RIFE={len(rife_frames)}")
    print(f"manifest backup: {backup}")
    print(f"committed decision record: {committed_path}")
    return manifest_path, committed_path


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"))
    parser.add_argument("--decision-file", required=True)
    parser.add_argument("--confirm-review", required=True)
    parser.add_argument("--confirm-decision-sha256", required=True)
    parser.add_argument("--review-note", required=True)
    parser.add_argument("--apply", action="store_true")
    args=parser.parse_args()
    try:
        identity = args.confirm_review.strip().upper()
        series, code = identity.split(":", 1)
        episode = args.episode.strip().upper()
        if args.series and args.series.upper() != series:
            raise PipelineError("review identity series conflicts with --series")
        if code != episode or identity != f"{series}:{episode}":
            raise PipelineError("--confirm-review must match the requested SERIES:SxxExx identity")
        decision_path = Path(args.decision_file).expanduser().resolve(strict=True)
        report = _read_json(decision_path, "PAD classification report")
        if report.get("identity") != identity:
            raise PipelineError(f"PAD classification report does not match {identity}")
        if _sha256(decision_path) != args.confirm_decision_sha256.lower():
            raise PipelineError("PAD classification report digest does not match --confirm-decision-sha256")
        if args.review_note.strip() != report.get("review_note"):
            raise PipelineError("--review-note does not match the note in the reviewed report")
        root = Path(os.environ.get("LDP_INSTANCE_ROOT", "")).expanduser().resolve(strict=True)
        if decision_path.parent.resolve() != (root / "plans").resolve():
            raise PipelineError("PAD classification report must be saved in the active episode plans folder")
        ld = Ld()
        ep = load_episode(identity, ld, series=series)
        manifest_path = episode_manifest_path(episode, ld, series=series).resolve(strict=True)
        assert_writable_path(manifest_path, ld)
        from shared_workspace import validate_device_config_profile, validate_instance
        instance = Path(os.environ.get("LDP_INSTANCE_ROOT", "")).expanduser().resolve(strict=True)
        validate_device_config_profile(ld.profile_path, ld.profile, ROOT)
        validate_instance(str(instance), ld.profile_path, ld.profile, identity)
        record_reviewed_classifications(
            decision_path, ep, ld, manifest_path, args.confirm_decision_sha256,
            args.review_note, apply=args.apply)
        return 0
    except (PipelineError, LockError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"PAD CLASSIFICATION ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
