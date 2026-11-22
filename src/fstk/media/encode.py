"""Final video encode recipe and encoded-stream checks (pure).

Carried verbatim from v38 (``encode_episode_mac.py`` / ``encode_episode.ps1`` /
``validate_encoded_video.py``): one single-pass x265 encode, CRF 14 / preset slow / tune grain /
Main 10, SMPTE 170M colour signalling, limited range, video only. SAR is applied
as a DAR ``-aspect`` *output option*, never ``-vf setsar=n:d`` (inside a filtergraph ``:``
separates options, so ``setsar=121:144`` silently set the wrong value — v38 V5 fix).
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Any

from fstk.core.errors import ArtifactError

X265_PARAMS = ("colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:"
               "range=limited:log-level=error:info=0")
OUTPUT_RATE = Fraction(24000, 1001)
DURATION_TOLERANCE_S = 0.002

#: Chroma of the delivered video. The stack master is ``yuv422p10le``, so **422 keeps the master's
#: chroma untouched** (owner decision 2026-11-21, audit 1 item A1: better than resampling it well).
#: 420 is the v38 output and the compatible one: 4:2:2 HEVC decodes in software (ffmpeg, mpv, VLC)
#: but is outside most hardware decoders and Apple devices, so 420 stays available per episode.
#: ``profile`` is what ffprobe prints: HEVC range-extension profiles (every 4:2:2) all report ``Rext``;
#: the pixel format carries the actual chroma check.
CHROMA = {
    "422": {"pix_fmt": "yuv422p10le", "profile": "Rext", "x265_profile": "main422-10",
            "vs_format": "vs.YUV422P10", "subsampling": (1, 0)},
    "420": {"pix_fmt": "yuv420p10le", "profile": "Main 10", "x265_profile": "main10",
            "vs_format": "vs.YUV420P10", "subsampling": (1, 1)},
}
DEFAULT_CHROMA = "422"
EXPECTED_PROFILE = CHROMA["420"]["profile"]        # kept for the v38-era deliverables already made
EXPECTED_PIX_FMT = CHROMA["420"]["pix_fmt"]


def chroma_spec(chroma: str | None) -> dict:
    spec = CHROMA.get(chroma or DEFAULT_CHROMA)
    if spec is None:
        raise ArtifactError(f"unknown chroma {chroma!r}; expected one of {sorted(CHROMA)}")
    return spec


def display_aspect(sar: str, width: int, height: int) -> str:
    if not re.fullmatch(r"\d+:\d+", sar or ""):
        raise ArtifactError(f"unexpected SAR {sar!r} from the stack master")
    num, den = (int(x) for x in sar.split(":"))
    if num <= 0 or den <= 0 or width <= 0 or height <= 0:
        raise ArtifactError("SAR and raster must be positive")
    return f"{num * width}:{den * height}"


def final_encode_args(*, aspect: str, frames: int, duration_s: float, capture: str, out: str,
                      chroma: str | None = None) -> list[str]:
    """FFmpeg arguments after the ``-f yuv4mpegpipe -i -`` input (v38 recipe; chroma per episode)."""
    if frames <= 0 or duration_s <= 0:
        raise ArtifactError("measured frames and duration are required for the final encode")
    spec = chroma_spec(chroma)
    return ["-map", "0:v:0", "-an", "-sn", "-aspect", aspect,
            "-c:v", "libx265", "-profile:v", spec["x265_profile"], "-pix_fmt", spec["pix_fmt"],
            "-crf", "14", "-preset", "slow", "-tune", "grain", "-x265-params", X265_PARAMS,
            "-color_primaries", "smpte170m", "-color_trc", "smpte170m",
            "-colorspace", "smpte170m", "-color_range", "tv",
            "-frames:v", str(frames), "-t", str(duration_s),
            "-metadata:s:v:0", f"title=LaserDisc {capture}, measured IVTC with source PAD repairs, 23.976p",
            "-max_muxing_queue_size", "8192", out]


def check_encoded_stream(stream: dict[str, Any], *, probe_frames: int, decoded_frames: int | None,
                         decode_errors: list[str], duration_s: float | None, expected_frames: int,
                         expected_duration_s: float, expected_raster: tuple[int, int] | None,
                         chroma: str | None = None) -> list[str]:
    """v38 ``validate_encoded_video`` checks, returned as issues (empty = pass)."""
    issues = []
    if probe_frames != expected_frames:
        issues.append(f"decoded video frames {probe_frames} != measured {expected_frames}")
    if expected_raster and (stream.get("width"), stream.get("height")) != expected_raster:
        issues.append(f"encoded raster is {stream.get('width')}x{stream.get('height')}, "
                      f"expected {expected_raster[0]}x{expected_raster[1]}")
    spec = chroma_spec(chroma)
    if stream.get("profile") != spec["profile"]:
        issues.append(f"encoded profile is {stream.get('profile')!r}, expected {spec['profile']}")
    if stream.get("pix_fmt") != spec["pix_fmt"]:
        issues.append(f"encoded pixel format is {stream.get('pix_fmt')!r}, expected {spec['pix_fmt']}")
    if stream.get("r_frame_rate") not in ("24000/1001",):
        issues.append(f"encoded rate is {stream.get('r_frame_rate')!r}, expected 24000/1001")
    if decoded_frames != expected_frames:
        issues.append(f"full FFmpeg null decode counted {decoded_frames}; expected {expected_frames}")
    if decode_errors:
        issues.append(f"decoder reported {len(decode_errors)} error line(s): {decode_errors[:3]}")
    if duration_s is None or abs(duration_s - expected_duration_s) > DURATION_TOLERANCE_S:
        issues.append(f"encoded duration {duration_s} differs from measured {expected_duration_s} "
                      f"by more than {DURATION_TOLERANCE_S}s")
    return issues
