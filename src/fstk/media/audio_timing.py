"""LD audio timing math (pure; no processes).

Ported verbatim from v38:
* ``audio_timing_diagnostic.py`` – duration (no-DVD) rate model, intermediate 2× asetrate,
  resampler filter chains, space plan;
* ``measure_audio_rate.py`` – envelope cross-correlation, robust Theil–Sen line fit with
  leave-one-window-out range, saved-report summaries;
* ``build_ld_synced.py`` – sample-accurate AudioSkip and output sample budget.

Conventions (unchanged from v38; documented in the runbook):
* master clock 30000/1001; output 24000/1001; native EFM clock 44100 Hz;
* ``asetrate = 44100 / slope`` where slope maps WAV seconds → master seconds;
* rate is applied as ``aresample=88200, asetrate=<2×rate>, aresample=44100`` so the
  candidate rate is quantised to 0.5 Hz;
* every result here is evidence, never an approval (``is_approval``/``candidate_is_approval``
  are always False). No threshold or origin convention that the runbook leaves undefined is
  invented here.
"""
from __future__ import annotations

import csv
import hashlib
import math
import wave
from datetime import UTC, datetime
from pathlib import Path

from fstk.core.errors import ArtifactError as PipelineError

FPS = 30000.0 / 1001.0
BASE_RATE = 44100
INTERMEDIATE_FACTOR = 2
NO_DVD_TOLERANCE_FRAMES = 2.0   # measured +0.32 (E06) and +1.32 (E07) against the pad-free stack (ADR 0004)
NO_DVD_PAD_TOLERANCE_FRAMES = NO_DVD_TOLERANCE_FRAMES   # old name
GIB = 1024 ** 3
# measure_audio_rate.py
SR = 8000
HOP = 0.05
WINDOW = 50.0
SEARCH = 8.0
RUNNER_EXCLUDE = 1.0
MIN_FIT_CORR = 0.50   # report-screen only; never by itself approves a correction
# build_ld_synced.py
SAMPLE_RATE = 44100
FPS_NUM, FPS_DEN = 24000, 1001


def no_dvd_audio_plan_gib(source_bytes: int) -> float:
    """Conservative peak estimate for corrected + synced FLACs and one temp output."""
    if source_bytes <= 0:
        raise PipelineError("native EFM WAV size must be positive for the no-DVD audio space estimate")
    return max(0.5, 3.0 * source_bytes / GIB)


def wav_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as w:
            if w.getcomptype() != "NONE":
                raise PipelineError(f"native EFM input must be uncompressed PCM WAV: {path}")
            return {
                "frames": w.getnframes(),
                "sample_rate": w.getframerate(),
                "channels": w.getnchannels(),
                "sample_width_bytes": w.getsampwidth(),
                "duration_s": w.getnframes() / w.getframerate(),
            }
    except (wave.Error, OSError) as e:
        raise PipelineError(f"cannot read exact PCM WAV sample count from {path}: {e}") from e


def summarize_timing(sample_rate: int, sample_count: int, stack_frames: int) -> dict:
    """The no-DVD duration model: the native EFM audio spans the same disc time as the stack.

    The stack has no inserted frames (ADR 0004 removed Orc's CLV time-code PADs), so the audio's
    frame-equivalent length should equal ``stack_frames``; the residual is checked by the caller. The key
    ``audio_minus_projected_content_frames`` keeps its v38 name so earlier reports still read the same.
    """
    if sample_rate <= 0 or sample_count <= 0 or stack_frames <= 0:
        raise PipelineError("sample rate, sample count, and stack frame count must be positive")
    raw_duration = sample_count / sample_rate
    stack_duration = stack_frames / FPS
    raw_equiv_frames = raw_duration * FPS
    rate = sample_rate * raw_duration / stack_duration
    intermediate_rate = round(INTERMEDIATE_FACTOR * rate)
    effective_rate = intermediate_rate / INTERMEDIATE_FACTOR
    return {
        "native_audio_duration_s": raw_duration,
        "native_audio_equiv_video_frames": raw_equiv_frames,
        "stack_frames": stack_frames,
        "stack_duration_s": stack_duration,
        "duration_delta_frames_stack_minus_audio": stack_frames - raw_equiv_frames,
        "audio_minus_projected_content_frames": raw_equiv_frames - stack_frames,
        "duration_candidate_effective_rate_hz": rate,
        "duration_candidate_two_x_asetrate_hz": intermediate_rate,
        "duration_candidate_effective_rate_rounded_hz": effective_rate,
        "duration_candidate_quantization_error_hz": effective_rate - rate,
        "dvd_opened": False,
        "is_approval": False,
        "caveat": "Duration estimate only. Verify matching endpoints, EFM structural trim and origin; do not auto-commit.",
    }


