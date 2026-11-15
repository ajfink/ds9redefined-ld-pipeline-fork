#!/usr/bin/env python3
"""Stream-copy the pad-corrected video together with this episode's tracks.

    python scripts/remux_padfixed_mac.py --episode S01E10 --dry-run
    python scripts/remux_padfixed_mac.py --episode S01E10

macOS counterpart of Workflow Version 5's ``remux_padfixed.ps1``. It has no
platform-specific input handling, so the recipe carries over verbatim; only the
invocation differs (no ``Invoke-LdFfmpeg``, no PowerShell here).

Which tracks are muxed, and where they come from, is decided by the manifest::

    mux_tracks         ["ld"] or ["ld","51","srt"]
    ld_audio_source    key of the track file holding the LD stereo

so one script serves every episode without an episode-specific branch.

Nothing is re-encoded: every output stream must be packet-identical to its
source, which is asserted after the mux by comparing packet MD5s against the
exact sources that were mapped - not just stream inventory/dispositions.
``--audio-source``/``--output`` support isolated, non-manifest A/B timing tests;
they never overwrite outputs.

Output defaults to the manifest's ``deliverable`` key (V6 initial manifests use
``pre_upscale``); pass ``--output`` only for an isolated, new-output test.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # sibling modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ldcommon
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402


def packet_md5(ld: Ld, path: Path, selector: str) -> str:
    """Packet MD5 of one mapped stream (stream-copy identity evidence)."""
    p = subprocess.run([ld.ffmpeg(), "-hide_banner", "-v", "error", "-i", str(path),
                        "-map", selector, "-c", "copy", "-f", "hash",
                        "-hash", "md5", "-"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode != 0:
        raise PipelineError(f"packet-hash failed for {path} {selector} "
                            f"(exit {p.returncode}): {(p.stderr or '')[-400:]}")
    hits = re.findall(r"(?m)^MD5=([0-9a-fA-F]{32})\s*$", p.stdout or "")
    if len(hits) != 1:
        raise PipelineError(f"could not read a unique packet MD5 for "
                            f"{path} {selector}: {(p.stdout or '')[-400:]}")
    return hits[0].lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--video", help="override the pad-corrected video input")
    ap.add_argument("--audio-source", help="override the LD stereo source (non-manifest A/B test)")
    ap.add_argument("--output", help="override the output .mkv path")
    ap.add_argument("--dry-run", action="store_true", help="print the command, do not run")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    capture = (ep.data.get("audio") or {}).get("capture")
    if not capture:
        raise PipelineError(f"{ep.code}: audio.capture is not measured; select the source WAV before remux")
    ff, ffp = ld.tool("ffmpeg"), ld.tool("ffprobe")
    video = Path(args.video) if args.video else ep.path("padfixed_video")
    deliverable_key = ep.data.get("deliverable") or "padfixed"
    out = Path(args.output) if args.output else ep.path(deliverable_key)
    dur = ep.data["video"].get("duration_s")
    if dur is None:
        raise PipelineError("manifest has no video.duration_s - measure it first")

    tracks = list(ep.data.get("mux_tracks") or [])
    if not tracks:
        raise PipelineError("manifest has no mux_tracks - record ['ld'] or "
                            "['ld','51','srt']")
    ld_key = ep.data.get("ld_audio_source")
    if not args.audio_source and not ld_key:
        raise PipelineError("manifest has no ld_audio_source")
    ld_src = Path(args.audio_source) if args.audio_source else ep.path(ld_key)
    want51 = "51" in tracks
    want_srt = "srt" in tracks
    if (want51 != want_srt) and ep.data.get("format") != "one_part":
        raise PipelineError("mux_tracks mixes audio and subtitle inclusion "
                            f"inconsistently: {','.join(tracks)}")

    for f in (video, ld_src):
        if not f.is_file():
            raise PipelineError(f"missing input: {f}")
    if not args.dry_run and out.exists():
        raise PipelineError(f"refusing to overwrite existing output: {out}")

    # V6 maps requested retimed DVD tracks from their own files. Retain the old
    # pre_upscale-container mapping for backward compatibility with older manifests.
    full = ep.optional_path("pre_upscale")
    fiveone = ep.optional_path("retimed_51")
    srt = ep.optional_path("retimed_srt")
    legacy_full = bool(full and full.is_file() and ld_src == full)
    ff_inputs = ["-i", str(video), "-i", str(ld_src)]
    maps = ["-map", "0:v:0", "-map", "1:a:0"]
    meta = ["-metadata:s:a:0", "title=LD 2.0 (original Dolby Surround)",
            "-metadata:s:a:0", "language=eng", "-disposition:a:0", "default"]
    track_pairs = []
    input_index = 2
    if want51:
        if fiveone and fiveone.is_file():
            ff_inputs += ["-i", str(fiveone)]
            maps += ["-map", f"{input_index}:a:0"]
            track_pairs.append(("DVD 5.1", fiveone, "0:a:0", "0:a:1"))
            input_index += 1
        elif legacy_full:
            maps += ["-map", "1:a:1"]
            track_pairs.append(("DVD 5.1", full, "0:a:1", "0:a:1"))
        else:
            raise PipelineError(f"requested DVD 5.1 source is missing: {fiveone}")
        meta += ["-metadata:s:a:1", "title=DVD 5.1 (retimed)",
                 "-metadata:s:a:1", "language=eng", "-disposition:a:1", "0"]
    if want_srt:
        if srt and srt.is_file():
            ff_inputs += ["-i", str(srt)]
            maps += ["-map", f"{input_index}:s:0"]
            track_pairs.append(("English SDH", srt, "0:s:0", "0:s:0"))
        elif legacy_full:
            maps += ["-map", "1:s:0"]
            track_pairs.append(("English SDH", full, "0:s:0", "0:s:0"))
        else:
            raise PipelineError(f"requested English SRT source is missing: {srt}")
        meta += ["-metadata:s:s:0", "title=English SDH (retimed)",
                 "-metadata:s:s:0", "language=eng", "-disposition:s:0", "0"]

    ff_cmd = [str(ff), "-y", "-hide_banner", "-loglevel", "warning"] + ff_inputs + maps + [
        "-c", "copy", "-map_metadata", "1",
        "-metadata:s:v:0",
         f"title=LaserDisc {capture}, 3:2 pulldown removed, 23.976p, "
        f"PAD blips repaired",
        "-disposition:v:0", "default"] + meta + [
        "-color_primaries", "smpte170m", "-color_trc", "smpte170m",
        "-colorspace", "smpte170m", "-color_range", "tv",
        "-t", str(dur), str(out)]

    print(f"episode : {ep.code}  ({ep.title})")
    print(f"tracks  : {' + '.join(tracks)}")
    print(f"video   : {video}")
    print(f"ld src  : {ld_src}")
    print(f"output  : {out}  (-t {dur})")
    if args.dry_run:
        print("\nDRY RUN - ffmpeg " + " ".join(ff_cmd))
        return 0

    p = subprocess.run(ff_cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode != 0:
        raise PipelineError(f"remux failed for {ep.code} (exit {p.returncode}): "
                            + (p.stderr or "")[-800:])
    print(f"created {out} ({out.stat().st_size / 2**30:.3f} GiB)")

    # ---- gate: stream inventory and dispositions ----
    show = ("stream=index,codec_type,codec_name,profile,pix_fmt,width,height,"
            "sample_aspect_ratio,sample_rate,channels,channel_layout,"
            "bits_per_raw_sample:"
            "stream_disposition=default:"
            "stream_tags=language,title")
    p = subprocess.run([ffp, "-v", "error", "-show_entries", show, "-of", "json",
                        str(out)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise PipelineError(f"ffprobe failed on {out}: {p.stderr[-400:]}")
    probe = json.loads(p.stdout or "{}")
    streams = probe.get("streams", [])
    va = [s for s in streams if s.get("codec_type") == "video"]
    aa = [s for s in streams if s.get("codec_type") == "audio"]
    ss = [s for s in streams if s.get("codec_type") == "subtitle"]
    want_a = 2 if want51 else 1
    want_s = 1 if want_srt else 0
    if len(va) != 1 or len(aa) != want_a or len(ss) != want_s:
        raise PipelineError(f"stream counts wrong: video={len(va)} audio={len(aa)} "
                            f"subtitle={len(ss)} (wanted 1/{want_a}/{want_s})")
    if (aa[0].get("disposition") or {}).get("default") != 1:
        raise PipelineError("LD stereo is not the default track")
    if want51 and (aa[1].get("disposition") or {}).get("default") != 0:
        raise PipelineError("DVD 5.1 must be non-default")
    if want_srt and (ss[0].get("disposition") or {}).get("default") != 0:
        raise PipelineError("subtitles must be non-default")
    if not want51 and ss:
        raise PipelineError("unexpected subtitle stream")
    for s in aa + ss:
        if (s.get("tags") or {}).get("language") != "eng":
            raise PipelineError("stream missing language=eng")
    print(f"gate PASS: {len(va)} video, {len(aa)} audio (LD default), "
          f"{len(ss)} subtitle")

    # ---- gate: packet identity for every stream that was stream-copied ----
    pairs = [("video", video, "0:v:0", "0:v:0"),
             ("LD audio", ld_src, "0:a:0", "0:a:0")]
    if want51:
        pairs += track_pairs
    for label, src, src_map, out_map in pairs:
        a, b = packet_md5(ld, src, src_map), packet_md5(ld, out, out_map)
        if a != b:
            raise PipelineError(f"{label} packet hash changed: source={a} output={b}")
        print(f"  packet identity PASS: {label} {b}")

    for s in aa + ss:
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        print(f"  [{s.get('codec_name')}] {tags.get('title')}  "
              f"default={disp.get('default')}  {tags.get('language')}")
    print(f"next: run qc_report.py --episode {ep.code}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
