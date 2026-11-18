#!/usr/bin/env python3
"""Generate and fully decode a unique audio-pad-aligned TFM log; never update the manifest."""
from __future__ import annotations

import argparse
import json
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from cadence_analysis import parse_tfm_log  # noqa: E402
from fileutil import sha256_file  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from validate_episode import Checks, validate_efm_capture_provenance  # noqa: E402


def _avs_quote(path: Path) -> str:
    return str(path.resolve(strict=True)).replace("\\", "/").replace('"', '\\"')


def _approved_trim_review(ep: Any, master: Path, native_audio: Path) -> dict[str, Any]:
    video = ep.data.get("video") or {}
    head, tail = video.get("head_trim"), video.get("tail_trim")
    if (type(head) is not int or head < 0 or type(tail) is not int or tail < 0):
        raise PipelineError("cadence evidence requires measured video.head_trim and video.tail_trim")
    review = video.get("trim_review") or {}
    if (review.get("status") != "APPROVED" or review.get("head_trim") != head or
            review.get("tail_trim") != tail or
            review.get("frame_domain") != "23.976 output frames; values entered after visual adjudication" or
            len(str(review.get("review_note") or "").strip()) < 8):
        raise PipelineError(
            "cadence evidence requires a visual trim-review marker matching both measured output-frame trims")
    master_hash = sha256_file(master)
    if master_hash != review.get("stack_master_sha256"):
        raise PipelineError(
            "stack master changed after the visual head/tail trim review "
            f"(actual {master_hash}, reviewed {review.get('stack_master_sha256')})")
    audio_hash = sha256_file(native_audio)
    if audio_hash != review.get("native_audio_sha256"):
        raise PipelineError(
            "selected native audio changed after the visual head/tail trim review "
            f"(actual {audio_hash}, reviewed {review.get('native_audio_sha256')})")
    if review.get("audio_pad_frames") != (ep.data.get("audio") or {}).get("pad_frames"):
        raise PipelineError("audio.pad_frames changed after the visual trim review")
    audio = ep.data.get("audio") or {}
    selected = str(audio.get("capture") or "")
    provenance = (audio.get("efm_provenance") or {}).get(selected) or []
    if (not selected or review.get("capture") != selected or not provenance or
            provenance[-1].get("report_sha256") != review.get("efm_provenance_sha256")):
        raise PipelineError("trim review is not bound to the current selected Orc provenance")
    alignment_value = (ep.data.get("video") or {}).get("dvd_alignment_review") or {}
    if ep.data.get("dvd_reference_available") is True:
        if alignment_value.get("status") != "APPROVED":
            raise PipelineError("DVD-backed cadence requires a visually approved DVD/LD content map")
        alignment_path = Path(ep.path("align_npy"))
        if sha256_file(alignment_path) != alignment_value.get("sha256"):
            raise PipelineError("DVD/LD alignment changed after its visual review")
        if (sha256_file(master) != alignment_value.get("master_source_sha256") or
                sha256_file(ep.path("dvd")) != alignment_value.get("dvd_source_sha256")):
            raise PipelineError("DVD or master source changed after alignment visual review")
        if review.get("alignment_sha256") != alignment_value.get("sha256"):
            raise PipelineError("head/tail trim review is not bound to the currently approved alignment")
        raw_assets = Path(str(alignment_value.get("review_assets_path") or "")).expanduser()
        if raw_assets.is_symlink():
            raise PipelineError("approved alignment visual assets may not be a symbolic link")
        if not raw_assets.is_absolute():
            parts = raw_assets.parts
            if not parts or parts[0].casefold() != "probe" or ".." in parts:
                raise PipelineError("approved alignment visual-assets path is unsafe")
            raw_assets = ep.ld.probe() / Path(*parts[1:])
        if (sha256_file(raw_assets) != alignment_value.get("review_assets_sha256") or
                review.get("review_assets_sha256") != alignment_value.get("review_assets_sha256")):
            raise PipelineError("head/tail trim review visual assets changed")
    elif ep.data.get("no_dvd_proceed_approved") is not True:
        raise PipelineError("no-DVD cadence evidence requires the explicit no-DVD route choice")
    return review


