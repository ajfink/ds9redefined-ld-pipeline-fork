#!/usr/bin/env python3
"""Validate the source-derived script, then make the single final x265 encode.

    python scripts/encode_episode_mac.py --episode S01E10 --pass validate
    python scripts/encode_episode_mac.py --episode S01E10 --pass final --dry-run

macOS counterpart of Workflow Version 5's ``encode_episode.ps1``. That script
feeds FFmpeg with ``-f avisynth -i <avs>``, a demuxer this FFmpeg build does not
have on macOS, so here ``vspipe -c y4m`` is piped into ``-f yuv4mpegpipe``.

The V5 contract, carried over
-----------------------------
* **validate** probes script metadata and decodes three frames to the null
  muxer. It performs **no video encode**; frames/duration are taken from the
  non-encoding probe, never from an encode's output.
* **final** reads ``avs_padfix`` (the source-derived PAD-repair script), writes
  **video only** to ``padfixed_video``, and encodes **exactly once**. Corrected
  audio is attached later by the stream-copy remux.
* ``encode_padfixed.ps1`` / its macOS port are **retired** in V4; this script
  never calls them and refuses a stale PAD script.
* Refuses to overwrite an output or a leftover ``.tmp.mkv``; rejects two-part
  E01 exactly as the Windows script does.

The ``-aspect`` fix (V5)
------------------------
SAR is applied as a DAR ``-aspect`` **output option**, never ``-vf
setsar=<n>:<d>``. Inside a filtergraph ``:`` separates filter options, so
``setsar=121:144`` sets num=121 and feeds 144 to setsar's unrelated ``max``
option - FFmpeg 9.0.2 then muxed SAR 121:1 (DAR 192:1) instead of 121:144.
``-aspect`` takes a plain rational with no filtergraph parsing, and the
container derives SAR exactly. DAR = SAR x width/height.

Documented substitutions (Windows -> macOS)
-------------------------------------------
| Windows | macOS |
|---|---|
| ``-f avisynth -i <avs>`` | ``vspipe -c y4m <vpy> -`` piped into ``-f yuv4mpegpipe -i -`` |
| ``ffprobe -f avisynth -i <avs> -show_entries stream=nb_frames,duration,...`` | ``vspipe --info <vpy>`` (frames/FPS/raster/format) |
| ``Invoke-LdFfmpeg`` (stderr-safe capture) | ``vspipe_util.pipe`` appending both processes to one log |
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # sibling modules (vspipe_util)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ldcommon
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402
from vspipe_util import (  # noqa: E402
    X265_PARAMS, pipe, probe_sar, vspipe_bin, vspipe_env)

PASSES = ("validate", "final")


def workflow_version() -> str:
    p = Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION"
    if not p.exists():
        raise PipelineError(f"workflow version marker missing: {p}")
    v = p.read_text(encoding="ascii").strip()
    if not v.isdecimal() or int(v) < 1:
        raise PipelineError(f"invalid workflow version marker: {v!r}")
    return v


def vspipe_info(vpy: Path) -> dict:
    """Probe a VapourSynth script the way V5 probes the AVS with ffprobe."""
    p = subprocess.run([vspipe_bin(), "--info", str(vpy)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=vspipe_env())
    text = (p.stdout or "") + "\n" + (p.stderr or "")
    if p.returncode != 0:
        raise PipelineError(
            f"vspipe could not inspect {vpy} (exit {p.returncode}): {text[-800:]}")

    def grab(key: str) -> str | None:
        m = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", text)
        return m.group(1) if m else None

    frames_s, fps_s = grab("Frames"), grab("FPS")
    if not frames_s or not frames_s.isdigit():
        raise PipelineError(f"AVS/script did not report a frame count: {vpy}\n{text[-500:]}")
    m = re.match(r"(\d+)/(\d+)", fps_s or "")
    if not m:
        raise PipelineError(f"could not parse frame rate {fps_s!r} from {vpy}")
    num, den = int(m.group(1)), int(m.group(2))
    frames = int(frames_s)
    return {
        "frames": frames,
        "fps_num": num,
        "fps_den": den,
        "duration": frames * den / num,
        "width": int(grab("Width") or 0),
        "height": int(grab("Height") or 0),
        "format": grab("Format Name") or "",
    }


def check_padfix_script(padfix: Path, base: Path, version: str) -> None:
    """V5's staleness gate, re-expressed for a .vpy.

    The Windows check requires ``Import("<baseAvs>")``, the
    ``# LD_PIPELINE_WORKFLOW_VERSION=<n>`` line, and the *absence* of
    ``FFVideoSource(``. Here the equivalents are: the script names the
    source-derived chain as its ``BASE``, carries the version header, and
    never opens encoded video.
    """
    text = padfix.read_text(encoding="utf-8", errors="replace")
    if not re.search(rf"(?m)^# LD_PIPELINE_WORKFLOW_VERSION={re.escape(version)}\s*$",
                     text):
        raise PipelineError(
            f"PAD script is stale: missing "
            f"'# LD_PIPELINE_WORKFLOW_VERSION={version}'. Preserve it, rebuild "
            f"with build_padfix_vpy.py, then validate.")
    if str(base) not in text:
        raise PipelineError(
            f"PAD script does not import the source-derived chain {base}. "
            f"Preserve it, rebuild with build_padfix_vpy.py, then validate.")
    if re.search(r"FFVideoSource\s*\(", text) or re.search(r"['\"][^'\"]+\.mkv['\"]",
                                                           text) or "encode_full" in text:
        raise PipelineError(
            f"PAD script is derived from encoded video rather than the "
            f"source-derived chain: {padfix}. Rebuild with build_padfix_vpy.py.")


def choose_script(ep, base: Path, padfix: Path, version: str) -> Path:
    """V5: final always uses avs_padfix; validate uses it only if current."""
    if not base.is_file():
        raise PipelineError(f"source-derived script missing: {base}")
    if padfix.is_file():
        check_padfix_script(padfix, base, version)
        return padfix
    return base


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--pass", dest="pass_", choices=PASSES, default="final")
    ap.add_argument("--output", help="override the final .mkv path")
    ap.add_argument("--dry-run", action="store_true", help="print the command, do not run")
    ap.add_argument("--planned-gib", type=float,
                    help="run check_space_budget for this stage first; refuse on FAIL")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    capture = (ep.data.get("audio") or {}).get("capture")
    if not capture:
        raise PipelineError(f"{ep.code}: audio.capture is not measured; select the source WAV before encoding")
    if ep.data.get("format") == "two_part" or ep.data.get("record_status") == "measured-partial":
        raise PipelineError(
            f"{ep.code} is a two-part/partial record; use its dedicated per-part "
            f"procedure, not the generic encoder.")

    version = workflow_version()
    base = ep.path("avs_encode")
    padfix = ep.path("avs_padfix")
    if args.pass_ == "final" and not padfix.is_file():
        raise PipelineError(
            f"source-derived PAD-repair script is required before the one final "
            f"encode: {padfix}; run the PAD audit/replacement/build stages first")
    script = choose_script(ep, base, padfix, version)

    ff, ffp = ld.tool("ffmpeg"), ld.tool("ffprobe")
    log = ld.root("logs") / f"encode-{ep.code}-{args.pass_}.log"
    info = vspipe_info(script)

    if args.pass_ == "validate":
        # Metadata first, then a three-frame decode to the null muxer. No encode.
        expected = ep.data["video"].get("frames")
        if expected is not None and int(expected) != info["frames"]:
            raise PipelineError(
                f"script reports {info['frames']} frames but manifest "
                f"video.frames={expected}")
        want_dur = ep.data["video"].get("duration_s")
        if want_dur is not None and abs(info["duration"] - float(want_dur)) > 0.002:
            raise PipelineError(
                f"script duration {info['duration']:.6f}s disagrees with manifest "
                f"video.duration_s={want_dur}")
        ff_cmd = [str(ff), "-hide_banner", "-v", "error",
                  "-f", "yuv4mpegpipe", "-i", "-",
                  "-frames:v", "3", "-an", "-sn", "-f", "null", "-"]
        # -e is inclusive, so this requests exactly frames 0..2 and vspipe
        # finishes on its own. Emitting the whole chain and letting FFmpeg stop
        # at -frames:v 3 instead closes the pipe under vspipe, which then exits
        # non-zero purely because it was cut short.
        vs_cmd = [vspipe_bin(), "-s", "0", "-e", "2", "-c", "y4m", str(script), "-"]
        print(f"episode : {ep.code}  ({ep.title})")
        print(f"pass    : {args.pass_}")
        print(f"script  : {script}")
        print(f"metadata: {info['frames']} frames  duration={info['duration']:.6f}s  "
              f"rate={info['fps_num']}/{info['fps_den']}  "
              f"raster={info['width']}x{info['height']}  format={info['format']}")
        if args.dry_run:
            print("\nDRY RUN - vspipe plus 3-frame null decode")
            print("  " + " ".join(vs_cmd))
            print("  | " + " ".join(ff_cmd))
            return 0
        log.parent.mkdir(parents=True, exist_ok=True)
        rc = pipe(vs_cmd, ff_cmd, log, vspipe_env(), require_vspipe_ok=False)
        if rc != 0:
            raise PipelineError(f"sample decode failed with exit {rc}; inspect {log}")
        print("VALIDATE PASS - script metadata and first three decoded frames; "
              "no video encode was performed.")
        return 0

    # ---------------- final pass: one video-only encode ----------------
    dst = Path(args.output) if args.output else ep.path("padfixed_video")
    if dst.suffix.lower() != ".mkv":
        raise PipelineError(f"final video output must be .mkv: {dst}")
    frames = ep.frame_count                       # refuses if unmeasured
    duration = ep.data["video"].get("duration_s")
    if duration is None:
        raise PipelineError("manifest has no video.duration_s - measure it first")

    sar = probe_sar(Path(ffp), ep.path("stack_master"))
    if not sar or not re.match(r"^\d+:\d+$", sar):
        raise PipelineError(f"unexpected SAR {sar!r} from the stack master")
    num, den = (int(x) for x in sar.split(":"))
    p = subprocess.run([str(ffp), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0",
                        str(ep.path("stack_master"))],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    wh = (p.stdout or "").strip().split(",")
    if p.returncode or len(wh) != 2 or not all(x.isdigit() for x in wh):
        raise PipelineError(f"could not read stack dimensions: {p.stdout!r} {p.stderr[-300:]}")
    aspect = f"{num * int(wh[0])}:{den * int(wh[1])}"

    tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
    vs_cmd = [vspipe_bin(), "-c", "y4m", str(script), "-"]
    ff_cmd = [str(ff), "-y", "-hide_banner", "-stats", "-stats_period", "30",
              "-f", "yuv4mpegpipe", "-i", "-",
              "-map", "0:v:0", "-an", "-sn", "-aspect", aspect,
              "-c:v", "libx265", "-profile:v", "main10", "-pix_fmt", "yuv420p10le",
              "-crf", "14", "-preset", "slow", "-tune", "grain",
              "-x265-params", X265_PARAMS,
              "-color_primaries", "smpte170m", "-color_trc", "smpte170m",
              "-colorspace", "smpte170m", "-color_range", "tv",
              "-frames:v", str(frames), "-t", str(duration),
              "-metadata:s:v:0",
               f"title=LaserDisc {capture}, measured IVTC with source "
              f"PAD repairs, 23.976p",
              "-max_muxing_queue_size", "8192", str(tmp)]

    print(f"episode : {ep.code}  ({ep.title})")
    print(f"pass    : {args.pass_}")
    print(f"script  : {script}")
    print(f"output  : {dst}")
    print(f"frames  : {frames}  duration={duration}  SAR={sar}  aspect={aspect}")
    if args.dry_run:
        print("\nDRY RUN")
        print("  " + " ".join(vs_cmd))
        print("  | " + " ".join(ff_cmd))
        return 0

    if dst.exists():
        raise PipelineError(f"Refusing to overwrite existing output: {dst}")
    if tmp.exists():
        raise PipelineError(f"Temporary output exists; inspect it before retrying: {tmp}")
    if args.planned_gib is not None:
        gate = subprocess.run([sys.executable,
                               str(Path(__file__).with_name("check_space_budget.py")),
                               "--stage", "encode", "--planned-gib", str(args.planned_gib)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        print((gate.stdout or "").strip())
        if gate.returncode != 0:
            raise PipelineError("space gate did not pass - not starting the encode")

    dst.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    rc = pipe(vs_cmd, ff_cmd, log, vspipe_env())
    mins = (time.time() - started) / 60.0
    print(f"ffmpeg exit={rc}  elapsed={mins:.1f} min  log={log}")
    if rc != 0:
        raise PipelineError(f"Encode failed with exit {rc}; inspect {log} "
                            f"and the retained temporary file {tmp}")
    if not tmp.is_file():
        raise PipelineError(f"ffmpeg reported success but output is missing: {tmp}")
    tmp.replace(dst)
    print(f"created {dst} ({dst.stat().st_size / 2**30:.3f} GiB)")
    print("The source-derived PAD-repaired script was encoded once (video-only); "
          "next stream-copy the validated LD audio and run delivery QC.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
