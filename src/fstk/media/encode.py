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


PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
           "placebo")
DEFAULT_PRESET = "slow"


def final_encode_args(*, aspect: str, frames: int, duration_s: float, capture: str, out: str,
                      chroma: str | None = None, preset: str | None = None, settings=None,
                      pass_no: int | None = None, stats: str | None = None) -> list[str]:
    """FFmpeg arguments after the ``-f yuv4mpegpipe -i -`` input.

    ``settings`` (``fstk.config.output.OutputSettings``) picks the encoder and quality; without it the
    v38 recipe is used (x265 CRF 14 / slow / tune grain). ``chroma``/``preset`` override the settings
    (per-episode chroma decision; smoke-run presets). Pass 1 of a two-pass encode writes nowhere."""
    from fstk.config.output import DEFAULTS, OutputSettings, video_args

    if frames <= 0 or duration_s <= 0:
        raise ArtifactError("measured frames and duration are required for the final encode")
    if settings is None:
        settings = OutputSettings(dict(DEFAULTS["video"]), dict(DEFAULTS["audio"]))
    video = dict(settings.video)
    if chroma is not None:
        chroma_spec(chroma)
        video["chroma"] = chroma
    if preset is not None:
        video["preset"] = preset
    enc = settings.encoder
    if video["chroma"] not in enc.chromas:
        raise ArtifactError(f"{enc.label} cannot encode {video['chroma']} chroma")
    if enc.presets and str(video["preset"]) not in enc.presets:
        raise ArtifactError(f"unknown {enc.label} preset {video['preset']!r}")
    settings = OutputSettings(video, settings.audio, settings.overridden)
    codec = video_args(settings, base_params=X265_PARAMS, pass_no=pass_no, stats=stats)
    target = ["-f", "null", "-"] if pass_no == 1 else [out]
    title = ["-metadata:s:v:0", f"title=LaserDisc {capture}, measured IVTC, 23.976p"]
    return ["-map", "0:v:0", "-an", "-sn", "-aspect", aspect, *codec,
            "-color_primaries", "smpte170m", "-color_trc", "smpte170m",
            "-colorspace", "smpte170m", "-color_range", "tv",
            "-frames:v", str(frames), "-t", str(duration_s),
            *([] if pass_no == 1 else title),
            "-max_muxing_queue_size", "8192", *target]


def check_encoded_stream(stream: dict[str, Any], *, probe_frames: int, decoded_frames: int | None,
                         decode_errors: list[str], duration_s: float | None, expected_frames: int,
                         expected_duration_s: float, expected_raster: tuple[int, int] | None,
                         chroma: str | None = None, expected: dict[str, Any] | None = None) -> list[str]:
    """v38 ``validate_encoded_video`` checks, returned as issues (empty = pass)."""
    issues = []
    if probe_frames != expected_frames:
        issues.append(f"decoded video frames {probe_frames} != measured {expected_frames}")
    if expected_raster and (stream.get("width"), stream.get("height")) != expected_raster:
        issues.append(f"encoded raster is {stream.get('width')}x{stream.get('height')}, "
                      f"expected {expected_raster[0]}x{expected_raster[1]}")
    spec = expected or chroma_spec(chroma)
    if spec.get("codec_name") and stream.get("codec_name") != spec["codec_name"]:
        issues.append(f"encoded codec is {stream.get('codec_name')!r}, expected {spec['codec_name']}")
    if spec.get("profile") and stream.get("profile") != spec["profile"]:
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
