#!/usr/bin/env python3
"""Emit machine-readable cadence measurements from a verified full-side TFM log."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from cadence_analysis import analyze_tfm_rows, load_motion_trace, parse_tfm_log  # noqa: E402
from fileutil import sha256_file  # noqa: E402
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402


def build_report(identity: str, verification_path: Path, tfm_log: Path,
                 motion: Path, motion_report_path: Path,
                 map_script: Path, di_map: Path,
                 manifest: Path, out: Path) -> dict[str, Any]:
    verification = json.loads(verification_path.read_text(encoding="utf-8-sig"))
    if (verification.get("schema") != "full-side-cadence-evidence-v1" or
            verification.get("identity") != identity or
            verification.get("status") != "VERIFIED" or
            verification.get("candidate_is_approval") is not False):
        raise PipelineError("cadence analysis requires a VERIFIED full-side TFM coverage report")
    manifest_hash = sha256_file(manifest)
    if manifest_hash != verification.get("manifest_sha256"):
        raise PipelineError("manifest changed after full-side TFM evidence generation")
    source = Path(str(verification.get("stack_master") or "")).resolve(strict=True)
    if sha256_file(source) != verification.get("stack_master_sha256"):
        raise PipelineError("stack master changed after full-side TFM evidence generation")
    native_audio = Path(str(verification.get("native_audio") or "")).resolve(strict=True)
    if sha256_file(native_audio) != verification.get("native_audio_sha256"):
        raise PipelineError("native audio changed after trim review/cadence evidence generation")
    if (sha256_file(tfm_log) != verification.get("tfm_log_sha256") or
            tfm_log.resolve(strict=True) != Path(str(verification.get("tfm_log_path"))).resolve(strict=True)):
        raise PipelineError("TFM log changed or differs from the verified coverage report")
    expected_frames = int(verification["expected_tfm_frames"])
    rows = parse_tfm_log(tfm_log.read_text(encoding="utf-8-sig"), expected_frames)
    motion_values = load_motion_trace(str(motion))
    try:
        motion_report = json.loads(motion_report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read motion verification report: {exc}") from exc
    if (motion_report.get("schema") != "motion-trace-evidence-v1" or
            motion_report.get("status") != "VERIFIED" or
            motion_report.get("identity") != identity or
            motion_report.get("manifest_sha256") != manifest_hash or
            motion_report.get("source_sha256") != verification.get("stack_master_sha256") or
            motion_report.get("audio_pad_frames") != verification.get("audio_pad_frames") or
            motion_report.get("motion_trace_sha256") != sha256_file(motion)):
        raise PipelineError("motion trace is not verified against the same manifest/source/pad frame domain")
    if (motion_report.get("source_frames") != verification.get("stack_frames_decoded") or
            motion_report.get("aligned_frames") != expected_frames):
        raise PipelineError("motion trace frame count differs from the verified TFM source domain")
    if len(motion_values) not in (expected_frames - 1, expected_frames):
        raise PipelineError(
            f"motion trace has {len(motion_values)} values; expected {expected_frames - 1} "
            f"(N-1) or {expected_frames}")
    metrics = analyze_tfm_rows(rows, motion_values=motion_values)
    source_rate_info = {
        "tfm": verification.get("ffprobe_source_stream") or {},
        "motion": motion_report.get("ffprobe_source_stream") or {},
    }
    inputs = {
        "manifest_path": str(manifest.resolve(strict=True)),
        "manifest_sha256": manifest_hash,
        "trim_review_sha256": verification.get("trim_review_sha256"),
        "stack_master_path": str(source.resolve(strict=True)),
        "stack_master_sha256": verification["stack_master_sha256"],
        "native_audio_path": str(native_audio),
        "native_audio_sha256": verification["native_audio_sha256"],
        "tfm_log_path": str(tfm_log.resolve(strict=True)),
        "tfm_log_sha256": sha256_file(tfm_log),
        "verification_report_path": str(verification_path.resolve(strict=True)),
        "verification_report_sha256": sha256_file(verification_path),
        "motion_trace_path": str(motion.resolve(strict=True)),
        "motion_trace_sha256": sha256_file(motion),
        "motion_report_path": str(motion_report_path.resolve(strict=True)),
        "motion_report_sha256": sha256_file(motion_report_path),
        "map_script_path": str(map_script.resolve(strict=True)),
        "map_script_sha256": sha256_file(map_script),
        "di_map_path": str(di_map.resolve(strict=True)),
        "di_map_sha256": sha256_file(di_map),
        "expected_tfm_frames": expected_frames,
        "observed_tfm_frames": len(rows),
        "observed_motion_values": len(motion_values),
    }
    if not out.is_absolute():
        out = out.resolve(strict=False)
    if out.exists() or out.is_symlink():
        raise PipelineError(f"refusing to overwrite cadence measurement report: {out}")
    report = {
        "schema": "cadence-measurements-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "identity": identity, "status": "CANDIDATE_ONLY",
        "candidate_is_approval": False,
        "source_frame_domain": "29.97 fps after measured audio.pad_frames trim",
        "output_trim_domain": "head_trim/tail_trim are independently adjudicated 23.976 output frames",
        "parameters": {
            "tfm_period5_definition": "match_code[i] == match_code[i-5]",
            "tfm_mic_combed_threshold": 80,
            "repair_mic_threshold": 65,
            "insert_window_frames": 90,
            "insert_window_step_frames": 15,
            "insert_min_combed_frames": 40,
            "native_30p_check_motion_mean_min": 15.0,
            "native_30p_check_telecine_activity_max_pct": 12.0,
        },
        "source_rate_info": source_rate_info,
        "inputs": inputs,
        **metrics,
        "manifest_updated": False,
        "caveat": ("Measurements are from a complete, contiguous TFM log. Review all flagged intervals "
                   "visually before recording; this report approves no cadence map."),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--tfm-log", required=True)
    parser.add_argument("--motion", required=True)
    parser.add_argument("--motion-report", required=True)
    parser.add_argument("--verification-report", required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--di-map", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    from ldcommon import episode_manifest_path
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    out = assert_writable_path(Path(args.out), ld)
    try:
        out.resolve(strict=False).relative_to(ld.probe().resolve(strict=True))
    except ValueError as exc:
        raise PipelineError("cadence metrics output must remain under the episode probe root") from exc
    build_report(ep.identity, Path(args.verification_report), Path(args.tfm_log),
                 Path(args.motion), Path(args.motion_report), Path(args.map),
                 Path(args.di_map), manifest_path, out)
    print(f"candidate-only cadence metrics: {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, PipelineError) as exc:
        print(f"CADENCE ANALYSIS ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