def _source_frame_count(ld: Ld, source: Path) -> tuple[int, dict[str, Any]]:
    command = [
        ld.ffprobe(), "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames,nb_frames,r_frame_rate,avg_frame_rate,duration",
        "-of", "json", str(source),
    ]
    probe = subprocess.run(command, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode:
        raise PipelineError(f"ffprobe failed to count source frames: {probe.stderr[-800:]}")
    try:
        streams = json.loads(probe.stdout or "{}").get("streams", [])
        if len(streams) != 1:
            raise ValueError("expected exactly one video stream")
        info = streams[0]
        count = int(info.get("nb_read_frames") or info.get("nb_frames") or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot determine exact stack-master frame count: {exc}") from exc
    if count <= 0:
        raise PipelineError("source frame count is not a positive measurement")
    return count, info


def generate(ep: Any, ld: Ld, *, avs_out: Path, review_avs_out: Path,
             tfm_log_out: Path, decode_log_out: Path, report_out: Path) -> Path:
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    source = ep.path("stack_master")
    if source.is_symlink() or not source.is_file():
        raise PipelineError("verified stack master is missing or unsafe")
    native_audio = ep.path("ld_native")
    audio = ep.data.get("audio") or {}
    selected_label = str(audio.get("capture") or "")
    capture = next((row for row in ep.data.get("captures", [])
                    if str(row.get("label")) == selected_label), None)
    if (audio.get("efm_provenance_required") is not True or capture is None or
            not audio.get("capture_selection")):
        raise PipelineError("cadence evidence requires verified, explicitly selected EFM provenance")
    checks = Checks()
    validate_efm_capture_provenance(checks, ep, ld, capture, native_audio)
    if checks.failed:
        details = "; ".join(f"{name}: {detail}" for status, name, detail in checks.rows
                            if status == "FAIL")
        raise PipelineError(f"selected EFM provenance validation failed: {details}")
    trim_review = _approved_trim_review(ep, source, native_audio)
    pad = (ep.data.get("audio") or {}).get("pad_frames")
    if type(pad) is not int or pad < 0:
        raise PipelineError("verified audio.pad_frames is required before cadence evidence")
    stack_frames = (ep.data.get("stack") or {}).get("frames")
    if type(stack_frames) is not int or stack_frames <= pad:
        raise PipelineError("measured positive stack.frames greater than audio.pad_frames is required")
    source_count, source_probe = _source_frame_count(ld, source)
    if source_count != stack_frames:
        raise PipelineError(
            f"native master decodes to {source_count} frames but manifest stack.frames is {stack_frames}; "
            "reconcile the source/count before cadence analysis")
    expected = source_count - pad
    token_paths = (avs_out, review_avs_out, tfm_log_out, decode_log_out, report_out)
    for path in token_paths:
        assert_writable_path(path, ld)
        if path.exists() or path.is_symlink():
            raise PipelineError(f"refusing to overwrite cadence evidence output: {path}")
    loader = (ld.profile.get("tools") or {}).get("avisynth_loader")
    if loader:
        loader_path = Path(str(loader)).expanduser().resolve(strict=True)
        loader_line = f'Import("{_avs_quote(loader_path)}")\n'
    else:
        loader_path = None
        loader_line = ""
    source_literal = _avs_quote(source)
    log_literal = tfm_log_out.resolve(strict=False).as_posix().replace('"', '\\"')
    aligned = f'src = LSMASHVideoSource("{source_literal}").Trim({pad}, 0)\n'
    matched = "matched = src.TFM(order=-1, pp=0, chroma=true, slow=2"
    avs_text = (loader_line + aligned + matched +
                f', output="{log_literal}")\nreturn matched\n')
    review_text = (loader_line + aligned +
                   "matched = src.TFM(order=-1, pp=0, chroma=true, slow=2)\n"
                   "return StackHorizontal(src, matched)\n")
    for path, payload in ((avs_out, avs_text), (review_avs_out, review_text)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    manifest_hash_before = sha256_file(manifest_path)
    source_hash_before = sha256_file(source)
    audio_hash_before = sha256_file(native_audio)
    avs_hash = sha256_file(avs_out)
    review_avs_hash = sha256_file(review_avs_out)
    command = [
        ld.ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-stats_period", "5",
        "-f", "avisynth", "-i", str(avs_out),
        "-map", "0:v:0", "-an", "-f", "null", "-",
    ]
    decoded = subprocess.run(command, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    decode_log_out.parent.mkdir(parents=True, exist_ok=True)
    with decode_log_out.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("COMMAND: " + json.dumps(command, ensure_ascii=False) + "\n")
        stream.write("STDOUT / FFmpeg progress:\n" + (decoded.stdout or "") + "\n")
        stream.write("STDERR:\n" + (decoded.stderr or ""))
    progress_frames = [int(value) for value in re.findall(r"(?m)^frame=(\d+)\s*$", decoded.stdout or "")]
    observed_decode = progress_frames[-1] if progress_frames else None
    log_rows: list[dict[str, Any]] = []
    parse_error = None
    try:
        if not tfm_log_out.is_file():
            raise ValueError("TFM did not create its configured output log")
        log_rows = parse_tfm_log(tfm_log_out.read_text(encoding="utf-8-sig"),
                                 expected_frames=expected)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        parse_error = str(exc)
    source_hash_after = sha256_file(source)
    audio_hash_after = sha256_file(native_audio)
    manifest_hash_after = sha256_file(manifest_path)
    complete = (
        decoded.returncode == 0 and observed_decode == expected and parse_error is None and
        source_hash_after == source_hash_before and audio_hash_after == audio_hash_before and
        manifest_hash_after == manifest_hash_before and
        sha256_file(avs_out) == avs_hash and sha256_file(review_avs_out) == review_avs_hash
    )
    report = {
        "schema": "full-side-cadence-evidence-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "identity": ep.identity, "status": "VERIFIED" if complete else "INCOMPLETE",
        "candidate_is_approval": False,
        "manifest_path": str(manifest_path), "manifest_sha256": manifest_hash_before,
        "stack_master": str(source), "stack_master_sha256": source_hash_before,
        "native_audio": str(native_audio), "native_audio_sha256": audio_hash_before,
        "stack_frames_manifest": stack_frames, "stack_frames_decoded": source_count,
        "audio_pad_frames": pad, "expected_tfm_frames": expected,
        "ffprobe_source_stream": source_probe,
        "tfm_log_path": str(tfm_log_out),
        "tfm_log_sha256": sha256_file(tfm_log_out) if tfm_log_out.is_file() else None,
        "tfm_rows": len(log_rows), "tfm_complete_contiguous": parse_error is None,
        "tfm_parse_error": parse_error,
        "ffmpeg_exit_code": decoded.returncode, "ffmpeg_decoded_frames": observed_decode,
        "ffmpeg_decode_log": str(decode_log_out),
        "tfm_avs_path": str(avs_out), "tfm_avs_sha256": avs_hash,
        "review_avs_path": str(review_avs_out), "review_avs_sha256": review_avs_hash,
        "trim_review_sha256": (trim_review.get("review_record_sha256") or
                               trim_review.get("evidence_sha256") or
                               hashlib.sha256(json.dumps(
                                   trim_review, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")).hexdigest()),
        "errors": [],
    }
    if decoded.returncode:
        report["errors"].append(f"FFmpeg null decode exited {decoded.returncode}")
    if observed_decode != expected:
        report["errors"].append(
            f"FFmpeg decoded {observed_decode} frames; expected {expected}")
    if parse_error:
        report["errors"].append(parse_error)
    if (source_hash_after != source_hash_before or audio_hash_after != audio_hash_before or
            manifest_hash_after != manifest_hash_before):
        report["errors"].append("source, native audio, or manifest changed during cadence evidence generation")
    report_out.parent.mkdir(parents=True, exist_ok=True)
    with report_out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    if not complete:
        raise PipelineError(
            "cadence evidence is incomplete; logs and the INCOMPLETE report were preserved, "
            "but they cannot be used for a map or manifest update")
    return report_out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"), default="DS9")
    parser.add_argument("--avs-out", required=True)
    parser.add_argument("--review-avs-out", required=True)
    parser.add_argument("--tfm-log-out", required=True)
    parser.add_argument("--decode-log-out", required=True)
    parser.add_argument("--report-out", required=True)
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld, series=args.series)
    report = generate(
        ep, ld, avs_out=Path(args.avs_out), review_avs_out=Path(args.review_avs_out),
        tfm_log_out=Path(args.tfm_log_out), decode_log_out=Path(args.decode_log_out),
        report_out=Path(args.report_out))
    print(f"VERIFIED full-side TFM evidence: {report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, PipelineError) as exc:
        print(f"CADENCE EVIDENCE ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