def rate_filter(rate_2x: int, use_soxr: bool) -> tuple[str, str]:
    if rate_2x == BASE_RATE * INTERMEDIATE_FACTOR:
        return "anull", "none (rate unchanged)"
    if use_soxr:
        return (f"aresample={BASE_RATE * INTERMEDIATE_FACTOR}:resampler=soxr:precision=28,"
                f"asetrate={rate_2x},aresample={BASE_RATE}:resampler=soxr:precision=28"), "soxr precision=28"
    # macOS's tested static FFmpeg 9.0.2 build has no libsoxr. FFmpeg's default
    # swr path is the established fallback; record it rather than failing late.
    return (f"aresample={BASE_RATE * INTERMEDIATE_FACTOR},asetrate={rate_2x},"
            f"aresample={BASE_RATE}"), "FFmpeg default resampler (swr)"


def diagnostic_skip(delta_s: float, pad_frames: int, override: float | None = None) -> float:
    """Window-center estimate only; a constant error is absorbed by the fit intercept."""
    return float(override) if override is not None else float(delta_s) - int(pad_frames) / FPS


def envelope(samples):
    import numpy as np
    hop = int(SR * HOP)
    n = len(samples) // hop
    if n == 0:
        return np.empty(0, np.float64)
    frames = samples[:n*hop].reshape(n, hop).astype(np.float64)
    return np.log1p(np.sqrt(np.mean(frames * frames, axis=1)))


def slide_corr(x, y):
    """Pearson correlation of x with each valid-length window in y."""
    import numpy as np
    x = x.astype(np.float64, copy=True)
    y = y.astype(np.float64, copy=False)
    x -= x.mean()
    n, m = len(x), len(y)
    if m < n or np.std(x) == 0:
        raise PipelineError("audio correlation window is too short or has no variation")
    nfft = 1 << (n + m - 2).bit_length()
    conv = np.fft.irfft(np.fft.rfft(y, nfft) * np.fft.rfft(x[::-1], nfft), nfft)
    dot = conv[n-1:n-1+(m-n+1)]
    sums0 = np.concatenate(([0.0], np.cumsum(y)))
    sums1 = np.concatenate(([0.0], np.cumsum(y*y)))
    sums = sums0[n:] - sums0[:-n]
    energy = sums1[n:] - sums1[:-n] - sums*sums/n
    denom = np.sqrt(float(np.dot(x,x))*np.maximum(energy,1.0))
    return dot/denom


def offset_at(align, master_s: float) -> float:
    import numpy as np
    frame = master_s * FPS
    k = int(np.searchsorted(align[:,0], frame))
    lo, hi = max(0,k-2), min(len(align),k+3)
    if lo == hi:
        raise PipelineError(f"alignment path does not cover master time {master_s:.3f}s")
    return float(np.median(align[lo:hi,1]))


def robust_line_fit(x, y) -> tuple[float, float, float, tuple[float, float]]:
    """Theil-Sen slope + median intercept; return slope, intercept, residual MAD,
    and the leave-one-window-out slope range. This is evidence only, not approval.
    """
    import numpy as np
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    slopes = [(y[j] - y[i]) / (x[j] - x[i])
              for i in range(len(x)) for j in range(i + 1, len(x))
              if x[j] != x[i]]
    if not slopes:
        raise PipelineError("not enough distinct windows for a robust rate fit")
    slope = float(np.median(slopes))
    intercept = float(np.median(y - slope * x))
    residual = y - (slope * x + intercept)
    center = float(np.median(residual))
    residual_mad = 1.4826 * float(np.median(np.abs(residual - center)))
    leave_one_out = []
    if len(x) > 4:
        for omit in range(len(x)):
            xx = np.delete(x, omit)
            yy = np.delete(y, omit)
            ss = [(yy[j] - yy[i]) / (xx[j] - xx[i])
                  for i in range(len(xx)) for j in range(i + 1, len(xx))
                  if xx[j] != xx[i]]
            if ss:
                leave_one_out.append(float(np.median(ss)))
    loo = (min(leave_one_out), max(leave_one_out)) if leave_one_out else (slope, slope)
    return slope, intercept, residual_mad, loo


