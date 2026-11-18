#!/usr/bin/env python3
"""Persist the exact alignment map and frame-coverage total from a saved Orc report."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from ldcommon import (Ld, PipelineError, assert_writable_path,
                      episode_manifest_path, load_episode)  # noqa: E402
from manifest_io import atomic_replace, exclusive_backup, manifest_lock  # noqa: E402
from workflow_app_model import WorkflowAppError, parse_verified_orc_reference_report  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--report", required=True, help="fingerprint-verified saved Orc report")
    parser.add_argument("--expected-manifest-sha256", required=True)
    return parser


def record_measurements(episode: str, report_path: Path,
                        expected_manifest_sha256: str) -> tuple[Path, Path | None]:
    ld = Ld()
    ep = load_episode(episode, ld)
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series)
    assert_writable_path(manifest_path, ld)
    if report_path.is_symlink() or not report_path.is_file():
        raise PipelineError(f"Orc report is missing or unsafe: {report_path}")
    probe_root = ld.root("probe").resolve(strict=False)
    logs_root = ld.root("logs").resolve(strict=False)
    if not any(_is_relative_to(report_path.resolve(strict=True), root)
               for root in (probe_root, logs_root)):
        raise PipelineError("Orc report must remain inside this episode's probe or logs folder")

    original = manifest_path.read_bytes()
    actual_sha256 = hashlib.sha256(original).hexdigest()
    if actual_sha256 != expected_manifest_sha256:
        raise PipelineError("episode manifest changed after the automatic continuation plan was built")
    try:
        manifest = json.loads(original.decode("utf-8-sig"))
        measurements = parse_verified_orc_reference_report(
            report_path.read_text(encoding="utf-8-sig"),
            ep.capture_set, ep.reference)
    except (UnicodeDecodeError, json.JSONDecodeError, WorkflowAppError) as exc:
        raise PipelineError(f"cannot use the saved Orc reference evidence: {exc}") from exc

    current_map = str(manifest.get("alignment_map") or "").strip()
    if current_map and current_map != measurements["alignment_map"]:
        raise PipelineError("manifest alignment_map conflicts with the verified Orc report")
    stack = manifest.get("stack")
    if not isinstance(stack, dict):
        raise PipelineError("manifest stack section is missing or invalid")
    current_frames = stack.get("frames")
    if current_frames not in (None, ""):
        try:
            current_frames = int(current_frames)
            if current_frames < 0 or (current_frames > 0 and
                                      current_frames != measurements["stack_frames"]):
                raise PipelineError("manifest stack.frames conflicts with the verified Orc report")
        except (TypeError, ValueError) as exc:
            raise PipelineError("manifest stack.frames is not a valid integer") from exc

    manifest["alignment_map"] = measurements["alignment_map"]
    stack["frames"] = measurements["stack_frames"]
    updated = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if updated == original:
        print(f"Orc measurements already recorded: alignment_map; stack.frames={measurements['stack_frames']}")
        return manifest_path, None

    lock_path = manifest_path.with_name(manifest_path.name + ".orc-measurements.lock")
    assert_writable_path(lock_path, ld)
    with manifest_lock(lock_path):
        if manifest_path.is_symlink() or manifest_path.read_bytes() != original:
            raise PipelineError("manifest changed before the Orc measurement update could be committed")
        backup = exclusive_backup(
            manifest_path, original, label="orc-measurements", error_type=PipelineError)
        atomic_replace(
            manifest_path, updated, original, error_type=PipelineError,
            changed_message="manifest changed while recording Orc measurements",
            pre_replace_message="manifest changed before Orc measurement commit")
    print(f"recorded verified Orc measurements: alignment_map; stack.frames={measurements['stack_frames']}")
    print(f"manifest backup: {backup}")
    return manifest_path, backup


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if len(args.expected_manifest_sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF"
            for character in args.expected_manifest_sha256):
        parser.error("--expected-manifest-sha256 must be a SHA-256 hex digest")
    try:
        report_path = Path(args.report).expanduser()
        record_measurements(args.episode, report_path, args.expected_manifest_sha256.lower())
        return 0
    except (OSError, PipelineError, RuntimeError, ValueError) as exc:
        print(f"ORC MEASUREMENT RECORD ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
