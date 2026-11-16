#!/usr/bin/env python3
"""Generate/apply a manifest-driven DVD 5.1 + English SRT retime schedule.

Break offsets and silence edges must already be measured/adjudicated and recorded
in episodes/<code>.json. This script performs the mechanical piecewise mapping; it
does not find picture matches or decide where an act break belongs.

  python scripts\\retime_dvd_assets.py --episode S01E05 --dry-run
  python scripts\\retime_dvd_assets.py --episode S01E05

All reference-DVD reads are sequential (no -ss), because some MakeMKV files have
non-monotonic video PTS. The DVD's unique 6-channel audio stream is selected by
FFprobe. Existing outputs are never overwritten.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ldcommon import (Ld, PipelineError, assert_writable_path, device_workspace_enabled,
                      load_episode, redact_shared_text)  # noqa: E402
from av_check_output import build_offset_model  # noqa: E402

FPS = 30000.0 / 1001.0


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def srt_seconds(value: str) -> float:
    h, m, rest = value.split(":")
    sec, millis = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(millis) / 1000


def fmt_srt(value: float) -> str:
    ms = max(0, int(round(value * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def coalesce_silence_intervals(intervals: list[tuple[float, float]],
                               epsilon_s: float = 1e-6) -> list[tuple[float, float]]:
    """Merge only overlapping or touching detector intervals, never nearby pauses."""
    if epsilon_s < 0:
        raise PipelineError("silence coalescing epsilon must be nonnegative")
    ordered = sorted((float(start), float(end)) for start, end in intervals)
    merged: list[list[float]] = []
    for start, end in ordered:
        if not (math.isfinite(start) and math.isfinite(end)) or end < start:
            raise PipelineError(f"invalid silence interval: {start}..{end}")
        if merged and start <= merged[-1][1] + epsilon_s:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def parse_silence_intervals(stderr: str) -> list[tuple[float, float]]:
    """Parse FFmpeg silencedetect output; ignore open-ended EOF silence."""
    start_re = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
    end_re = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
    pending: float | None = None
    intervals: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        start_match = start_re.search(line)
        end_match = end_re.search(line)
        if start_match:
            start = float(start_match.group(1))
            if pending is not None:
                intervals.append((pending, start))
            pending = start
        if end_match:
            end = float(end_match.group(1))
            if pending is not None:
                intervals.append((pending, end))
                pending = None
    return coalesce_silence_intervals(intervals)


def verify_expected_gaps(expected_gaps: list[dict], detected: list[tuple[float, float]],
                         *, tolerance_s: float = 0.070,
                         minimum_confident_gap_s: float = 0.050) -> dict:
    """Compare planned output gaps with observed silence; short gaps remain LIMITs."""
    if tolerance_s < 0 or minimum_confident_gap_s < 0:
        raise PipelineError("gap verification tolerances must be nonnegative")
    intervals = coalesce_silence_intervals(detected)
    rows = []
    matched_intervals: set[int] = set()
    for expected in expected_gaps:
        start = float(expected["start_s"])
        end = float(expected["end_s"])
        duration = float(expected.get("duration_s", end - start))
        if not all(math.isfinite(v) for v in (start, end, duration)) or end < start or duration < 0:
            raise PipelineError(f"invalid expected gap record: {expected!r}")
        row = {"label": str(expected.get("label", "gap")), "expected_start_s": start,
               "expected_end_s": end, "expected_duration_s": duration}
        if duration < minimum_confident_gap_s:
            row.update({"status": "LIMIT", "detail": "gap is below confident position-check resolution"})
            rows.append(row)
            continue
        candidates = []
        for interval_index, (obs_start, obs_end) in enumerate(intervals):
            if interval_index in matched_intervals:
                continue
            overlap = max(0.0, min(end + tolerance_s, obs_end) - max(start - tolerance_s, obs_start))
            if overlap > 0:
                candidates.append((overlap, interval_index, obs_start, obs_end))
        if not candidates:
            row.update({"status": "FAIL", "detail": "no detected output silence near the planned gap"})
            rows.append(row)
            continue
        _, interval_index, obs_start, obs_end = max(candidates, key=lambda item: item[0])
        matched_intervals.add(interval_index)
        position_delta = obs_start - start
        duration_delta = (obs_end - obs_start) - duration
        row.update({"detected_start_s": obs_start, "detected_end_s": obs_end,
                    "detected_duration_s": obs_end - obs_start,
                    "position_delta_s": position_delta, "duration_delta_s": duration_delta})
        if abs(position_delta) <= tolerance_s and abs(duration_delta) <= tolerance_s:
            row.update({"status": "PASS", "detail": "detected silence agrees within tolerance"})
        else:
            row.update({"status": "FAIL", "detail": "detected silence differs from planned gap beyond tolerance"})
        rows.append(row)
    statuses = [row["status"] for row in rows]
    overall = "FAIL" if "FAIL" in statuses else "LIMIT" if "LIMIT" in statuses else "PASS"
    return {"status": overall, "tolerance_s": tolerance_s,
            "minimum_confident_gap_s": minimum_confident_gap_s,
            "gaps": rows, "detected_interval_count": len(intervals)}


# Verification scan threshold. -40 dB merged quiet post-break content into planned
# gaps on S01E12 (scene-opening ambience whose samples sit between -40 and -50 dB
# inflated B1 by 1223 ms); at -50 dB the same FLAC yields exactly the 17 gaps plus
# head/tail with B1 matching plan to ~1 ms, and digital anullsrc gaps remain
# detectable at any threshold. See the S01E12 retime-verification episode log.
VERIFY_NOISE_DB = -50


def silencedetect_filter() -> str:
    """FFmpeg silence-detect expression used to verify the generated output."""
    return f"silencedetect=noise={VERIFY_NOISE_DB}dB:d=0.015"


def segment_chain(index: int, src_start: float, src_end: float, tempo: float,
                  dst_span: float) -> str:
    """atrim -> asetpts -> atempo, then pin the segment to exactly ``dst_span``.

    ``atempo`` output length deviates from input/tempo by 14-31 ppm (measured in
    isolation on S01E12: decode+atrim is sample-exact, the A0 chain lost 267
    samples). The error accumulates across segments and shifts later gaps earlier
    (-76 ms over S01E12's 18 segments, past the 70 ms verification tolerance).
    ``apad``+``atrim`` pin each segment's output so every gap lands at its planned
    position; the <=11 ms correction sits at the segment end, adjacent to the
    following gap, where it is absorbed by that gap.
    """
    return (f"[src{index}]atrim=start={src_start:.6f}:end={src_end:.6f},"
            f"asetpts=PTS-STARTPTS,atempo={tempo:.9f},"
            f"apad=whole_dur={dst_span:.6f},atrim=duration={dst_span:.6f}[a{index}]")


def scan_retimed_gaps(ld: Ld, audio_path: Path, expected_gaps: list[dict]) -> dict:
    cmd = [ld.ffmpeg(), "-hide_banner", "-nostats", "-i", str(audio_path), "-map", "0:a:0",
           "-af", silencedetect_filter(), "-f", "null", "-"]
    proc = run(cmd)
    if proc.returncode:
        raise PipelineError(f"FFmpeg output-silence verification failed (exit {proc.returncode}): {proc.stderr[-800:]}")
    return verify_expected_gaps(expected_gaps, parse_silence_intervals(proc.stderr or ""))


def get_dvd_streams(ld: Ld, dvd: Path) -> tuple[str, float, str]:
    p = run([ld.ffprobe(), "-v", "error", "-show_entries",
             "stream=index,codec_type,channels,channel_layout:stream_tags=language,title:format=duration",
             "-of", "json", str(dvd)])
    if p.returncode:
        raise PipelineError(f"ffprobe failed on DVD {dvd}: {p.stderr[-800:]}")
    obj = json.loads(p.stdout or "{}")
    streams = obj.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    six = [(i, s) for i, s in enumerate(audio) if int(s.get("channels") or 0) == 6]
    if len(six) != 1:
        raise PipelineError(f"expected exactly one 6-channel DVD audio stream, found {len(six)}; adjudicate and record the selector")
    duration = float((obj.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise PipelineError(f"DVD duration unavailable: {dvd}")
    layout = six[0][1].get("channel_layout") or "5.1"
    return f"0:a:{six[0][0]}", duration, layout


def parse_srt(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    out = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) >= 2 and "-->" in lines[1]:
            out.append(lines)
    if not out:
        raise PipelineError(f"no SRT cues parsed from {path}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--dry-run", action="store_true", help="print schedule/FFmpeg command without writing outputs")
    ap.add_argument("--graph-out")
    ap.add_argument("--audio-out")
    ap.add_argument("--srt-out")
    ap.add_argument("--map-out")
    ap.add_argument("--verify-out", help="new JSON output-gap verification report path")
    args = ap.parse_args()

    import numpy as np
    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("two-part E01 requires per-part retime measurements; generic retime stage is one-part only")
    dvd = ep.path("dvd")
    align = ep.path("align_npy")
    srt_src = ep.optional_path("srt_source")
    if not dvd.exists():
        raise PipelineError(f"DVD reference missing: {dvd}")
    if not align.exists():
        raise PipelineError(f"DVD/LD alignment path missing: {align}; measure and verify it first")
    if not srt_src or not srt_src.exists():
        raise PipelineError("manifest files.srt_source must point to the verified English source SRT")

    r = ep.data.get("retime") or {}
    breaks = sorted(r.get("breaks") or [], key=lambda x: (x.get("ld_black") or [0])[0])
    if not breaks:
        raise PipelineError(f"{ep.code}: no adjudicated retime.breaks in manifest")
    required = ("tail_off", "head_sil_end", "tail_sil_start")
    missing = [k for k in required if r.get(k) is None]
    if missing:
        raise PipelineError(f"{ep.code}: missing measured retime values {missing}; measure them, do not guess")
    for i, b in enumerate(breaks, 1):
        if b.get("merged") or not b.get("dvd_silence"):
            continue
        if not b.get("ld_black") or b.get("off_before") is None or b.get("off_after") is None:
            raise PipelineError(f"{ep.code} break {b.get('label',i)} is missing measured black/offset values")

    selector, dvd_duration, channel_layout = get_dvd_streams(ld, dvd)
    # Offset model is built from content-only alignment windows and the manifest's adjudicated black ranges.
    off = build_offset_model(ep, align)
    total = ep.duration_s
    delta = ep.delta_s
    master_end = ep.stack_frames / FPS
    tail_off = float(r["tail_off"])
    cuts = []
    for i, b in enumerate(breaks, 1):
        if b.get("merged") or not b.get("dvd_silence"):
            continue
        ls, le = map(float, b["ld_black"])
        ds, de = map(float, b["dvd_silence"])
        before, after = float(b["off_before"]), float(b["off_after"])
        src0, src1 = ds, de
        dst_end = src0 - before / FPS - delta
        dst_start = src1 - after / FPS - delta
        gap = dst_start - dst_end
        if gap < 0:
            src0 += -gap
            dst_end = src0 - before / FPS - delta
            gap = dst_start - dst_end
        if gap < -1e-8 or not (ds - 1e-8 <= src0 <= src1 <= de + 1e-8):
            raise PipelineError(f"{ep.code} break {b.get('label',i)} has invalid cut/gap after measured adjustment")
        if gap > (le - ls) + 0.050:
            raise PipelineError(f"{ep.code} break {b.get('label',i)} gap {gap:.3f}s exceeds its measured LD black")
        cuts.append((src0, src1, dst_end, dst_start, gap, b.get("label", f"B{i}")))
        print(f"{b.get('label',i)}: DVD silence {ds:.3f}-{de:.3f}s, gap {gap:.3f}s, step {after-before:+.0f} frames")

    head_src = max(0.0, delta + off(delta) / FPS)
    tail_src = (total + delta) + tail_off / FPS
    if tail_src > dvd_duration:
        print(f"tail source {tail_src:.3f}s exceeds DVD duration {dvd_duration:.3f}s; clamping to container end")
        tail_src = dvd_duration
    tail_dst = min(total, (tail_src - tail_off / FPS) - delta)
    if head_src >= float(r["head_sil_end"]):
        raise PipelineError("measured head mapping lands after the DVD head-silence end")
    if tail_src <= float(r["tail_sil_start"]):
        raise PipelineError("measured tail mapping lands before the DVD tail-silence start")

    segments, gaps = [], []
    src, dst = head_src, 0.0
    for src0, src1, dst_end, dst_start, gap, _label in cuts:
        segments.append([src, src0, dst, dst_end, None])
        gaps.append(gap)
        src, dst = src1, dst_start
    segments.append([src, tail_src, dst, tail_dst, None])
    for seg in segments:
        if seg[3] <= seg[2] or seg[1] <= seg[0]:
            raise PipelineError("retime produced a non-positive segment; inspect adjudicated offsets")
        seg[4] = (seg[1] - seg[0]) / (seg[3] - seg[2])
        if not 0.99 <= seg[4] <= 1.01:
            raise PipelineError(f"retime tempo {seg[4]:.9f} is outside 0.99..1.01; do not stretch to force a pass")
    if segments[0][2] != 0.0 or segments[-1][3] > total + 1e-6:
        raise PipelineError("retime segment timeline does not match output bounds")

    graph_out = Path(args.graph_out) if args.graph_out else ep.path("retime_graph")
    audio_out = Path(args.audio_out) if args.audio_out else ep.path("retimed_51")
    srt_out = Path(args.srt_out) if args.srt_out else ep.path("retimed_srt")
    map_out = Path(args.map_out) if args.map_out else ep.path("retime_map")
    default_verify_dir = ld.root("logs") if device_workspace_enabled(ld.profile) else ld.probe()
    verify_out = (Path(args.verify_out) if args.verify_out else
                  default_verify_dir / f"{ep.code}-retime-audio-verification.json")
    outputs = [graph_out, audio_out, srt_out, map_out, verify_out]
    for output in outputs:
        assert_writable_path(output, ld)
    if not args.dry_run:
        existing = [str(p) for p in outputs if p.exists()]
        if existing:
            raise PipelineError(f"refusing to overwrite retime outputs: {existing}")

    n = len(segments)
    graph = [f"[{selector}]asplit={n}" + "".join(f"[src{i}]" for i in range(n))]
    concat = []
    for i, (s0, s1, d0, d1, tempo) in enumerate(segments):
        graph.append(segment_chain(i, s0, s1, tempo, d1 - d0))
        concat.append(f"[a{i}]")
        if i < len(gaps) and gaps[i] > 0:
            graph.append(f"anullsrc=r=48000:cl={channel_layout}:d={gaps[i]:.9f}[g{i}]")
            concat.append(f"[g{i}]")
    graph.append("".join(concat) + f"concat=n={len(concat)}:v=0:a=1[joined]")
    graph.append(f"[joined]apad=whole_dur={total:.6f}[out]")
    # FFmpeg 9 no longer accepts the old -filter_complex_script option. Keep the
    # graph as an audit artifact but pass the expression through the current CLI.
    ffmpeg_cmd = [ld.ffmpeg(), "-hide_banner", "-i", str(dvd), "-filter_complex", ";".join(graph),
                  "-map", "[out]", "-c:a", "flac", "-sample_fmt", "s32", "-ar", "48000", "-compression_level", "8", str(audio_out)]

    cues = parse_srt(srt_src)
    def map_time(t: float) -> float:
        if t <= segments[0][0]:
            return 0.0
        for i, (s0, s1, d0, d1, _tempo) in enumerate(segments):
            if s0 <= t <= s1:
                return d0 + (t-s0)*(d1-d0)/(s1-s0)
            if i < len(gaps) and s1 < t < segments[i+1][0]:
                return d1 + (t-s1)/(segments[i+1][0]-s1)*gaps[i]
        return total

    retimed, skipped = [], 0
    for lines in cues:
        start_text, end_text = (x.strip() for x in lines[1].split("-->",1))
        start, end = map_time(srt_seconds(start_text)), map_time(srt_seconds(end_text))
        start, end = max(0.0,start), min(total,end)
        if end <= start + 0.05:
            skipped += 1
            continue
        lines[0] = str(len(retimed)+1)
        lines[1] = f"{fmt_srt(start)} --> {fmt_srt(end)}"
        retimed.append("\n".join(lines))
    if skipped:
        raise PipelineError(f"SRT retime dropped {skipped} cue(s); adjudicate rather than silently accepting a partial subtitle track")

    print(f"DVD audio selector: {selector} ({channel_layout}); DVD duration={dvd_duration:.3f}s; output={total:.6f}s; head={head_src:.3f}s; tail={tail_src:.3f}s")
    print("segments:")
    for i, seg in enumerate(segments):
        print(f"  A{i}: DVD {seg[0]:.3f}-{seg[1]:.3f} -> out {seg[2]:.3f}-{seg[3]:.3f}, tempo={seg[4]:.9f}, gap={gaps[i] if i < len(gaps) else 0:.3f}")
    print(f"subtitle cues: {len(cues)} input -> {len(retimed)} output, skipped={skipped}")
    print("FFmpeg:", " ".join(ffmpeg_cmd))
    expected_gaps = [
        {"label": label, "start_s": dst_end, "end_s": dst_start, "duration_s": gap}
        for _src0, _src1, dst_end, dst_start, gap, label in cuts if gap > 0
    ]
    print(f"post-write output-silence verification: {len(expected_gaps)} gaps; report={verify_out}")
    if args.dry_run:
        print("DRY RUN - no retime artifacts written")
        return 0

    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    graph_out.write_text(";\n".join(graph), encoding="ascii")
    with map_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(("segment", "source_start", "source_end", "output_start", "output_end", "tempo", "gap_after"))
        for i, seg in enumerate(segments):
            w.writerow((f"A{i}", *(f"{x:.6f}" for x in seg[:4]), f"{seg[4]:.9f}", f"{gaps[i] if i < len(gaps) else 0:.6f}"))

    srt_out.write_text("\r\n\r\n".join(retimed)+"\r\n",encoding="utf-8")
    p = subprocess.run(ffmpeg_cmd,capture_output=True,text=True,encoding="utf-8",errors="replace")
    if p.returncode:
        raise PipelineError(f"FFmpeg DVD 5.1 retime failed (exit {p.returncode}): {p.stderr[-1000:]}")
    if not audio_out.exists():
        raise PipelineError(f"FFmpeg returned success but retimed audio is missing: {audio_out}")
    verification = scan_retimed_gaps(ld, audio_out, expected_gaps)
    verification.update({
        "schema": "ld-pipeline-retime-audio-verification-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code,
        "audio": str(audio_out),
        "map": str(map_out),
        "method": f"FFmpeg silencedetect on generated 5.1 output; {VERIFY_NOISE_DB} dB, 15 ms minimum; only touching intervals coalesced",
        "manifest_updated": False,
    })
    if verification["status"] == "LIMIT":
        verification["caveat"] = "One or more gaps are below confident position-check resolution; review independently and do not claim a full gap-verification pass."
    report_text = json.dumps(verification, indent=2, ensure_ascii=False) + "\n"
    instance_root = Path(os.environ["LDP_INSTANCE_ROOT"]).resolve() if os.environ.get("LDP_INSTANCE_ROOT") else None
    if instance_root is not None and (ld.profile.get("shared") or {}).get("enabled") is True:
        try:
            verify_out.resolve().relative_to(instance_root)
            report_text = redact_shared_text(report_text, ld)
        except ValueError:
            pass
    verify_out.parent.mkdir(parents=True, exist_ok=True)
    with verify_out.open("x", encoding="utf-8", newline="\n") as report_file:
        report_file.write(report_text)
    for item in verification["gaps"]:
        print(f"gap verification {item['label']}: {item['status']} - {item['detail']}")
    if verification["status"] == "FAIL":
        raise PipelineError(f"retimed audio gap verification failed; inspect {verify_out}; outputs are preserved")
    if verification["status"] == "LIMIT":
        print("LIMIT: sub-resolution gap(s) remain unverified; do not report an ordinary full gap-verification pass")
    print(f"created retimed 5.1: {audio_out}; SRT {len(retimed)} cues: {srt_out}; map: {map_out}; verification: {verify_out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
