#!/usr/bin/env python3
r"""Emit a motion trace for build_map.ps1 (mean |frame - previous| per frame).

Why this exists
---------------
`build_map.ps1` accepts an optional motion trace produced on Windows by
`motion-probe.ps1`, which is **not shipped in this bundle**. Without it the cadence
report cannot run its native-30p gate and ends with
`VERDICT: motion trace not supplied`. This helper is the reusable, parameterized
replacement: nothing here is episode-specific and no measurement is inferred.

Contract expected by `build_map.ps1`
------------------------------------
* one plain decimal per line, matching ``^\s*([\d.]+)\s*$``
* **N-1 values** for an N-frame clip - tblend drops frame 0, and build_map.ps1
  pads the front with 0.0 automatically when it sees exactly N-1
* the motion probe must run on a *trim-only* script: a script that still contains
  TFM would re-run TFM and overwrite the cadence log

Usage
-----
  python scripts/build_motion_trace.py --source <stack-master.mov> --out <motion.txt>
  python scripts/build_motion_trace.py --episode S01E10          # resolves via manifest

Exit codes: 0 written, 2 refused (missing source / measured value / overwrite).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from fileutil import sha256_file  # noqa: E402


def ffmpeg_motion_trace(ld: Ld, ep, source: Path, out: Path, pad_frames: int,
                        log_out: Path, report_out: Path) -> dict:
    """Windows fallback: sequential FFmpeg frame-difference trace in source bit-depth units."""
    manifest_path = episode_manifest_path(ep.code, ld, series=ep.series).resolve(strict=True)
    manifest_hash_before = sha256_file(manifest_path)
    source_hash_before = sha256_file(source)
    log_out = assert_writable_path(log_out, ld)
    report_out = assert_writable_path(report_out, ld)
    out = assert_writable_path(out, ld)
    if any(path.exists() or path.is_symlink() for path in (out, log_out, report_out)):
        raise PipelineError("refusing to overwrite motion trace diagnostic outputs")

    ffprobe_command = [
        ld.ffprobe(), "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames,nb_frames,pix_fmt,bits_per_raw_sample,r_frame_rate,avg_frame_rate",
        "-of", "json", str(source),
    ]
    probe = subprocess.run(ffprobe_command, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode:
        raise PipelineError(f"ffprobe failed counting motion source frames: {probe.stderr[-800:]}")
    try:
        streams = json.loads(probe.stdout or "{}").get("streams", [])
        if len(streams) != 1:
            raise ValueError("expected one video stream")
        info = streams[0]
        source_frames = int(info.get("nb_read_frames") or info.get("nb_frames") or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot measure source frame count for motion trace: {exc}") from exc
    if source_frames <= pad_frames or source_frames < 2:
        raise PipelineError("source frame count must exceed the measured audio.pad_frames trim")
    expected_aligned_frames = source_frames - pad_frames
    manifest_frames = (ep.data.get("stack") or {}).get("frames")
    if type(manifest_frames) is not int or manifest_frames != source_frames:
        raise PipelineError(
            f"decoded source frame count {source_frames} does not match measured stack.frames={manifest_frames}")
    try:
        bits = int(info.get("bits_per_raw_sample") or 0)
    except (TypeError, ValueError):
        bits = 0
    if bits <= 0:
        pixel_format = str(info.get("pix_fmt") or "").casefold()
        match = re.search(r"p(\d{2})(?:le|be)?$", pixel_format)
        if match:
            bits = int(match.group(1))
        elif pixel_format in {
                "yuv420p", "yuv422p", "yuv444p", "yuv411p", "yuvj420p",
                "yuvj422p", "yuvj444p", "rgb24", "bgr24", "rgba", "bgra",
                "rgb0", "bgr0", "gray", "gray8"}:
            bits = 8
        elif pixel_format in {"rgb48le", "rgb48be", "bgr48le", "bgr48be",
                              "rgba64le", "rgba64be", "bgra64le", "bgra64be",
                              "gray16le", "gray16be"}:
            bits = 16
        else:
            raise PipelineError(
                f"cannot determine motion pixel bit depth from FFprobe pix_fmt={info.get('pix_fmt')!r}; "
                "refusing to scale normalized motion values by a guessed factor")
    scale = float((1 << bits) - 1)
    command = [
        ld.ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(source),
        "-vf", f"trim=start_frame={pad_frames},tblend=all_mode=difference,"
               "signalstats,metadata=print",
        "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    log_out.parent.mkdir(parents=True, exist_ok=True)
    with log_out.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("COMMAND: " + json.dumps(command, ensure_ascii=False) + "\n")
        stream.write(result.stderr or "")
    normalized = re.findall(
        r"(?m)^.*lavfi\.signalstats\.YAVG=([0-9.eE+-]+)\s*$",
        result.stderr or "")
    try:
        values = [float(value) * scale for value in normalized]
    except ValueError as exc:
        values = []
    # tblend drops frame zero, so its stats are exactly the N-1 frame differences.
    trace_values = values
    valid_count = len(trace_values) == expected_aligned_frames - 1
    finite = all(math.isfinite(value) and value >= 0 for value in trace_values)
    source_hash_after = sha256_file(source)
    manifest_hash_after = sha256_file(manifest_path)
    complete = (result.returncode == 0 and valid_count and finite and
                source_hash_before == source_hash_after and manifest_hash_before == manifest_hash_after)
    report = {
        "schema": "motion-trace-evidence-v1",
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code, "series": ep.series, "identity": ep.identity,
        "status": "VERIFIED" if complete else "INCOMPLETE",
        "candidate_is_approval": False,
        "source_path": str(source), "source_sha256": source_hash_before,
        "manifest_path": str(manifest_path), "manifest_sha256": manifest_hash_before,
        "source_frames": source_frames, "audio_pad_frames": pad_frames,
        "aligned_frames": expected_aligned_frames,
        "motion_values": len(trace_values), "expected_motion_values": expected_aligned_frames - 1,
        "raw_signalstats_values": len(values), "bit_depth_scale": scale,
        "ffprobe_source_stream": info,
        "motion_trace_path": str(out), "motion_trace_sha256": None,
        "decode_log": str(log_out), "decode_log_sha256": sha256_file(log_out),
        "ffmpeg_exit_code": result.returncode,
        "errors": [],
    }
    if result.returncode:
        report["errors"].append(f"FFmpeg exited {result.returncode}")
    if not valid_count:
        report["errors"].append(
            f"FFmpeg yielded {len(values)} frame statistics; expected {expected_aligned_frames - 1}")
    if not finite:
        report["errors"].append("motion trace contains non-finite or negative values")
    if source_hash_before != source_hash_after or manifest_hash_before != manifest_hash_after:
        report["errors"].append("source or manifest changed during motion-trace generation")
    if not complete:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        with report_out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        raise PipelineError("motion trace is incomplete; decoder log/report were retained, no trace was written")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(f"{value:.6f}" for value in trace_values) + "\n")
    report["motion_trace_sha256"] = sha256_file(out)
    with report_out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="stack master (.mov)")
    ap.add_argument("--episode", help="resolve --source from the episode manifest")
    ap.add_argument("--out", required=True, help="motion trace file to write")
    ap.add_argument("--trim-head", type=int, default=0,
                    help="audio alignment offset to drop first, so the trace runs on the "
                         "audio-aligned clip like the TFM log (audio.pad_frames)")
    ap.add_argument("--log-out", help="new sequential decoder diagnostic log (required for Windows cadence plans)")
    ap.add_argument("--report-out", help="new source/frame-count/hash verification JSON")
    ap.add_argument("--dry-run", action="store_true", help="print stats, do not write")
    args = ap.parse_args()

    ld = Ld()
    ep = None
    if args.episode:
        ep = load_episode(args.episode, ld)
        source = str(ep.path("stack_master"))
    elif args.source:
        source = str(Path(args.source).resolve())
    else:
        raise PipelineError("supply --source or --episode")

    src_path = Path(source)
    if not src_path.is_file():
        raise PipelineError(f"source not found: {src_path}")
    out = Path(args.out)
    assert_writable_path(out, ld)
    if out.exists():
        raise PipelineError(f"refusing to overwrite existing file: {out}")

    os_name = str((ld.profile.get("os") or
                   ("windows" if os.name == "nt" else ""))).casefold()
    if ep is not None and (os.name == "nt" or os_name in ("windows", "win32")):
        if args.dry_run:
            print(f"DRY RUN - would sequentially measure motion from {source} after {args.trim_head} frame(s)")
            return 0
        token = f"{int(time.time() * 1000000)}"
        log_out = Path(args.log_out) if args.log_out else ld.probe() / f"{ep.code}-motion-decode-{token}.log"
        report_out = (Path(args.report_out) if args.report_out else
                      ld.probe() / f"{ep.code}-motion-trace-{token}.json")
        report = ffmpeg_motion_trace(ld, ep, src_path, out, args.trim_head, log_out, report_out)
        print(f"motion trace: {out} ({report['motion_values']} values)")
        print(f"verified report: {report_out}")
        return 0

    import vapoursynth as vs

    core = vs.core
    # Bounded cache: this walks ~83k frames of a 22 GB master.
    core.max_cache_size = 4096
    src = core.bs.VideoSource(source)
    # Run on the AUDIO-ALIGNED clip (runbook §4: "run the TFM log on the audio-aligned
    # clip, .Trim(pad,0)") so the trace and the cadence log share one frame domain.
    if args.trim_head:
        src = core.std.Trim(src, args.trim_head, None)
        print(f"aligned: dropped first {args.trim_head} frame(s) (audio.pad_frames)")
    total = src.num_frames
    if total < 2:
        raise PipelineError(f"source has only {total} frame(s); need at least 2")

    # PlaneStatsDiff is NORMALISED to [0,1] by the plane's max value, but
    # build_map.ps1 thresholds motion against a pixel-difference scale
    # (`$motMean -ge 15.0`). Measured on this master: raw/PlaneStatsDiff = exactly
    # 1023 = 2**10 - 1. Convert back to raw mean |frame-prev| in the clip's own
    # units so the native-30p gate can actually fire.
    fmt = src.format
    bits = fmt.bits_per_sample if fmt is not None else 10
    scale = float((1 << bits) - 1)

    # NOTE: VapourSynth's std.Trim does NOT accept AviSynth-style negative
    # "count from the end" indices - any negative `last` raises
    # "invalid last frame specified". Use absolute indices instead.
    prev = core.std.Trim(src, 0, total - 2)   # frames 0 .. N-2
    cur = core.std.Trim(src, 1, None)         # frames 1 .. N-1
    stats = core.std.PlaneStats(prev, cur, plane=0)

    print(f"source : {src_path}")
    print(f"frames : {total}  ->  expect {total - 1} motion values")
    print(f"scale  : PlaneStatsDiff x {scale:.0f} (bit depth {bits}) -> raw units")
    started = time.time()
    values: list[str] = []
    peak = 0.0
    for i in range(total - 1):
        f = stats.get_frame(i)
        v = float(f.props.get("PlaneStatsDiff", 0.0)) * scale
        if v > peak:
            peak = v
        values.append(f"{v:.6f}")
        if (i + 1) % 20000 == 0:
            print(f"  ... {i + 1}/{total - 1} "
                  f"({(i + 1) / (time.time() - started):.0f} fps)", flush=True)
    elapsed = time.time() - started

    print(f"done   : {len(values)} values in {elapsed:.1f}s "
          f"({(total - 1) / max(elapsed, 0.001):.0f} fps), peak |diff|={peak:.6f}")
    if args.dry_run:
        print("DRY RUN - not written")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(values) + "\n")
    print(f"wrote {out}")
    if args.report_out:
        report_out = assert_writable_path(Path(args.report_out), ld)
        if report_out.exists() or report_out.is_symlink():
            raise PipelineError(f"refusing to overwrite motion trace report: {report_out}")
        report = {
            "schema": "motion-trace-evidence-v1",
            "workflow_version": (Path(__file__).resolve().parent.parent
                                 / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "episode": ep.code if ep else None, "series": ep.series if ep else None,
            "source_path": str(src_path), "source_sha256": sha256_file(src_path),
            "source_frames": total + args.trim_head, "audio_pad_frames": args.trim_head,
            "aligned_frames": total, "motion_values": len(values),
            "motion_trace_path": str(out), "motion_trace_sha256": sha256_file(out),
            "motion_method": "VapourSynth PlaneStatsDiff scaled to source bit depth",
            "candidate_is_approval": False,
        }
        report_out.parent.mkdir(parents=True, exist_ok=True)
        with report_out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print(f"motion trace report: {report_out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
