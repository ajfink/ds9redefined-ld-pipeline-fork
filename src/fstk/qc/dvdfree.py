"""DVD-free timeline check (v38 ``ld_only_sync_check.py``), as a QC gate.

Not an absolute lip-sync measurement — without a DVD or another shared reference that cannot be
measured. It proves that the delivered LD audio is exactly the audio the ``dvd-free-audio`` stage
built and recorded: the operator's no-DVD choice, the native rate and duration residual, the
capture-pad geometry behind AudioSkip, the hashes of native/corrected/synced audio, and an exact
sample budget equal to the video's frame count.
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

FPS_NUM, FPS_DEN = 24000, 1001
MASTER_FPS = 30000.0 / 1001.0
SAMPLE_RATE = 44100
RATE_METHOD = "dvd-free-native-rate"
LEGACY_RATE_METHOD = "dvd-free-duration-pad"   # v38-era recipe: duration rate covering CLV PAD frames
REPORT_SCHEMA = "fstk-dvd-free-audio-timing-v1"


def exact_audio_samples(stream: dict) -> int:
    """Exact sample count from ffprobe ``duration_ts × time_base × sample_rate`` (must be an integer)."""
    try:
        exact = int(stream["duration_ts"]) * Fraction(stream["time_base"]) * int(stream["sample_rate"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"audio stream lacks exact duration/time-base metadata: {stream}") from exc
    if exact.denominator != 1:
        raise ValueError(f"audio duration does not resolve to an integer sample count: {exact}")
    return exact.numerator


def target_samples(frames: int) -> int:
    return round(frames * FPS_DEN * SAMPLE_RATE / FPS_NUM)


def expected_skip(delta_s: float, pad_frames: int) -> float:
    """AudioSkip from capture-pad geometry: WAV sample 0 sits ``pad_frames`` master frames in."""
    return float(delta_s) - int(pad_frames) / MASTER_FPS


def ld_correction(values: dict[str, Any]) -> tuple[float, float]:
    """(offset_ms, drift_ppm) the reviewer-driven A/V correction applies to the LD track."""
    cur = ((values.get("audio.av_correction") or {}).get("ld") or {}).get("current") or {}
    return float(cur.get("offset_ms", 0.0)), float(cur.get("drift_ppm", 0.0))


def corrected_skip(base_skip_s: float, offset_ms: float, drift_ppm: float) -> float:
    """AudioSkip after a correction: delaying audio by D(t) = offset + drift·t stretches the corrected
    timeline by (1 + drift) and starts ``offset`` earlier in it."""
    return base_skip_s * (1.0 + drift_ppm * 1e-6) - offset_ms / 1000.0


def start_sample(skip_s: float) -> int:
    return math.floor(skip_s * SAMPLE_RATE + 1e-9)


def check_dvd_free(values: dict[str, Any], report: dict, *, native_sha256: str, corrected_sha256: str,
                   synced_sha256: str, synced_stream: dict, delivered_stream: dict) -> list[str]:
    """Every reason the DVD-free timeline is not proven (empty list = PASS)."""
    v = values
    issues: list[str] = []
    if v.get("mode.dvd_reference_available") is not False or v.get("mode.no_dvd_proceed_approved") is not True:
        issues.append("the episode is not an operator-approved no-DVD episode")
    if list(v.get("deliverable.tracks") or ["ld"]) != ["ld"]:
        issues.append("a no-DVD delivery must contain LD stereo only")
    method = v.get("audio.rate_method")
    if v.get("audio.dvd_free_approved") is not True or method not in (RATE_METHOD, LEGACY_RATE_METHOD):
        issues.append("no promoted DVD-free audio recipe in the manifest")
    if v.get("audio.origin_verified_by_duration") is not True:
        issues.append("DVD-free capture-origin/duration evidence is not marked verified")
    if report.get("schema") != REPORT_SCHEMA or report.get("dvd_opened") is not False:
        issues.append("timing report is not an fstk DVD-free report that never opened the DVD")
    rate = float(report.get("duration_candidate_effective_rate_hz") or 0.0)
    if rate <= 0 or abs(rate - float(v.get("audio.duration_candidate_rate_hz") or 0.0)) > 1e-6:
        issues.append("manifest duration-rate value differs from the timing report")
    residual = report.get("audio_minus_projected_content_frames")
    tolerance = float(v.get("audio.endpoint_tolerance_frames") or 1.0)
    if residual is None or abs(float(residual)) > tolerance:
        issues.append(f"duration endpoint residual {residual!r} exceeds ±{tolerance:.3f} frames")
    if native_sha256 != report.get("source_audio_sha256"):
        issues.append("native EFM WAV hash differs from the timing report")
    if corrected_sha256 != (report.get("corrected") or {}).get("output_sha256"):
        issues.append("corrected LD audio hash differs from the timing report")
    synced = report.get("synced") or {}
    if synced_sha256 != synced.get("output_sha256"):
        issues.append("synced LD audio hash differs from the timing report")
    frames, delta, pad = v.get("video.frames"), v.get("video.delta_s"), v.get("audio.pad_frames")
    skip = v.get("audio.skip_s")
    if not frames or delta is None or pad is None or skip is None:
        issues.append("video.frames, video.delta_s, audio.pad_frames and audio.skip_s are required")
        return issues
    offset_ms, drift_ppm = ld_correction(v)
    want_skip = corrected_skip(expected_skip(delta, pad), offset_ms, drift_ppm)
    if abs(float(skip) - want_skip) > 0.5 / SAMPLE_RATE:
        issues.append(f"audio.skip_s {float(skip):.9f} disagrees with capture-pad geometry {want_skip:.9f}s "
                      f"(A/V correction {offset_ms:+.1f} ms, {drift_ppm:+.1f} ppm) by more than half a sample")
    eff = report.get("effective_rate_hz")
    base = rate if method == LEGACY_RATE_METHOD else float(SAMPLE_RATE)
    if eff is None or abs(float(eff) - base / (1.0 + drift_ppm * 1e-6)) > 1e-6:
        issues.append(f"applied rate {eff!r} Hz is not {base:.6f} Hz with the recorded drift "
                      f"correction ({drift_ppm:+.1f} ppm)")
    applied = report.get("av_correction") or {"offset_ms": 0.0, "drift_ppm": 0.0}
    if (abs(float(applied.get("offset_ms", 0)) - offset_ms) > 1e-6
            or abs(float(applied.get("drift_ppm", 0)) - drift_ppm) > 1e-6):
        issues.append("the delivered LD audio was built with a different A/V correction than the manifest's")
    if abs(float(synced.get("skip_s") or 0.0) - float(skip)) > 0.5 / SAMPLE_RATE:
        issues.append("manifest AudioSkip differs from the timing report's synced recipe")
    if int(synced.get("start_sample", -1)) != start_sample(float(skip)):
        issues.append("synced audio start sample differs from the recipe")
    if (delivered_stream.get("codec_name") != "flac" or str(delivered_stream.get("sample_rate")) != str(SAMPLE_RATE)
            or int(delivered_stream.get("channels") or 0) != 2):
        issues.append(f"delivered LD track has an unexpected format: {delivered_stream.get('codec_name')} "
                      f"{delivered_stream.get('sample_rate')} Hz {delivered_stream.get('channels')} ch")
    want = target_samples(int(frames))
    try:
        actual = exact_audio_samples(synced_stream)
    except ValueError as exc:
        issues.append(str(exc))
    else:
        if actual != want:
            issues.append(f"synced audio has {actual} samples; {int(frames)} video frames need {want}")
    if int(synced.get("target_samples", -1)) != want:
        issues.append("timing report target sample count disagrees with video.frames")
    return issues