def summarize_saved_report(path: Path, min_corr: float) -> dict:
    """Recompute OLS/robust summaries from a saved window TSV; performs no media I/O."""
    import numpy as np
    if not path.is_file():
        raise PipelineError(f"saved audio-rate report missing: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    required = ("ld_wav_time_s", "master_equivalent_time_s", "corr", "search_edge")
    if not rows or any(key not in rows[0] for key in required):
        raise PipelineError(f"{path} lacks required saved diagnostic columns: {required}")
    x = np.array([float(r["ld_wav_time_s"]) for r in rows], dtype=np.float64)
    y = np.array([float(r["master_equivalent_time_s"]) for r in rows], dtype=np.float64)
    corr = np.array([float(r["corr"]) for r in rows], dtype=np.float64)
    edge = np.array([str(r["search_edge"]).strip().lower() in ("true", "1", "yes") for r in rows])
    use = (corr >= min_corr) & (~edge)
    if int(use.sum()) < 4:
        raise PipelineError(f"only {int(use.sum())} saved windows meet corr>={min_corr} and non-edge; need >=4")
    ols_slope, ols_intercept = np.polyfit(x[use], y[use], 1)
    residual = y[use] - (ols_slope * x[use] + ols_intercept)
    dof = max(1, int(use.sum()) - 2)
    ols_se = float(np.sqrt(np.sum(residual ** 2) / dof / np.sum((x[use] - x[use].mean()) ** 2)))
    robust_slope, robust_intercept, robust_mad, loo = robust_line_fit(x[use], y[use])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema": "dvd-picture-audio-rate-summary-v2",
        "created_utc": datetime.now(UTC).isoformat(),
        "mode": "saved-report-only",
        "dvd_opened": False,
        "source_tsv": str(path),
        "source_sha256": digest,
        "window_count": len(rows),
        "accepted_window_count": int(use.sum()),
        "min_corr_threshold": min_corr,
        "ols": {"slope_master_s_per_wav_s": float(ols_slope), "intercept_s": float(ols_intercept),
                "rate_delta_ppm": float((ols_slope - 1.0) * 1e6), "slope_stderr_ppm": ols_se * 1e6,
                "residual_sd_s": float(residual.std(ddof=1)),
                "candidate_rate_hz": float(44100.0 / ols_slope) if ols_slope > 0 else None},
        "theil_sen": {"slope_master_s_per_wav_s": float(robust_slope), "intercept_s": float(robust_intercept),
                      "rate_delta_ppm": float((robust_slope - 1.0) * 1e6), "residual_mad_s": robust_mad,
                      "leave_one_window_out_rate_ppm_range": [(loo[0] - 1.0) * 1e6, (loo[1] - 1.0) * 1e6],
                      "candidate_rate_hz": float(44100.0 / robust_slope) if robust_slope > 0 else None},
        "candidate_is_approval": False,
        "windows": [{"ld_wav_time_s": float(x[i]), "master_equivalent_time_s": float(y[i]),
                     "corr": float(corr[i]), "search_edge": bool(edge[i]), "used": bool(use[i])}
                    for i in range(len(rows))],
    }


def sample_plan(skip_s: float, frames: int) -> tuple[int, int]:
    if skip_s < 0 or frames <= 0:
        raise PipelineError("AudioSkip must be nonnegative and video frame count positive")
    start_sample = math.floor(skip_s * SAMPLE_RATE + 1e-9)
    target_samples = round(frames * FPS_DEN * SAMPLE_RATE / FPS_NUM)
    return start_sample, target_samples
