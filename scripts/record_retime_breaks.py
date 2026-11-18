#!/usr/bin/env python3
"""Preview or explicitly record reviewed break candidates into a new manifest revision.

The default is read-only. Recording requires --apply, the exact canonical identity in
--confirm-review, an explicit review note, and labels that the candidate report marks unique
(or explicitly no-audio-silence for a human-declared merged-music case). This tool records
black/silence intervals only; offsets, steps, and gap values always remain null for the
separate measured/adjudication stage.
"""
from __future__ import annotations

import argparse
import hashlib
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from dependency_lock import LockError  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from manifest_io import atomic_replace, exclusive_backup, manifest_lock as _manifest_lock  # noqa: E402
from retime_break_candidates import (CANDIDATE_SCHEMA, candidate_break_entries,
                                     sha256_file)  # noqa: E402


def _read_json(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"{label} must be an existing regular file, not a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} root must be a JSON object: {path}")
    return value


def _validate_report_inputs(report: dict, report_path: Path, ep, ld: Ld,
                            manifest_path: Path, expected_report_sha256: str) -> str:
    if report.get("schema") != CANDIDATE_SCHEMA or report.get("status") != "REPORT_ONLY":
        raise PipelineError("candidate report schema/status is unsupported")
    if (report.get("identity") != ep.identity or report.get("series") != ep.series or
            report.get("episode") != ep.code):
        raise PipelineError(f"candidate report identity does not match {ep.identity}")
    if sha256_file(manifest_path) != report.get("manifest_sha256"):
        raise PipelineError("manifest changed after candidate preparation; regenerate the report")
    inputs = report.get("inputs") or {}
    probe = ld.probe().resolve()
    ld_report_relative = inputs.get("ld_black_report_relative_path")
    if not isinstance(ld_report_relative, str):
        raise PipelineError("candidate report has no safe LD-black report relative path")
    relative = PurePosixPath(ld_report_relative)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts or
            "\\" in ld_report_relative or ":" in ld_report_relative or
            relative.as_posix() != ld_report_relative):
        raise PipelineError("candidate report has an unsafe LD-black report relative path")
    ld_report_path = probe.joinpath(*relative.parts)
    try:
        ld_report_path = ld_report_path.resolve(strict=True)
        ld_report_path.relative_to(probe)
    except (OSError, ValueError) as exc:
        raise PipelineError("source LD-black report is missing or outside this probe root") from exc
    if ld_report_path.is_symlink() or not ld_report_path.is_file():
        raise PipelineError(f"source LD-black report is missing or unsafe: {ld_report_path}")
    if sha256_file(ld_report_path) != inputs.get("ld_black_report_sha256"):
        raise PipelineError("source LD-black report changed after candidate preparation")
    alignment_path = ep.path("align_npy")
    if sha256_file(alignment_path) != inputs.get("alignment_sha256"):
        raise PipelineError("alignment path changed after candidate preparation")
    dvd_path = ep.path("dvd")
    try:
        dvd_name = dvd_path.name
        dvd_size = dvd_path.stat().st_size
        dvd_mtime = dvd_path.stat().st_mtime_ns
    except OSError as exc:
        raise PipelineError(f"cannot verify candidate report DVD input: {exc}") from exc
    if (dvd_name != inputs.get("dvd_name") or dvd_size != inputs.get("dvd_size_bytes") or
            dvd_mtime != inputs.get("dvd_mtime_ns") or
            (inputs.get("dvd_sha256") and sha256_file(dvd_path) != inputs.get("dvd_sha256"))):
        raise PipelineError("DVD identity/metadata changed after candidate preparation")
    actual_report_sha256 = sha256_file(report_path)
    if actual_report_sha256 != expected_report_sha256:
        raise PipelineError("candidate report changed after review; regenerate and review again")
    return actual_report_sha256


def _exclusive_backup(path: Path, payload: bytes) -> Path:
    return exclusive_backup(path, payload, label="retime-breaks",
                            error_type=PipelineError)


