"""Stream-copy remux recipe and container gates (pure).

From v38 ``remux_padfixed_mac.py`` (unchanged behaviour):
* stream copy only — audio corrections rebuild their own file and remux; x265 is never re-run;
* LD stereo is always present, first, and the default track; DVD 5.1 and SRT are optional,
  non-default, and come from their own retimed files;
* every audio/subtitle stream is tagged ``language=eng``;
* ``-max_interleave_delta 0``: Matroska's default 10 s interleave flush produced video-only
  clusters, so fast seeks could land without audio (confirmed on the M5 Mac);
* each copied stream's packet MD5 must equal its source's (identity proof, checked by the stage).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fstk.core.errors import ArtifactError


def remux_args(*, video: Path, ld_audio: Path, fiveone: Path | None, srt: Path | None, capture: str,
               duration_s: float, out: Path) -> tuple[list[str], list[tuple[str, int, str, str]]]:
    """Return FFmpeg args and the (label, input_index, source_selector, output_selector) identity pairs."""
    inputs = ["-i", str(video), "-i", str(ld_audio)]
    maps = ["-map", "0:v:0", "-map", "1:a:0"]
    meta = ["-metadata:s:a:0", "title=LD 2.0 (original Dolby Surround)",
            "-metadata:s:a:0", "language=eng", "-disposition:a:0", "default"]
    pairs = [("video", 0, "0:v:0", "0:v:0"), ("LD audio", 1, "0:a:0", "0:a:0")]
    index = 2
    if fiveone is not None:
        inputs += ["-i", str(fiveone)]
        maps += ["-map", f"{index}:a:0"]
        meta += ["-metadata:s:a:1", "title=DVD 5.1 (retimed)", "-metadata:s:a:1", "language=eng",
                 "-disposition:a:1", "0"]
        pairs.append(("DVD 5.1", index, "0:a:0", "0:a:1"))
        index += 1
    if srt is not None:
        inputs += ["-i", str(srt)]
        maps += ["-map", f"{index}:s:0"]
        meta += ["-metadata:s:s:0", "title=English SDH (retimed)", "-metadata:s:s:0", "language=eng",
                 "-disposition:s:0", "0"]
    args = ["-y", "-loglevel", "warning", *inputs, *maps,
            "-c", "copy", "-map_metadata", "1",
            "-metadata:s:v:0", f"title=LaserDisc {capture}, 3:2 pulldown removed, 23.976p, PAD blips repaired",
            "-disposition:v:0", "default", *meta,
            "-color_primaries", "smpte170m", "-color_trc", "smpte170m", "-colorspace", "smpte170m",
            "-color_range", "tv", "-max_interleave_delta", "0", "-t", str(duration_s), str(out)]
    return args, pairs


def check_inventory(streams: list[dict[str, Any]], *, want_51: bool, want_srt: bool) -> list[str]:
    va = [s for s in streams if s.get("codec_type") == "video"]
    aa = [s for s in streams if s.get("codec_type") == "audio"]
    ss = [s for s in streams if s.get("codec_type") == "subtitle"]
    want_a, want_s = (2 if want_51 else 1), (1 if want_srt else 0)
    issues = []
    if (len(va), len(aa), len(ss)) != (1, want_a, want_s):
        issues.append(f"stream inventory video={len(va)} audio={len(aa)} subtitle={len(ss)} "
                      f"(wanted 1/{want_a}/{want_s})")
        return issues
    if (aa[0].get("disposition") or {}).get("default") != 1:
        issues.append("LD stereo is not the default track")
    if want_51 and (aa[1].get("disposition") or {}).get("default") != 0:
        issues.append("DVD 5.1 must be non-default")
    if want_srt and (ss[0].get("disposition") or {}).get("default") != 0:
        issues.append("subtitles must be non-default")
    for s in aa + ss:
        if (s.get("tags") or {}).get("language") != "eng":
            issues.append(f"stream {s.get('index')} missing language=eng")
    return issues


def ld_audio_key(manifest) -> str:
    """Artifact key of the LD stereo to deliver: the click/gap-repaired file when ``audio-quality`` made one
    (it only exists when something was actually repaired), else the operator's/stage's chosen source."""
    if manifest.artifact("ld_audio_finished"):
        return "ld_audio_finished"
    return manifest.require("deliverable.ld_audio_source")


def tracks_from_settings(mux_tracks: list[str]) -> tuple[bool, bool]:
    if not mux_tracks or mux_tracks[0] != "ld":
        raise ArtifactError("mux_tracks must start with 'ld'")
    return "51" in mux_tracks, "srt" in mux_tracks
