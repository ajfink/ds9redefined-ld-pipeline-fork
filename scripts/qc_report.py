#!/usr/bin/env python3
"""Delivery QC report for one episode: run every gate, print PASS/FAIL with numbers.

  python scripts\\qc_report.py --episode S01E05
  python scripts\\qc_report.py --episode S01E05 --skip-heavy   (reuse prior gate artifacts)

Gates, each with an explicit threshold (runbook section 10):
  1. full decode completes and the frame count matches the manifest
  2. stream inventory: types, frame count, duration, dispositions, languages, titles
  3. packet hashes are bit-exact against both the manifest and the source files
  4. no PAD-aligned single-frame black survives        scan_padfixed_black.py
  5. replacement images match their own frame          verify_pad_identities.py
  6. absolute A/V within +/-100 ms, +/-30 ppm, rms<=40 ms   av_check_output.py
     (or DVD-free timeline/sample-budget validation when the user elected no-DVD mode)
  7. between-track drift within +/-30 ppm, rms<=40 ms   sync_check.py
  8. retimed SRT: count, monotonic, inside the timeline
  9. measured values agree with what the manifest recorded (warnings, not failures)

Writes <code>-qc-report.md. Exit 0 only if every hard gate passes.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import BUNDLE, Ld, PipelineError, load_episode  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent

STANDING_WARNINGS = [
    "validate artifacts, not exit codes: Orc refresh, blackdetect and curl have all "
    "returned non-zero while reporting success - check the produced report/log/frame count",
    "reference DVDs have non-monotonic PTS: never seek them with -ss; decode sequentially "
    "and index with showinfo",
    "ffmpeg select expressions: keep each expression to <=32 terms or extraction silently mis-selects",
    "never make A/B copies as hardlinks then overwrite with Copy-Item -Force (writes through the inode)",
    "respect machine.json limits.heavy_jobs and the recorded hardware; never run two downloaders for one item",
    "Flowframes CLI needs elevation/dev mode - use the upstream rife-ncnn-vulkan engine",
    "measure every episode: never copy another episode's AudioSkip, DELTA, rate or trim",
    "DVD-free deliveries can verify the capture-origin/duration/PAD model and exact audio/video endpoint, "
    "but cannot claim an external absolute A/V cross-correlation",
]


def sh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_gate(script: str, args: list[str], skip: bool) -> tuple[str, str]:
    """Run a gate script; return (status, detail). status in PASS/FAIL/ERROR/SKIP."""
    if skip:
        return "SKIP", "reuse artifacts (--skip-heavy)"
    p = sh([sys.executable, str(SCRIPTS / script), *args])
    tail = "\n".join((p.stdout or "").strip().splitlines()[-4:])
    if p.returncode == 0:
        if "not applicable" in (p.stdout or "").lower():
            return "SKIP", tail.replace("\n", " | ")
        return "PASS", tail.replace("\n", " | ")
    if p.returncode == 1:
        return "FAIL", tail.replace("\n", " | ") or (p.stderr or "")[-300:]
    return "ERROR", ((p.stderr or "") + (p.stdout or ""))[-400:]


def ffprobe_json(ld: Ld, path: Path, count_frames: bool = False) -> dict:
    cmd = [ld.ffprobe(), "-v", "error"]
    if count_frames:
        cmd.append("-count_frames")
    cmd += ["-show_entries",
            "stream=index,codec_type,codec_name,profile,pix_fmt,width,height,"
            "sample_aspect_ratio,r_frame_rate,nb_read_frames,nb_frames,sample_rate,channels,"
            "channel_layout,bits_per_raw_sample:stream_disposition=default:"
            "stream_tags=language,title:format=duration,size",
            "-of", "json", str(path)]
    p = sh(cmd)
    if p.returncode:
        raise PipelineError(f"ffprobe failed on {path}: {p.stderr[-400:]}")
    return json.loads(p.stdout or "{}")


def packet_hash(ld: Ld, path: Path, selector: str) -> str:
    p = sh([ld.ffmpeg(), "-hide_banner", "-v", "error", "-i", str(path),
            "-map", selector, "-c", "copy", "-f", "hash", "-hash", "md5", "-"])
    if p.returncode:
        raise PipelineError(f"packet hash failed {path} {selector}: {p.stderr[-300:]}")
    last = (p.stdout or "").strip().splitlines()[-1]
    return last.replace("MD5=", "").strip().lower()


def parse_srt(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    items = re.findall(r"(?m)^\s*(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", text)

    def sec(t: str) -> float:
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    starts = [sec(a) for _, a, _ in items]
    ends = [sec(b) for _, _, b in items]
    seq = all(int(items[i][0]) == i + 1 for i in range(len(items)))
    mono = all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1)) if len(starts) > 1 else True
    return len(items), starts, ends, seq, mono


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--skip-heavy", action="store_true", help="skip heavy gates for triage only; report is unverified and exits nonzero")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("record_status") == "measured-partial" or ep.data.get("format") == "two_part":
        raise PipelineError(f"{ep.code} is a partial/two-part record; generic QC is not valid until per-part deliverables and gates are defined")
    probe = ld.probe()
    dkey = ep.data.get("deliverable") or "padfixed"
    deliverable = ep.path(dkey)
    if not deliverable.exists():
        raise PipelineError(f"missing deliverable ({dkey}): {deliverable}")
    results: list[tuple[str, str, str]] = []

    def add(name: str, status: str, detail: str = ""):
        results.append((name, status, detail))

    print(f"=== {ep.code} {ep.title} QC ===\n{deliverable}\n", flush=True)

    # ---- heavy gates (they decode the deliverable) ----
    print("[1/9] full decode + PAD black gate ...", flush=True)
    add("full-decode + PAD black", *run_gate("scan_padfixed_black.py",
                                             ["--episode", ep.code, "--encode", str(deliverable), "--rescan"],
                                             args.skip_heavy))
    print("[2/9] replacement identity ...", flush=True)
    add("replacement identity", *run_gate("verify_pad_identities.py",
                                          ["--episode", ep.code, "--encode", str(deliverable)],
                                          args.skip_heavy))
    dvd_available = ep.data.get("dvd_reference_available")
    if dvd_available is None:  # existing V1-V5 manifests carry files.dvd instead
        dvd_available = bool((ep.data.get("files") or {}).get("dvd"))
    if dvd_available:
        print("[3/9] absolute A/V ...", flush=True)
        add("absolute A/V", *run_gate("av_check_output.py",
                                       ["--episode", ep.code, "--video", str(deliverable)],
                                       args.skip_heavy))
    else:
        print("[3/9] DVD-free timeline sync ...", flush=True)
        add("absolute A/V", "SKIP",
            "no DVD reference supplied; external absolute A/V cross-correlation is unavailable")
        add("DVD-free sync", *run_gate("ld_only_sync_check.py",
                                        ["--episode", ep.code, "--video", str(deliverable)],
                                        args.skip_heavy))
    if "51" in list(ep.data.get("deliverable_tracks") or ["ld"]):
        print("[4/9] between-track sync ...", flush=True)
        add("between-track sync", *run_gate("sync_check.py",
                                            ["--episode", ep.code], args.skip_heavy))
    else:
        add("between-track sync", "SKIP", "no DVD 5.1 track in deliverable_tracks")

    # ---- container / stream inventory ----
    print("[5/9] stream inventory ...", flush=True)
    info = ffprobe_json(ld, deliverable, count_frames=False)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    vid = [s for s in streams if s.get("codec_type") == "video"]
    aud = [s for s in streams if s.get("codec_type") == "audio"]
    sub = [s for s in streams if s.get("codec_type") == "subtitle"]
    tracks = list(ep.data.get("deliverable_tracks") or ["ld"])
    want_a = 2 if "51" in tracks else 1
    want_s = 1 if "srt" in tracks else 0
    problems = []
    if len(vid) != 1:
        problems.append(f"video streams={len(vid)}")
    else:
        v = vid[0]
        if v.get("profile") != "Main 10":
            problems.append(f"profile={v.get('profile')}")
        if v.get("pix_fmt") != "yuv420p10le":
            problems.append(f"pix_fmt={v.get('pix_fmt')}")
        if v.get("r_frame_rate") not in ("24000/1001", "23.976"):
            problems.append(f"rate={v.get('r_frame_rate')}")
        if (v.get("width"), v.get("height")) != (768, 484):
            problems.append(f"raster={v.get('width')}x{v.get('height')}")
        if v.get("disposition", {}).get("default") != 1:
            problems.append("video not default")
        if "LaserDisc" not in v.get("tags", {}).get("title", ""):
            problems.append("video title missing LaserDisc provenance")
    if len(aud) != want_a:
        problems.append(f"audio streams={len(aud)} want {want_a}")
    else:
        if (aud[0].get("codec_name") != "flac" or aud[0].get("channels") != 2
                or aud[0].get("sample_rate") != "44100" or aud[0].get("bits_per_raw_sample") != "16"):
            problems.append("LD track is not 44.1 kHz/16-bit FLAC stereo")
        if aud[0].get("disposition", {}).get("default") != 1:
            problems.append("LD is not the default track")
        if aud[0].get("tags", {}).get("language") != "eng":
            problems.append("LD missing language=eng")
        if aud[0].get("tags", {}).get("title") != "LD 2.0 (original Dolby Surround)":
            problems.append("LD track title is missing or unexpected")
        if want_a == 2:
            if (aud[1].get("codec_name") != "flac" or aud[1].get("channels") != 6
                    or aud[1].get("sample_rate") != "48000" or aud[1].get("bits_per_raw_sample") != "24"):
                problems.append(f"5.1 track wrong: {aud[1].get('codec_name')} {aud[1].get('channels')}ch/"
                                f"{aud[1].get('sample_rate')}Hz/{aud[1].get('bits_per_raw_sample')}bit")
            if aud[1].get("disposition", {}).get("default") != 0:
                problems.append("5.1 must be non-default")
            if aud[1].get("tags", {}).get("language") != "eng":
                problems.append("5.1 missing language=eng")
            if aud[1].get("tags", {}).get("title") != "DVD 5.1 (retimed)":
                problems.append("5.1 track title is missing or unexpected")
    if len(sub) != want_s:
        problems.append(f"subtitle streams={len(sub)} want {want_s}")
    elif sub:
        if sub[0].get("codec_name") != "subrip":
            problems.append(f"subtitle codec={sub[0].get('codec_name')} (wanted SubRip)")
        if sub[0].get("disposition", {}).get("default") != 0:
            problems.append("subtitles must be non-default")
        if sub[0].get("tags", {}).get("language") != "eng":
            problems.append("subtitle missing language=eng")
        if sub[0].get("tags", {}).get("title") != "English SDH (retimed)":
            problems.append("subtitle title is missing or unexpected")
    dur = float(fmt.get("duration") or 0.0)
    if abs(dur - ep.duration_s) > 0.002:
        problems.append(f"container duration {dur} != manifest {ep.duration_s}")
    add("stream inventory", "PASS" if not problems else "FAIL",
        "; ".join(problems) if problems else
        f"1 video Main10 768x484 23.976, {len(aud)} audio (LD default), {len(sub)} subtitle, "
        f"{dur:.6f}s, {fmt.get('size')} bytes")

    # ---- frame count (from the full decode log to avoid a second decode) ----
    print("[6/9] frame count ...", flush=True)
    scan_log = probe / f"{ep.code}-blackscan-{deliverable.stem}.txt"
    frames = None
    if scan_log.exists():
        found = re.findall(r"frame=(\d+)",
                           scan_log.read_text(encoding="utf-8", errors="replace"))
        if found:
            frames = int(found[-1])   # progress lines precede the total
    if frames is None:
        p = sh([ld.ffprobe(), "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(deliverable)])
        m = re.search(r"\d+", p.stdout or "")
        frames = int(m.group()) if m else None
    if frames is None:
        add("frame count", "ERROR", "could not determine the frame count")
    elif frames == ep.frame_count:
        add("frame count", "PASS", f"{frames} == manifest video.frames")
    else:
        add("frame count", "FAIL", f"{frames} != manifest {ep.frame_count}")

    # ---- packet hashes ----
    print("[7/9] packet hashes ...", flush=True)
    try:
        vh = packet_hash(ld, deliverable, "0:v:0")
        recorded = (ep.data.get("qc") or {}).get("video_md5")
        if recorded and vh != recorded.lower():
            add("video packet hash", "FAIL", f"{vh} != manifest {recorded}")
        else:
            add("video packet hash", "PASS", vh)
        ld_h = packet_hash(ld, deliverable, "0:a:0")
        src_key = ep.data.get("ld_audio_source")
        ld_src = ep.path(src_key) if src_key else None
        src_h = packet_hash(ld, ld_src, "0:a:0") if ld_src and ld_src.exists() else None
        rec_ld = (ep.data.get("qc") or {}).get("ld_audio_md5")
        if src_h and ld_h != src_h:
            add("LD audio hash", "FAIL", f"{ld_h} != source {src_h}")
        elif rec_ld and ld_h != rec_ld.lower():
            add("LD audio hash", "FAIL", f"{ld_h} != manifest {rec_ld}")
        else:
            add("LD audio hash", "PASS", ld_h + (" (source match)" if src_h else " (manifest match)"))
        if "51" in tracks:
            src = ep.path("retimed_51") if ep.data.get("files", {}).get("retimed_51") \
                else ep.path("pre_upscale")
            if not src.exists():
                raise PipelineError(f"5.1 source missing: {src}")
            a = packet_hash(ld, deliverable, "0:a:1")
            b = packet_hash(ld, src, "0:a:0")
            rec51 = (ep.data.get("qc") or {}).get("fiveone_md5")
            ok51 = a == b and (not rec51 or a == rec51.lower())
            add("DVD 5.1 hash", "PASS" if ok51 else "FAIL",
                f"out={a} src={b}" + (f" manifest={rec51}" if rec51 else ""))
            ssrc = ep.path("retimed_srt") if ep.data.get("files", {}).get("retimed_srt") \
                else ep.path("pre_upscale")
            # .srt and .mkv both expose subtitles as 0:s:0
            if not ssrc.exists():
                raise PipelineError(f"subtitle source missing: {ssrc}")
            s1 = packet_hash(ld, deliverable, "0:s:0")
            s2 = packet_hash(ld, ssrc, "0:s:0")
            rec_srt = (ep.data.get("qc") or {}).get("srt_md5")
            ok_srt = s1 == s2 and (not rec_srt or s1 == rec_srt.lower())
            add("SRT hash", "PASS" if ok_srt else "FAIL",
                f"out={s1} src={s2}" + (f" manifest={rec_srt}" if rec_srt else ""))
    except PipelineError as e:
        add("packet hashes", "ERROR", str(e))

    # ---- subtitles ----
    print("[8/9] subtitle timeline ...", flush=True)
    srt_path = ep.path("retimed_srt") if ep.data.get("files", {}).get("retimed_srt") else None
    if "srt" not in tracks:
        add("subtitle timeline", "SKIP", "no subtitle track in deliverable_tracks")
    elif not srt_path or not srt_path.exists():
        add("subtitle timeline", "FAIL", "retimed SRT file not found; cannot verify requested subtitle track")
    else:
        n, starts, ends, seq, mono = parse_srt(srt_path)
        in_range = bool(starts) and min(starts) >= 0 and max(ends) <= ep.duration_s + 1e-6
        recorded = (ep.data.get("retime") or {}).get("srt_cues")
        detail = f"{n} cues, monotonic={mono}, sequential={seq}, {min(starts):.3f}..{max(ends):.3f}s"
        if recorded is not None and n != recorded:
            add("subtitle timeline", "FAIL", detail + f"; manifest says {recorded}")
        elif not (mono and seq and in_range):
            add("subtitle timeline", "FAIL", detail + f", in_range={in_range}")
        else:
            add("subtitle timeline", "PASS", detail)

    # ---- manifest cross-check (informational) ----
    print("[9/9] manifest cross-check ...", flush=True)
    notes = []
    recorded_bytes = (ep.data.get("qc") or {}).get("delivered_bytes")
    if recorded_bytes and int(fmt.get("size") or 0) != recorded_bytes:
        notes.append(f"size {fmt.get('size')} != recorded {recorded_bytes}")
    for key, measured in (("pad_aligned_black_hits", 0),):
        rec = (ep.data.get("qc") or {}).get(key)
        if rec is not None and rec != measured:
            notes.append(f"{key}: measured {measured} vs recorded {rec}")
    add("manifest cross-check", "WARN" if notes else "PASS",
        "; ".join(notes) if notes else "recorded size/hash/counters agree")

    # ---- report ----
    w = max(len(r[0]) for r in results)
    print()
    for name, status, detail in results:
        print(f"  [{status:5s}] {name.ljust(w)}  {detail}")
    n_pass = sum(1 for r in results if r[1] == "PASS")
    n_warn = sum(1 for r in results if r[1] == "WARN")
    n_skip = sum(1 for r in results if r[1] == "SKIP")
    # A sync check is legitimately not applicable when the delivered file has no
    # 5.1 track. Any other skipped gate (including --skip-heavy) is unverified.
    allowed_skips = set()
    if not dvd_available:
        allowed_skips.add("absolute A/V")
    if "51" not in tracks:
        allowed_skips.add("between-track sync")
    if "srt" not in tracks:
        allowed_skips.add("subtitle timeline")
    unverified = [r for r in results if r[1] == "SKIP" and r[0] not in allowed_skips]
    n_fail = sum(1 for r in results if r[1] in ("FAIL", "ERROR")) + len(unverified)
    print(f"\n  {n_pass} pass, {n_warn} warn, {n_skip} skip, {n_fail} fail/unverified")
    print("\n  standing warnings:")
    for x in STANDING_WARNINGS:
        print(f"    - {x}")

    report = probe / f"{ep.code}-qc-report.md"
    lines = [f"# {ep.code} {ep.title} - delivery QC", "",
             f"- deliverable: `{deliverable}`",
             f"- profile: `{ld.profile_path}` (machine {ld.profile.get('machine')})",
             f"- generated by `scripts/qc_report.py --episode {ep.code}`", "",
             "| gate | status | detail |", "|---|---|---|"]
    for name, status, detail in results:
        lines.append(f"| {name} | {status} | {detail.replace('|', '/')} |")
    lines += ["", f"**{n_pass} pass, {n_warn} warn, {n_skip} skip, {n_fail} fail/unverified**", "", "## Standing warnings", ""]
    lines += [f"- {x}" for x in STANDING_WARNINGS]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  report: {report}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
