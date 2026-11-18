#!/usr/bin/env python3
"""Record an explicit visual decision for one hash-bound DVD/LD candidate path."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from manifest_io import atomic_replace, exclusive_backup, manifest_lock  # noqa: E402
from fileutil import sha256_file  # noqa: E402


ASSET_SCHEMA = "dvd-alignment-review-assets-v1"


def _safe_probe_file(path: Path, probe: Path, label: str) -> Path:
    if path.is_symlink():
        raise PipelineError(f"{label} may not be a symbolic link: {path}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(probe.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"{label} must be an existing file in the episode probe folder") from exc
    if not resolved.is_file():
        raise PipelineError(f"{label} is not a regular file: {resolved}")
    return resolved


def _read_assets(index_path: Path, ep: Any, alignment: Path, diagnostics: Path,
                 expected_index_sha256: str) -> tuple[dict[str, Any], dict[str, str]]:
    probe = ep.ld.probe().resolve(strict=True)
    index_path = _safe_probe_file(index_path, probe, "review asset index")
    if sha256_file(index_path) != expected_index_sha256:
        raise PipelineError("review asset index changed after visual adjudication")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read review asset index: {exc}") from exc
    if (not isinstance(index, dict) or index.get("schema") != ASSET_SCHEMA or
            index.get("candidate_is_approval") is not False or
            index.get("identity") != ep.identity):
        raise PipelineError("review index is not a candidate-only asset set for this episode")
    manifest_path = episode_manifest_path(ep.code, ep.ld, series=ep.series).resolve(strict=True)
    if sha256_file(manifest_path) != index.get("manifest_sha256"):
        raise PipelineError("manifest changed since candidate alignment assets were generated")
    bound = {
        "alignment": _safe_probe_file(alignment, probe, "alignment candidate"),
        "diagnostics": _safe_probe_file(diagnostics, probe, "alignment diagnostics"),
        "assets": index_path,
    }
    expected = {
        "alignment": index.get("alignment_sha256"),
        "diagnostics": index.get("diagnostics_sha256"),
        "assets": expected_index_sha256,
    }
    if bound["alignment"] != Path(str(index.get("alignment_path", ""))).resolve(strict=True):
        raise PipelineError("review index names a different alignment candidate")
    if bound["diagnostics"] != Path(str(index.get("diagnostics_path", ""))).resolve(strict=True):
        raise PipelineError("review index names different alignment diagnostics")
    for key, path in bound.items():
        if sha256_file(path) != expected[key]:
            raise PipelineError(f"{key} changed after the review assets were built")

    source_keys = {
        "master_source": "stack_master", "dvd_source": "dvd",
    }
    for index_key, ep_key in source_keys.items():
        actual = ep.path(ep_key).resolve(strict=True)
        indexed = Path(str(index.get(index_key, ""))).resolve(strict=True)
        if actual != indexed or sha256_file(actual) != index.get(index_key + "_sha256"):
            raise PipelineError(f"{ep_key} changed or does not match the reviewed assets")
    descriptor_hashes = {}
    for key in ("master_descriptors", "master_descriptors_meta",
                "dvd_descriptors", "dvd_descriptors_meta"):
        source = _safe_probe_file(Path(str(index.get(key, ""))), probe, key)
        digest = sha256_file(source)
        if digest != index.get(key + "_sha256"):
            raise PipelineError(f"{key} changed after the visual review")
        descriptor_hashes[key] = digest

    sheet_value = index.get("contact_sheet")
    if sheet_value:
        sheet = _safe_probe_file(Path(str(sheet_value)), probe, "paired contact sheet")
        if sha256_file(sheet) != index.get("contact_sheet_sha256"):
            raise PipelineError("paired contact sheet changed after the visual review")
    ready_points = [row for row in index.get("review_points", [])
                    if isinstance(row, dict) and row.get("asset_status") == "READY"]
    if not ready_points:
        raise PipelineError("review index contains no verified paired-frame playback samples")
    for row in ready_points:
        gif = _safe_probe_file(Path(str(row.get("playback_path", ""))), probe, "paired playback sample")
        if sha256_file(gif) != row.get("playback_sha256"):
            raise PipelineError("paired playback sample changed after visual review")
    hashes = {
        "alignment_sha256": expected["alignment"],
        "diagnostics_sha256": expected["diagnostics"],
        "review_assets_sha256": expected["assets"],
        "master_source_sha256": index.get("master_source_sha256"),
        "dvd_source_sha256": index.get("dvd_source_sha256"),
        **descriptor_hashes,
    }
    return index, hashes


def record_review(ep: Any, ld: Ld, *, alignment: Path, diagnostics: Path,
                  assets_index: Path, decision: str, review_note: str,
                  expected_manifest_sha256: str, expected_alignment_sha256: str,
                  expected_diagnostics_sha256: str, expected_assets_sha256: str,
                  report_out: Path, replace_approved: bool = False,
                  source_rate_acknowledged: bool = False) -> dict[str, Any]:
    decision = decision.upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise PipelineError("visual decision must be APPROVED or REJECTED")
    if len(review_note.strip()) < 8:
        raise PipelineError("a human review note of at least 8 characters is required")
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    report_out = _safe_probe_output(report_out, ld.probe().resolve(strict=True), ld)
    if report_out.exists() or report_out.is_symlink():
        raise PipelineError(f"refusing to overwrite visual review record: {report_out}")
    original = manifest_path.read_bytes()
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise PipelineError("manifest changed since alignment review was prepared")
    index, hashes = _read_assets(assets_index, ep, alignment, diagnostics, expected_assets_sha256)
    if expected_manifest_sha256 != index.get("manifest_sha256"):
        raise PipelineError("review plan manifest hash differs from the candidate-generation manifest")
    if (hashes["alignment_sha256"] != expected_alignment_sha256 or
            hashes["diagnostics_sha256"] != expected_diagnostics_sha256):
        raise PipelineError("alignment or diagnostics hash differs from the reviewed candidate")
    descriptor_summary = index.get("descriptor_summary") or {}
    rate_mismatch = any(
        (descriptor_summary.get(role) or {}).get("ffprobe_r_frame_rate") and
        (descriptor_summary.get(role) or {}).get("ffprobe_avg_frame_rate") and
        (descriptor_summary.get(role) or {}).get("ffprobe_r_frame_rate") !=
        (descriptor_summary.get(role) or {}).get("ffprobe_avg_frame_rate")
        for role in ("master", "dvd"))
    if decision == "APPROVED" and rate_mismatch and source_rate_acknowledged is not True:
        raise PipelineError("source frame-rate mismatch was not explicitly acknowledged")
    record = {
        "schema": "dvd-alignment-visual-review-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "identity": ep.identity, "decision": decision,
        "reviewed_by": getpass.getuser(), "review_note": review_note.strip(),
        "candidate_is_approval": decision == "APPROVED",
        "manifest_sha256_before": expected_manifest_sha256,
        "inputs": hashes,
        "alignment_path": str(Path(alignment).resolve(strict=True)),
        "diagnostics_path": str(Path(diagnostics).resolve(strict=True)),
        "review_assets_path": str(Path(assets_index).resolve(strict=True)),
        "review_point_count": len(index.get("review_points") or []),
        "source_rate_acknowledged": source_rate_acknowledged,
        "manifest_updated": False,
    }
    if decision == "APPROVED":
        lock_path = manifest_path.with_name(manifest_path.name + ".dvd-alignment-review.lock")
        assert_writable_path(lock_path, ld)
        with manifest_lock(lock_path):
            current = manifest_path.read_bytes()
            if sha256_file(manifest_path) != expected_manifest_sha256:
                raise PipelineError("manifest changed during review; refusing approval")
            manifest = json.loads(current.decode("utf-8-sig"))
            if (manifest.get("episode") != ep.code or
                    str(manifest.get("series", "DS9")).upper() != ep.series or
                    manifest.get("identity", ep.identity) != ep.identity):
                raise PipelineError("manifest identity changed; refusing alignment approval")
            video = manifest.setdefault("video", {})
            previous = video.get("dvd_alignment_review") or {}
            if previous.get("status") == "APPROVED" and not replace_approved:
                raise PipelineError(
                    "an approved DVD/LD alignment already exists; explicit replacement approval is required")
            alignment_path = Path(alignment).resolve(strict=True)
            diagnostics_path = Path(diagnostics).resolve(strict=True)
            master_descriptors = Path(str(index["master_descriptors"])).resolve(strict=True)
            dvd_descriptors = Path(str(index["dvd_descriptors"])).resolve(strict=True)
            probe = ld.probe().resolve(strict=True)
            try:
                alignment_name = alignment_path.relative_to(probe).as_posix()
                diagnostics_name = diagnostics_path.relative_to(probe).as_posix()
                master_descriptors_name = master_descriptors.relative_to(probe).as_posix()
                dvd_descriptors_name = dvd_descriptors.relative_to(probe).as_posix()
            except ValueError as exc:
                raise PipelineError("approved alignment artifacts must remain in this probe root") from exc
            files = manifest.setdefault("files", {})
            files["align_npy"] = {"root": "probe", "name": alignment_name}
            files["alignment_report"] = {"root": "probe", "name": diagnostics_name}
            files["master_descriptors"] = {"root": "probe", "name": master_descriptors_name}
            files["dvd_descriptors"] = {"root": "probe", "name": dvd_descriptors_name}
            video["dvd_alignment_review"] = {
                "status": "APPROVED",
                "path": f"probe/{alignment_name}",
                "sha256": hashes["alignment_sha256"],
                "diagnostics_path": f"probe/{diagnostics_name}",
                "diagnostics_sha256": hashes["diagnostics_sha256"],
                "review_assets_path": f"probe/{Path(assets_index).resolve(strict=True).relative_to(probe).as_posix()}",
                "review_assets_sha256": hashes["review_assets_sha256"],
                "master_source_sha256": hashes["master_source_sha256"],
                "dvd_source_sha256": hashes["dvd_source_sha256"],
                "master_descriptors_sha256": hashes["master_descriptors"],
                "dvd_descriptors_sha256": hashes["dvd_descriptors"],
                "reviewed_by": record["reviewed_by"],
                "review_note": record["review_note"],
                "reviewed_utc": record["created_utc"],
                "source_rate_acknowledged": source_rate_acknowledged,
            }
            _read_assets(assets_index, ep, alignment, diagnostics, expected_assets_sha256)
            if sha256_file(manifest_path) != expected_manifest_sha256:
                raise PipelineError("manifest changed before backup; refusing alignment approval")
            backup = exclusive_backup(
                manifest_path, current, label="dvd-alignment-review",
                error_type=PipelineError)
            _read_assets(assets_index, ep, alignment, diagnostics, expected_assets_sha256)
            atomic_replace(
                manifest_path, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                current, error_type=PipelineError,
                changed_message="manifest changed during alignment approval; original preserved",
                pre_replace_message="manifest changed before atomic alignment approval; original preserved")
            record["manifest_updated"] = True
            record["manifest_sha256_after"] = sha256_file(manifest_path)
            record["manifest_backup"] = str(backup)
    if report_out.exists() or report_out.is_symlink():
        raise PipelineError(f"refusing to overwrite visual review record: {report_out}")
    report_out.parent.mkdir(parents=True, exist_ok=True)
    with report_out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record


def _safe_probe_output(path: Path, probe: Path, ld: Ld | None = None) -> Path:
    if path.is_symlink():
        raise PipelineError(f"review record output may not be a symbolic link: {path}")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(probe)
    except ValueError as exc:
        raise PipelineError("review record output must remain under this episode's probe root") from exc
    assert_writable_path(resolved, ld)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--alignment", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--assets-index", required=True)
    parser.add_argument("--decision", choices=("APPROVED", "REJECTED"), required=True)
    parser.add_argument("--review-note", required=True)
    parser.add_argument("--report-out", required=True)
    parser.add_argument("--confirm-manifest-sha256", default="")
    parser.add_argument("--confirm-alignment-sha256", default="")
    parser.add_argument("--confirm-diagnostics-sha256", default="")
    parser.add_argument("--confirm-assets-sha256", required=True)
    parser.add_argument("--replace-approved-alignment", action="store_true")
    parser.add_argument("--ack-source-rate-mismatch", action="store_true")
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    report_out = Path(args.report_out)
    if args.decision == "APPROVED":
        if not args.confirm_manifest_sha256:
            raise PipelineError("approval requires the exact reviewed manifest hash")
        record = record_review(
            ep, ld, alignment=Path(args.alignment), diagnostics=Path(args.report),
            assets_index=Path(args.assets_index), decision=args.decision,
            review_note=args.review_note,
            expected_manifest_sha256=args.confirm_manifest_sha256,
            expected_alignment_sha256=args.confirm_alignment_sha256,
            expected_diagnostics_sha256=args.confirm_diagnostics_sha256,
            expected_assets_sha256=args.confirm_assets_sha256,
            report_out=report_out, replace_approved=args.replace_approved_alignment,
            source_rate_acknowledged=args.ack_source_rate_mismatch)
    else:
        manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
        if len(args.review_note.strip()) < 8:
            raise PipelineError("a human review note of at least 8 characters is required")
        if sha256_file(manifest_path) != args.confirm_manifest_sha256:
            raise PipelineError("rejection audit requires the current reviewed manifest hash")
        index, hashes = _read_assets(
            Path(args.assets_index), ep, Path(args.alignment), Path(args.report),
            args.confirm_assets_sha256)
        if (hashes["alignment_sha256"] != args.confirm_alignment_sha256 or
                hashes["diagnostics_sha256"] != args.confirm_diagnostics_sha256):
            raise PipelineError("reviewed alignment inputs changed")
        record = {
            "schema": "dvd-alignment-visual-review-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
            "identity": ep.identity, "decision": "REJECTED",
            "reviewed_by": getpass.getuser(), "review_note": args.review_note.strip(),
            "candidate_is_approval": False, "manifest_updated": False,
            "manifest_sha256": sha256_file(manifest_path), "inputs": hashes,
            "alignment_path": str(Path(args.alignment).resolve(strict=True)),
            "diagnostics_path": str(Path(args.report).resolve(strict=True)),
            "review_assets_path": str(Path(args.assets_index).resolve(strict=True)),
            "review_point_count": len(index.get("review_points") or []),
            "source_rate_acknowledged": args.ack_source_rate_mismatch,
        }
        report_out = _safe_probe_output(report_out, ld.probe().resolve(strict=True), ld)
        if report_out.exists():
            raise PipelineError(f"refusing to overwrite visual review record: {report_out}")
        report_out.parent.mkdir(parents=True, exist_ok=True)
        with report_out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
    print(f"alignment review decision recorded: {args.decision}; manifest_updated={record['manifest_updated']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError, PipelineError) as exc:
        print(f"DVD ALIGNMENT REVIEW ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
