#!/usr/bin/env python3
"""Record already-adjudicated output-frame head/tail trims with visual evidence hashes."""
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

from fileutil import sha256_file  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from manifest_io import atomic_replace, exclusive_backup, manifest_lock  # noqa: E402
from validate_episode import Checks, validate_efm_capture_provenance  # noqa: E402


def _resolve_probe_marker(raw: str, probe: Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise PipelineError("visual boundary review asset may not be a symbolic link")
    if not path.is_absolute():
        if not path.parts or path.parts[0].casefold() != "probe" or ".." in path.parts:
            raise PipelineError("visual boundary review asset path is unsafe")
        path = probe / Path(*path.parts[1:])
    path = path.resolve(strict=True)
    try:
        path.relative_to(probe.resolve(strict=True))
    except ValueError as exc:
        raise PipelineError("visual boundary review assets must remain in this episode's probe root") from exc
    if path.is_symlink() or not path.is_file():
        raise PipelineError("visual boundary review index is missing or unsafe")
    return path


def record_trim_review(ep: Any, ld: Ld, *, head_trim: int, tail_trim: int,
                       review_note: str, expected_manifest_sha256: str,
                       expected_source_sha256: str, expected_audio_sha256: str,
                       review_assets_path: Path | None, expected_review_assets_sha256: str,
                       report_out: Path, replace_existing: bool = False) -> dict[str, Any]:
    if type(head_trim) is not int or head_trim < 0 or type(tail_trim) is not int or tail_trim < 0:
        raise PipelineError("head_trim/tail_trim must be nonnegative output-frame counts")
    if len(review_note.strip()) < 8:
        raise PipelineError("a visual trim review note of at least 8 characters is required")
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    probe = ld.probe().resolve(strict=True)
    source = ep.path("stack_master").resolve(strict=True)
    native_audio = ep.path("ld_native").resolve(strict=True)
    if source.is_symlink() or native_audio.is_symlink():
        raise PipelineError("trim review source video/audio may not be symlinks")
    if (sha256_file(manifest_path) != expected_manifest_sha256 or
            sha256_file(source) != expected_source_sha256 or
            sha256_file(native_audio) != expected_audio_sha256):
        raise PipelineError("manifest or source evidence changed after visual trim review")
    audio = ep.data.get("audio") or {}
    pad_frames = audio.get("pad_frames")
    if type(pad_frames) is not int or pad_frames < 0 or not audio.get("capture"):
        raise PipelineError("trim review requires verified capture and measured audio.pad_frames")
    selected = str(audio.get("capture"))
    capture = next((row for row in ep.data.get("captures", [])
                    if str(row.get("label")) == selected), None)
    if (audio.get("efm_provenance_required") is not True or capture is None or
            not isinstance(audio.get("capture_selection"), dict)):
        raise PipelineError("trim review requires the explicitly selected, provenance-verified EFM capture")
    provenance_checks = Checks()
    validate_efm_capture_provenance(provenance_checks, ep, ld, capture, native_audio)
    if provenance_checks.failed:
        failures = "; ".join(name for status, name, _detail in provenance_checks.rows if status == "FAIL")
        raise PipelineError(f"selected EFM provenance is no longer current: {failures}")
    provenance_entry = (audio.get("efm_provenance") or {}).get(selected, [])[-1]

    dvd_available = ep.data.get("dvd_reference_available") is True
    alignment_sha = None
    assets_hash = None
    assets_path = None
    alignment_path = None
    dvd_path = None
    dvd_hash = None
    if dvd_available:
        video = ep.data.get("video") or {}
        alignment_review = video.get("dvd_alignment_review") or {}
        alignment_path = ep.path("align_npy").resolve(strict=True)
        dvd_path = ep.path("dvd").resolve(strict=True)
        if (alignment_review.get("status") != "APPROVED" or
                sha256_file(alignment_path) != alignment_review.get("sha256")):
            raise PipelineError("DVD-backed trim review requires the current visually approved DVD/LD map")
        alignment_sha = alignment_review["sha256"]
        dvd_hash = sha256_file(dvd_path)
        if review_assets_path is None:
            raise PipelineError("DVD-backed trim review requires paired start/end visual samples")
        assets_path = _resolve_probe_marker(str(review_assets_path), probe)
        assets_hash = sha256_file(assets_path)
        if assets_hash != expected_review_assets_sha256 or assets_hash != alignment_review.get("review_assets_sha256"):
            raise PipelineError("paired boundary samples changed after visual review")
        try:
            assets = json.loads(assets_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"cannot read paired boundary samples: {exc}") from exc
        dvd = ep.path("dvd")
        if (assets.get("schema") != "dvd-alignment-review-assets-v1" or
                assets.get("identity") != ep.identity or
                assets.get("manifest_sha256") != expected_manifest_sha256 or
                assets.get("alignment_sha256") != alignment_review.get("sha256") or
                assets.get("master_source_sha256") != expected_source_sha256 or
                assets.get("dvd_source_sha256") != dvd_hash):
            raise PipelineError("paired boundary samples do not match current manifest/source/alignment evidence")
        reasons = {reason for sample in assets.get("review_points", [])
                   for reason in sample.get("reasons", [])}
        if not {"measured DVD-origin neighborhood", "measured content-end neighborhood"}.issubset(reasons):
            raise PipelineError("visual review assets do not contain both measured start/end boundary neighborhoods")
        for sample in assets.get("review_points", []):
            if sample.get("asset_status") != "READY":
                continue
            gif = Path(str(sample.get("playback_path") or ""))
            if (gif.is_symlink() or not gif.is_file() or
                    sha256_file(gif) != sample.get("playback_sha256")):
                raise PipelineError("paired boundary playback sample changed after visual review")
    elif ep.data.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("no-DVD trim review requires the explicit no-DVD route approval")

    report_out = report_out.expanduser().resolve(strict=False)
    report_out = assert_writable_path(report_out, ld)
    if report_out.exists() or report_out.is_symlink():
        raise PipelineError(f"refusing to overwrite trim review record: {report_out}")

    lock_path = manifest_path.with_name(manifest_path.name + ".trim-review.lock")
    assert_writable_path(lock_path, ld)
    with manifest_lock(lock_path):
        original = manifest_path.read_bytes()
        if sha256_file(manifest_path) != expected_manifest_sha256:
            raise PipelineError("manifest changed while trim review was pending")
        manifest = json.loads(original.decode("utf-8-sig"))
        video = manifest.setdefault("video", {})
        if (video.get("head_trim") is not None or video.get("tail_trim") is not None) and not replace_existing:
            raise PipelineError("existing trim values require explicit replacement confirmation")
        video["head_trim"] = head_trim
        video["tail_trim"] = tail_trim
        video["trim_review"] = {
            "status": "APPROVED", "head_trim": head_trim, "tail_trim": tail_trim,
            "frame_domain": "23.976 output frames; values entered after visual adjudication",
            "audio_pad_frames": pad_frames,
            "capture": selected,
            "efm_provenance_sha256": provenance_entry.get("report_sha256"),
            "stack_master_sha256": expected_source_sha256,
            "native_audio_sha256": expected_audio_sha256,
            "alignment_sha256": alignment_sha,
            "review_assets_path": (
                "probe/" + assets_path.relative_to(probe).as_posix() if assets_path else None),
            "review_assets_sha256": assets_hash,
            "reviewed_by": getpass.getuser(),
            "review_note": review_note.strip(),
            "reviewed_utc": datetime.now(timezone.utc).isoformat(),
        }
        if (sha256_file(source) != expected_source_sha256 or
                sha256_file(native_audio) != expected_audio_sha256 or
                sha256_file(manifest_path) != expected_manifest_sha256):
            raise PipelineError("source/manifest changed before trim approval")
        if dvd_available and (
                sha256_file(alignment_path) != alignment_sha or
                sha256_file(dvd_path) != dvd_hash or
                sha256_file(assets_path) != assets_hash):
            raise PipelineError("DVD/LD alignment or paired boundary assets changed before trim approval")
        backup = exclusive_backup(manifest_path, original,
                                  label="visual-trim-review", error_type=PipelineError)
        atomic_replace(
            manifest_path,
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            original, error_type=PipelineError,
            changed_message="manifest changed while recording trim review; original preserved",
            pre_replace_message="manifest changed before trim review commit; original preserved")
    result = {
        "schema": "visual-trim-review-v1", "identity": ep.identity,
        "head_trim": head_trim, "tail_trim": tail_trim, "pad_frames": pad_frames,
        "manifest_sha256_before": expected_manifest_sha256,
        "manifest_sha256_after": sha256_file(manifest_path),
        "manifest_backup": str(backup), "manifest_updated": True,
        "alignment_sha256": alignment_sha, "review_assets_sha256": assets_hash,
        "review_note": review_note.strip(), "reviewed_by": getpass.getuser(),
    }
    report_out.parent.mkdir(parents=True, exist_ok=True)
    with report_out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--head-trim", type=int, required=True)
    parser.add_argument("--tail-trim", type=int, required=True)
    parser.add_argument("--review-note", required=True)
    parser.add_argument("--review-assets", default="")
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--confirm-source-sha256", required=True)
    parser.add_argument("--confirm-audio-sha256", required=True)
    parser.add_argument("--confirm-review-assets-sha256", default="")
    parser.add_argument("--report-out", required=True)
    parser.add_argument("--replace-existing-trims", action="store_true")
    parser.add_argument("--confirm-visual-review", action="store_true")
    args = parser.parse_args()
    if not args.confirm_visual_review:
        raise PipelineError("recording trims requires the explicit GUI visual-review confirmation")
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    result = record_trim_review(
        ep, ld, head_trim=args.head_trim, tail_trim=args.tail_trim,
        review_note=args.review_note, expected_manifest_sha256=args.confirm_manifest_sha256,
        expected_source_sha256=args.confirm_source_sha256,
        expected_audio_sha256=args.confirm_audio_sha256,
        review_assets_path=Path(args.review_assets) if args.review_assets else None,
        expected_review_assets_sha256=args.confirm_review_assets_sha256,
        report_out=Path(args.report_out), replace_existing=args.replace_existing_trims)
    print(f"visual output-frame trims recorded with backup {result['manifest_backup']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError, PipelineError) as exc:
        print(f"VIDEO TRIM REVIEW ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
