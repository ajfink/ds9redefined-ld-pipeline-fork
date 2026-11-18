#!/usr/bin/env python3
"""Record cadence metrics only after full-log verification and explicit visual review."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fileutil import sha256_file  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from manifest_io import atomic_replace, exclusive_backup, manifest_lock  # noqa: E402
from validate_episode import Checks, validate_efm_capture_provenance  # noqa: E402


def _digest_object(value: dict[str, Any]) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _probe_file(raw: str, probe: Path, label: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise PipelineError(f"{label} may not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(probe.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"{label} must be an existing regular file in the probe root") from exc
    if not resolved.is_file():
        raise PipelineError(f"{label} is not a regular file")
    return resolved


def _read_cadence_evidence(ep: Any, index_path: Path, expected_index_sha256: str) -> tuple[dict, dict, dict]:
    probe = ep.ld.probe().resolve(strict=True)
    index_path = _probe_file(str(index_path), probe, "visual cadence index")
    if sha256_file(index_path) != expected_index_sha256:
        raise PipelineError("visual cadence index changed after adjudication")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read cadence visual index: {exc}") from exc
    if (index.get("schema") != "cadence-review-assets-v1" or
            index.get("identity") != ep.identity or
            index.get("candidate_is_approval") is not False):
        raise PipelineError("cadence index is not candidate-only evidence for this episode")
    metrics_path = _probe_file(str(index.get("metrics_path") or ""), probe, "cadence metrics report")
    verification_path = _probe_file(
        str(index.get("verification_report_path") or ""), probe, "TFM coverage report")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8-sig"))
    verification = json.loads(verification_path.read_text(encoding="utf-8-sig"))
    if (metrics.get("schema") != "cadence-measurements-v1" or
            metrics.get("identity") != ep.identity or
            metrics.get("candidate_is_approval") is not False or
            verification.get("schema") != "full-side-cadence-evidence-v1" or
            verification.get("status") != "VERIFIED" or
            verification.get("identity") != ep.identity):
        raise PipelineError("cadence metrics or coverage report is not a valid candidate evidence set")
    inputs = metrics.get("inputs") or {}
    if Path(str(inputs.get("verification_report_path") or "")).resolve(strict=True) != verification_path:
        raise PipelineError("metrics report points at a different TFM coverage verification report")
    artifacts = {
        "metrics_path": metrics_path, "verification_path": verification_path,
        "tfm_log": _probe_file(str(inputs.get("tfm_log_path") or ""), probe, "TFM log"),
        "motion": _probe_file(str(inputs.get("motion_trace_path") or ""), probe, "motion trace"),
        "motion_report": _probe_file(
            str(index.get("motion_report_path") or ""), probe, "motion trace verification report"),
        "di_map": _probe_file(str(inputs.get("di_map_path") or ""), probe, "deinterlace map"),
        "map_script": Path(str(inputs.get("map_script_path") or "")).resolve(strict=True),
        "review_avs": Path(str(index.get("review_avs_path") or "")).resolve(strict=True),
        "source": ep.path("stack_master").resolve(strict=True),
        "native_audio": Path(str(index.get("native_audio_path") or "")).resolve(strict=True),
    }
    for key in ("metrics_path", "verification_path", "tfm_log", "motion", "motion_report", "di_map"):
        if not artifacts[key].is_file():
            raise PipelineError(f"cadence artifact is missing: {artifacts[key]}")
    for key, hash_key, label in (
            ("metrics_path", "metrics_sha256", "metrics"),
            ("verification_path", "verification_report_sha256", "coverage report"),
            ("review_avs", "review_avs_sha256", "review AviSynth script"),
            ("map_script", "map_script_sha256", "generated cadence map"),
            ("source", "source_sha256", "stack master"),
            ("native_audio", "native_audio_sha256", "native LD audio")):
        path = artifacts[key]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != index.get(hash_key):
            raise PipelineError(f"{label} changed after visual review")
    if (str(artifacts["native_audio"]) !=
            str(Path(str(metrics.get("inputs", {}).get("native_audio_path") or "")).resolve(strict=True)) or
            index.get("native_audio_sha256") != metrics.get("inputs", {}).get("native_audio_sha256")):
        raise PipelineError("native audio path/hash does not match the verified metrics input")
    for key, hash_key in (
            ("tfm_log", "tfm_log_sha256"), ("motion", "motion_trace_sha256"),
            ("di_map", "di_map_sha256")):
        if sha256_file(artifacts[key]) != inputs.get(hash_key):
            raise PipelineError(f"{key} changed after cadence analysis")
    comparisons = (
        ("verification_path", "verification_report_sha256", "verification_report_sha256"),
        ("motion_report", "motion_report_sha256", "motion_report_sha256"),
        ("map_script", "map_script_sha256", "map_script_sha256"),
        ("source", "source_sha256", "stack_master_sha256"),
        ("native_audio", "native_audio_sha256", "native_audio_sha256"),
    )
    for artifact_key, index_key, metric_key in comparisons:
        digest = sha256_file(artifacts[artifact_key])
        if digest != index.get(index_key) or digest != inputs.get(metric_key):
            raise PipelineError(f"{artifact_key} digest is inconsistent across cadence evidence")
    for row in index.get("review_intervals", []):
        sample = _probe_file(str(row.get("playback_path") or ""), index_path.parent, "cadence GIF")
        if sha256_file(sample) != row.get("playback_sha256"):
            raise PipelineError("cadence playback sample changed after visual review")
    contact = index.get("contact_sheet")
    if contact:
        contact_path = _probe_file(str(contact), probe, "cadence contact sheet")
        if sha256_file(contact_path) != index.get("contact_sheet_sha256"):
            raise PipelineError("cadence contact sheet changed after visual review")
    return index, metrics, verification


def record_review(ep: Any, ld: Ld, index_path: Path, *, decision: str,
                  decisions: dict[str, str], review_note: str,
                  expected_manifest_sha256: str, expected_index_sha256: str,
                  report_out: Path, source_rate_acknowledged: bool = False) -> dict[str, Any]:
    decision = decision.upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise PipelineError("cadence decision must be APPROVED or REJECTED")
    if len(review_note.strip()) < 8:
        raise PipelineError("cadence review note must contain at least 8 characters")
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    probe = ld.probe().resolve(strict=True)
    index_path = _probe_file(str(index_path), probe, "cadence index")
    manifest_hash = sha256_file(manifest_path)
    if manifest_hash != expected_manifest_sha256:
        raise PipelineError("manifest changed since cadence review was opened")
    index, metrics, verification = _read_cadence_evidence(ep, index_path, expected_index_sha256)
    if (index.get("manifest_sha256") != expected_manifest_sha256 or
            metrics.get("inputs", {}).get("manifest_sha256") != expected_manifest_sha256):
        raise PipelineError("cadence evidence was generated from a different manifest revision")
    sample_ids = {str(row.get("id")) for row in index.get("review_intervals", [])}
    if (not set(decisions).issubset(sample_ids) or
            any(value not in ("accepted", "unresolved") for value in decisions.values())):
        raise PipelineError("cadence visual decisions contain an unknown interval/status")
    if decision == "APPROVED" and set(decisions) != sample_ids:
        raise PipelineError("cadence approval must cover every generated review sample")
    if decision == "APPROVED" and any(value != "accepted" for value in decisions.values()):
        raise PipelineError("cadence cannot be approved while any visual interval is unresolved")
    if decision == "APPROVED" and not sample_ids:
        raise PipelineError("cadence approval requires at least one visual sample")
    rate_info = index.get("source_rate_info") or {}
    rate_mismatch = False
    for role in ("tfm", "motion"):
        row = rate_info.get(role) or {}
        if (row.get("r_frame_rate") and row.get("avg_frame_rate") and
                row.get("r_frame_rate") != row.get("avg_frame_rate")):
            rate_mismatch = True
    if ((rate_info.get("tfm") or {}).get("r_frame_rate") and
            (rate_info.get("motion") or {}).get("r_frame_rate") and
            (rate_info.get("tfm") or {}).get("r_frame_rate") !=
            (rate_info.get("motion") or {}).get("r_frame_rate")):
        rate_mismatch = True
    if decision == "APPROVED" and rate_mismatch and source_rate_acknowledged is not True:
        raise PipelineError("native source frame-rate mismatch was not explicitly acknowledged")
    report_out = Path(report_out).expanduser()
    if report_out.is_symlink():
        raise PipelineError("cadence review report may not be a symlink")
    report_out = report_out.resolve(strict=False)
    try:
        report_out.relative_to(probe)
    except ValueError as exc:
        raise PipelineError("cadence review report must be under the episode probe root") from exc
    assert_writable_path(report_out, ld)
    if report_out.exists():
        raise PipelineError(f"refusing to overwrite cadence review report: {report_out}")
    record = {
        "schema": "cadence-visual-review-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "identity": ep.identity, "decision": decision,
        "reviewed_by": getpass.getuser(), "review_note": review_note.strip(),
        "decisions": decisions, "manifest_sha256_before": manifest_hash,
        "source_rate_acknowledged": source_rate_acknowledged,
        "cadence_index_sha256": expected_index_sha256,
        "metrics_sha256": sha256_file(Path(index["metrics_path"])),
        "verification_sha256": sha256_file(Path(index["verification_report_path"])),
        "manifest_updated": False,
    }
    if decision == "APPROVED":
        trim_review = (ep.data.get("video") or {}).get("trim_review") or {}
        if trim_review.get("status") != "APPROVED":
            raise PipelineError("visual trim review is missing or no longer approved")
        audio = ep.data.get("audio") or {}
        if (type(audio.get("pad_frames")) is not int or
                audio.get("pad_frames") != trim_review.get("audio_pad_frames") or
                sha256_file(Path(str(metrics.get("inputs", {}).get("native_audio_path")))) !=
                trim_review.get("native_audio_sha256")):
            raise PipelineError("audio pad/source changed after visual trim adjudication")
        selected = str(audio.get("capture") or "")
        capture = next((row for row in ep.data.get("captures", [])
                        if str(row.get("label")) == selected), None)
        if (not selected or capture is None or trim_review.get("capture") != selected):
            raise PipelineError("cadence review requires the same verified selected EFM capture as trim review")
        provenance_checks = Checks()
        validate_efm_capture_provenance(
            provenance_checks, ep, ld, capture, Path(str(metrics["inputs"]["native_audio_path"])))
        if provenance_checks.failed:
            raise PipelineError("selected EFM provenance changed after trim/cadence review")
        trim_digest = _digest_object(trim_review)
        if metrics.get("inputs", {}).get("trim_review_sha256") != trim_digest:
            raise PipelineError("cadence metrics were generated from a different visual trim review")
        lock_path = manifest_path.with_name(manifest_path.name + ".cadence-review.lock")
        assert_writable_path(lock_path, ld)
        with manifest_lock(lock_path):
            original = manifest_path.read_bytes()
            if sha256_file(manifest_path) != expected_manifest_sha256:
                raise PipelineError("manifest changed while cadence approval was pending")
            current_index, current_metrics, current_verification = _read_cadence_evidence(
                ep, index_path, expected_index_sha256)
            if (current_metrics.get("inputs", {}).get("trim_review_sha256") != trim_digest or
                    current_index.get("map_script_sha256") != index.get("map_script_sha256") or
                    current_verification.get("status") != "VERIFIED"):
                raise PipelineError("cadence evidence changed during visual review")
            manifest = json.loads(original.decode("utf-8-sig"))
            video = manifest.setdefault("video", {})
            values = {
                "tfm_period5_pct": current_metrics["period5_agreement_pct"],
                "tfm_unmatched": current_metrics["unmatched_count"],
                "tfm_combed_count": current_metrics["combed_count"],
                "tfm_combed_spans": current_metrics["combed_spans"],
                "inserts": current_metrics["insert_count"],
                "repair_frames": current_metrics["repair_frame_count"],
                "repair_pct": current_metrics["repair_frame_pct"],
            }
            video.update(values)
            map_script = Path(index["map_script_path"]).resolve(strict=True)
            report_metrics = Path(index["metrics_path"]).resolve(strict=True)
            assets_dir = index_path.parent.resolve(strict=True)
            tfm_log = Path(current_metrics["inputs"]["tfm_log_path"]).resolve(strict=True)
            motion = Path(current_metrics["inputs"]["motion_trace_path"]).resolve(strict=True)
            di_map = Path(current_metrics["inputs"]["di_map_path"]).resolve(strict=True)
            summary_file = Path(str(index.get("legacy_summary_path") or "")).resolve(strict=True)
            files = manifest.setdefault("files", {})
            def relative_root_path(path: Path, root_name: str) -> dict[str, str]:
                base = ld.root(root_name).resolve(strict=True)
                return {"root": root_name, "name": path.relative_to(base).as_posix()}
            files["avs_encode"] = relative_root_path(map_script, "avs")
            files["tfm_log"] = relative_root_path(tfm_log, "probe")
            files["motion"] = relative_root_path(motion, "probe")
            files["di_map"] = relative_root_path(di_map, "probe")
            files["cadence_summary"] = relative_root_path(summary_file, "probe")
            video["cadence_review"] = {
                "status": "APPROVED",
                "trim_review_sha256": trim_digest,
                "metrics_path": f"probe/{report_metrics.relative_to(probe).as_posix()}",
                "metrics_sha256": sha256_file(report_metrics),
                "review_assets_path": f"probe/{assets_dir.relative_to(probe).as_posix()}/index.json",
                "review_assets_sha256": expected_index_sha256,
                "tfm_log_path": f"probe/{tfm_log.relative_to(probe).as_posix()}",
                "tfm_log_sha256": sha256_file(tfm_log),
                "motion_trace_path": f"probe/{motion.relative_to(probe).as_posix()}",
                "motion_trace_sha256": sha256_file(motion),
                "motion_report_path": f"probe/{Path(current_metrics['inputs']['motion_report_path']).resolve(strict=True).relative_to(probe).as_posix()}",
                "motion_report_sha256": sha256_file(Path(current_metrics["inputs"]["motion_report_path"])),
                "di_map_path": f"probe/{di_map.relative_to(probe).as_posix()}",
                "di_map_sha256": sha256_file(di_map),
                "verification_report_path": f"probe/{Path(current_metrics['inputs']['verification_report_path']).resolve(strict=True).relative_to(probe).as_posix()}",
                "verification_report_sha256": sha256_file(Path(current_metrics["inputs"]["verification_report_path"])),
                "map_script_path": f"avs/{map_script.relative_to(ld.root('avs').resolve(strict=True)).as_posix()}",
                "map_script_sha256": sha256_file(map_script),
                "verified_frame_count": current_metrics["frame_count"],
                "reviewed_by": record["reviewed_by"],
                "review_note": record["review_note"],
                "reviewed_utc": record["created_utc"],
                "interval_decisions": decisions,
                "source_rate_acknowledged": source_rate_acknowledged,
            }
            backup = exclusive_backup(
                manifest_path, original, label="cadence-visual-review",
                error_type=PipelineError)
            atomic_replace(
                manifest_path,
                (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                original, error_type=PipelineError,
                changed_message="manifest changed during cadence approval; original preserved",
                pre_replace_message="manifest changed before cadence approval; original preserved")
            record["manifest_updated"] = True
            record["manifest_sha256_after"] = sha256_file(manifest_path)
            record["manifest_backup"] = str(backup)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    with report_out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--review-index", required=True)
    parser.add_argument("--decision", choices=("APPROVED", "REJECTED"), required=True)
    parser.add_argument("--reviewed-interval", action="append", default=[])
    parser.add_argument("--unresolved-interval", action="append", default=[])
    parser.add_argument("--review-note", required=True)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--confirm-index-sha256", required=True)
    parser.add_argument("--report-out", required=True)
    parser.add_argument("--confirm-visual-review", action="store_true")
    parser.add_argument("--ack-source-rate-mismatch", action="store_true")
    args = parser.parse_args()
    decisions = {key: "accepted" for key in args.reviewed_interval}
    decisions.update({key: "unresolved" for key in args.unresolved_interval})
    if args.decision == "APPROVED" and not args.confirm_visual_review:
        raise PipelineError("cadence approval requires explicit confirmation of visual review")
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    result = record_review(
        ep, ld, Path(args.review_index), decision=args.decision, decisions=decisions,
        review_note=args.review_note, expected_manifest_sha256=args.confirm_manifest_sha256,
        expected_index_sha256=args.confirm_index_sha256, report_out=Path(args.report_out),
        source_rate_acknowledged=args.ack_source_rate_mismatch)
    print(f"cadence visual decision recorded: {args.decision}; manifest_updated={result['manifest_updated']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, PipelineError) as exc:
        print(f"CADENCE REVIEW ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