def _atomic_replace(path: Path, payload: bytes, expected_original: bytes) -> None:
    atomic_replace(
        path, payload, expected_original, error_type=PipelineError,
        changed_message="manifest changed during apply; refusing to replace it",
        pre_replace_message="manifest changed before atomic replace; original preserved")


def apply_candidates(report: dict, report_path: Path, ep, ld: Ld,
                     manifest_path: Path, selected: list[str], merged: list[str],
                     review_note: str, report_sha256: str) -> tuple[Path, Path, list[dict]]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PipelineError(f"refusing missing/symlink manifest: {manifest_path}")
    assert_writable_path(manifest_path, ld)
    lock_path = manifest_path.with_name(manifest_path.name + ".retime-candidates.lock")
    assert_writable_path(lock_path, ld)
    with _manifest_lock(lock_path):
        original = manifest_path.read_bytes()
        if sha256_file(manifest_path) != report.get("manifest_sha256"):
            raise PipelineError("manifest changed since the candidate report; regenerate and review again")
        manifest = _read_json(manifest_path, "manifest")
        if (manifest.get("episode") != ep.code or str(manifest.get("series", "DS9")).upper() != ep.series or
                manifest.get("identity", ep.identity) != ep.identity):
            raise PipelineError(f"manifest identity does not match {ep.identity}")
        retime = manifest.setdefault("retime", {})
        if retime.get("breaks"):
            raise PipelineError("retime.breaks is not empty; refusing to replace or merge candidate/measurement data")
        report_hash = _validate_report_inputs(report, report_path, ep, ld, manifest_path,
                                              report_sha256)
        entries, merged_entries = candidate_break_entries(
            report, selected, merged, reviewed_by=getpass.getuser(),
            review_note=review_note, report_sha256=report_hash)
        retime["breaks"] = entries
        old_merged = retime.get("merged_music_through") or []
        if not isinstance(old_merged, list) or any(not isinstance(row, dict) for row in old_merged):
            raise PipelineError("retime.merged_music_through must be an array of interval objects")
        old_labels = {str(row.get("label", "")) for row in old_merged}
        duplicate_labels = old_labels & {str(row.get("label", "")) for row in merged_entries}
        if duplicate_labels:
            raise PipelineError("merged_music_through already contains candidate label(s): "
                                + ", ".join(sorted(duplicate_labels)))
        retime["merged_music_through"] = [*old_merged, *merged_entries]
        payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        _validate_report_inputs(report, report_path, ep, ld, manifest_path, report_hash)
        backup = _exclusive_backup(manifest_path, original)
        _validate_report_inputs(report, report_path, ep, ld, manifest_path, report_hash)
        _atomic_replace(manifest_path, payload.encode("utf-8"), original)
    return backup, manifest_path, entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True, help="SxxExx or SERIES:SxxExx")
    parser.add_argument("--candidate-report", required=True, help="report from prepare_retime_breaks.py")
    parser.add_argument("--select", action="append", default=[], metavar="LABEL",
                        help="human-reviewed unique-pair candidate to record; repeatable")
    parser.add_argument("--merged-through", action="append", default=[], metavar="LABEL",
                        help="human-reviewed no-silence candidate explicitly adjudicated as merged music-through")
    parser.add_argument("--dry-run", action="store_true", help="preview only (default)")
    parser.add_argument("--apply", action="store_true", help="write the reviewed candidate intervals")
    parser.add_argument("--confirm-review", help="must exactly equal the canonical SERIES:SxxExx identity")
    parser.add_argument("--confirm-report-sha256",
                        help="must equal the reviewed candidate report digest when applying")
    parser.add_argument("--review-note", help="short human review rationale, required with --apply")
    args = parser.parse_args()
    try:
        if args.apply and args.dry_run:
            raise PipelineError("--apply and --dry-run are mutually exclusive")
        ld = Ld()
        ep = load_episode(args.episode, ld)
        identity = ep.identity
        if args.apply:
            if args.confirm_review != identity:
                raise PipelineError(f"--apply requires --confirm-review {identity}")
            if not isinstance(args.review_note, str) or len(args.review_note.strip()) < 8:
                raise PipelineError("--apply requires a non-empty --review-note of at least 8 characters")
        raw_candidate_path = Path(args.candidate_report).expanduser()
        if raw_candidate_path.is_symlink():
            raise PipelineError(f"candidate report may not be a symlink: {raw_candidate_path}")
        candidate_path = raw_candidate_path.resolve()
        try:
            candidate_path.relative_to(ld.probe().resolve())
        except ValueError as exc:
            raise PipelineError("candidate report must reside under this episode's profile probe root") from exc
        candidate_bytes = candidate_path.read_bytes()
        report_hash = hashlib.sha256(candidate_bytes).hexdigest()
        try:
            report = json.loads(candidate_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"candidate report is not UTF-8 JSON: {candidate_path}: {exc}") from exc
        report_hash = _validate_report_inputs(
            report, candidate_path, ep, ld,
            episode_manifest_path(ep.code, ld, series=ep.series), report_hash)
        if args.apply and args.confirm_report_sha256 != report_hash:
            raise PipelineError(f"--apply requires --confirm-report-sha256 {report_hash}")
        if not args.apply and not args.select and not args.merged_through:
            print(json.dumps({
                "identity": identity,
                "status": report.get("status"),
                "manifest_updated": False,
                "report_sha256": report_hash,
                "candidates": report.get("candidates", []),
                "next_action": "Select only reviewed unique-pair labels; use --merged-through only after independent human adjudication.",
            }, indent=2, ensure_ascii=False))
            print("DRY RUN: manifest unchanged; offsets and steps remain unmeasured.")
            return 0
        entries, merged_entries = candidate_break_entries(
            report, args.select, args.merged_through, reviewed_by=getpass.getuser(),
            review_note=(args.review_note or "preview only"), report_sha256=report_hash)
        preview = {"identity": identity, "apply": bool(args.apply),
                   "selected_labels": list(args.select),
                   "merged_music_through": merged_entries,
                   "manifest_updates": entries, "candidate_report_sha256": report_hash,
                   "manifest_sha256": report.get("manifest_sha256")}
        if not args.apply:
            print(json.dumps(preview, indent=2, ensure_ascii=False))
            print("DRY RUN: manifest unchanged; review candidates and adjudicate offsets separately.")
            return 0

        inputs_gate = subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_episode.py"), "--episode", identity,
             "--stage", "inputs"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=os.environ.copy())
        print(inputs_gate.stdout, end="")
        if inputs_gate.returncode:
            print(inputs_gate.stderr, file=sys.stderr)
            print("INPUTS GATE FAILED; no manifest candidate was recorded.", file=sys.stderr)
            return inputs_gate.returncode

        backup, manifest_path, _ = apply_candidates(
            report, candidate_path, ep, ld, episode_manifest_path(ep.code, ld, series=ep.series),
            args.select, args.merged_through, args.review_note.strip(), report_hash)
        validate = subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_episode.py"), "--episode", identity,
             "--stage", "manifest"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=os.environ.copy())
        print(f"recorded {len(entries)} break candidate(s) and {len(merged_entries)} "
              f"reviewed merged interval(s) in {manifest_path}")
        print(f"verified backup: {backup}")
        print(validate.stdout, end="")
        if validate.returncode:
            print(validate.stderr, file=sys.stderr)
            print("POST-WRITE VALIDATION FAILED; preserve the manifest and backup, stop before retime.",
                  file=sys.stderr)
            return validate.returncode
        print("Offsets/steps remain null; measure/adjudicate them before retime.")
        return 0
    except (OSError, json.JSONDecodeError, ValueError, LockError, PipelineError) as exc:
        print(f"RETIME BREAK RECORDER ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
