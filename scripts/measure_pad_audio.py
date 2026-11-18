#!/usr/bin/env python3
"""Report whether frame-map PAD_1 positions carry audio energy.

This is a report-only diagnostic. It never selects a sample rate, edits a manifest,
or writes audio. PAD positions are mapped to the selected EFM WAV using the recorded
capture-alignment offset. Compare each 100 ms PAD window with nearby matched controls;
interpret the result as evidence about this episode only.

    python scripts/measure_pad_audio.py --episode S01E08
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from ldcommon import Ld, PipelineError, assert_writable_path, load_episode, redact_shared_text  # noqa: E402
from fileutil import hash_file  # noqa: E402
from pad_audit import reference_map_ranges, source_id_for_capture  # noqa: E402

FPS = 30000.0 / 1001.0
BASE_RATE = 44100
DEFAULT_WINDOW_SECONDS = 0.100
DEFAULT_CONTROL_OFFSETS_SECONDS = (-0.5, 0.5)


def sha256_file(path: Path) -> str:
    return hash_file(path, chunk_size=4 * 1024 * 1024)


def window_rms_dbfs(samples, center_sample: int, window_samples: int) -> tuple[float | None, tuple[int, int]]:
    """Return channel-averaged PCM RMS dBFS for a centered, in-bounds window."""
    import numpy as np

    if window_samples <= 0:
        raise PipelineError("window_samples must be positive")
    start = int(center_sample) - window_samples // 2
    end = start + window_samples
    if start < 0 or end > len(samples):
        return None, (start, end)
    block = np.asarray(samples[start:end], dtype=np.float64)
    if block.size == 0:
        return None, (start, end)
    rms = float(np.sqrt(np.mean(np.square(block / 32768.0))))
    dbfs = -160.0 if rms == 0 else 20.0 * math.log10(rms)
    return dbfs, (start, end)


def summarize(values: list[float]) -> dict[str, float | int] | None:
    import numpy as np

    finite = np.asarray([v for v in values if math.isfinite(v)], dtype=np.float64)
    if not len(finite):
        return None
    return {
        "count": int(len(finite)),
        "median_dbfs": float(np.median(finite)),
        "mean_dbfs": float(np.mean(finite)),
        "p10_dbfs": float(np.percentile(finite, 10)),
        "p90_dbfs": float(np.percentile(finite, 90)),
        "below_minus_60_dbfs": int(np.count_nonzero(finite < -60.0)),
    }


def analyze_pcm(samples, sample_rate: int, pad_positions: list[int],
                capture_pad_frames: int, *, window_seconds: float = DEFAULT_WINDOW_SECONDS,
                control_offsets_seconds: tuple[float, ...] = DEFAULT_CONTROL_OFFSETS_SECONDS,
                minimum_controls: int = 10) -> dict[str, Any]:
    """Compare PAD windows with nearby controls without making a rate decision."""
    if sample_rate <= 0 or capture_pad_frames < 0 or window_seconds <= 0:
        raise PipelineError("sample rate, capture-pad offset, and window duration must be positive")
    if not pad_positions:
        raise PipelineError("frame map contains no PAD_1 positions")
    window_samples = max(1, round(window_seconds * sample_rate))
    pad_rows = []
    control_rows = []
    centers = {frame: round((frame - capture_pad_frames) * sample_rate / FPS)
               for frame in sorted({int(value) for value in pad_positions})}
    valid_pad_windows: list[tuple[int, int]] = []
    for center_sample in centers.values():
        start = center_sample - window_samples // 2
        end = start + window_samples
        if start >= 0 and end <= len(samples):
            valid_pad_windows.append((start, end))
    control_seen: set[tuple[int, int]] = set()
    control_offsets = tuple(float(v) for v in control_offsets_seconds)

    for frame, center_sample in centers.items():
        energy, bounds = window_rms_dbfs(samples, center_sample, window_samples)
        row = {"master_frame": frame, "center_sample": center_sample,
               "sample_start": bounds[0], "sample_end": bounds[1],
               "rms_dbfs": energy}
        if energy is None:
            row["status"] = "out-of-audio-bounds"
            pad_rows.append(row)
            continue
        row["status"] = "measured"
        pad_rows.append(row)
        for offset in control_offsets:
            control_center = round(center_sample + offset * sample_rate)
            control_energy, control_bounds = window_rms_dbfs(samples, control_center, window_samples)
            if control_energy is None or control_bounds in control_seen:
                continue
            if any(control_bounds[0] < p_end and control_bounds[1] > p_start
                   for p_start, p_end in valid_pad_windows):
                continue
            control_seen.add(control_bounds)
            control_rows.append({"near_master_frame": frame, "offset_seconds": offset,
                                 "center_sample": control_center,
                                 "sample_start": control_bounds[0],
                                 "sample_end": control_bounds[1],
                                 "rms_dbfs": control_energy})

    pad_values = [float(row["rms_dbfs"]) for row in pad_rows if row.get("status") == "measured"]
    control_values = [float(row["rms_dbfs"]) for row in control_rows]
    pad_summary, control_summary = summarize(pad_values), summarize(control_values)
    status = ("OBSERVED" if pad_summary and len(control_values) >= minimum_controls
              else "LIMIT")
    return {
        "status": status,
        "method": "100ms channel-averaged RMS dBFS; digital zero is floored at -160 dBFS; controls at recorded nearby offsets",
        "digital_zero_floor_dbfs": -160.0,
        "window_seconds": window_seconds,
        "control_offsets_seconds": list(control_offsets),
        "minimum_controls": minimum_controls,
        "sample_rate": sample_rate,
        "capture_pad_frames": capture_pad_frames,
        "pad_positions_master_frames": len(pad_positions),
        "pad_windows": pad_rows,
        "control_windows": control_rows,
        "pad_summary": pad_summary,
        "control_summary": control_summary,
        "median_pad_minus_control_db": (
            pad_summary["median_dbfs"] - control_summary["median_dbfs"]
            if pad_summary and control_summary else None),
        "rate_recommendation": None,
        "manifest_updated": False,
        "caveat": ("PAD audio occupancy is episode-specific evidence only; it does not select "
                   "asetrate or prove an audio-clock model."),
    }


class PcmWavWindowReader:
    """Random-access PCM reader that holds only the requested short windows in memory."""

    def __init__(self, path: Path):
        import numpy as np
        try:
            self.source = wave.open(str(path), "rb")
        except (wave.Error, OSError) as exc:
            raise PipelineError(f"cannot read native PCM WAV {path}: {exc}") from exc
        if (self.source.getcomptype() != "NONE" or self.source.getsampwidth() != 2 or
                self.source.getnchannels() not in (1, 2)):
            self.source.close()
            raise PipelineError("expected uncompressed 16-bit mono/stereo PCM WAV")
        self.frames = self.source.getnframes()
        self.sample_rate = self.source.getframerate()
        self.channels = self.source.getnchannels()
        self.dtype = np.dtype("<i2")

    def __len__(self) -> int:
        return self.frames

    def __getitem__(self, key):
        import numpy as np
        if not isinstance(key, slice) or key.step not in (None, 1):
            raise TypeError("PCM window reader supports contiguous slices only")
        start = 0 if key.start is None else int(key.start)
        end = self.frames if key.stop is None else int(key.stop)
        if start < 0 or end < start or end > self.frames:
            raise IndexError("PCM window is outside the WAV bounds")
        self.source.setpos(start)
        payload = self.source.readframes(end - start)
        return np.frombuffer(payload, dtype=self.dtype).reshape(-1, self.channels)

    def close(self) -> None:
        self.source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--window-ms", type=float, default=100.0)
    parser.add_argument("--control-offsets", type=float, nargs="+", default=list(DEFAULT_CONTROL_OFFSETS_SECONDS))
    parser.add_argument("--minimum-controls", type=int, default=10)
    parser.add_argument("--report-out")
    args = parser.parse_args()
    try:
        if (args.window_ms <= 0 or args.minimum_controls < 1 or not args.control_offsets or
                any(offset == 0 for offset in args.control_offsets)):
            raise PipelineError("window, control offsets, and minimum control count must be positive")
        ld = Ld()
        ep = load_episode(args.episode, ld)
        audio = ep.data.get("audio") or {}
        capture_pad = audio.get("pad_frames")
        if capture_pad is None:
            raise PipelineError("measured audio.pad_frames is required to map PAD_1 windows to the selected WAV")
        wav_path = ep.path("ld_native")
        project_path = ep.path("stack_project")
        if not wav_path.is_file() or not project_path.is_file():
            raise PipelineError("selected native WAV and stack project are required")
        _source_id = source_id_for_capture(project_path, ep.reference)
        pads = reference_map_ranges(project_path, source_id=_source_id)
        samples = PcmWavWindowReader(wav_path)
        sample_rate, channels = samples.sample_rate, samples.channels
        try:
            report = analyze_pcm(samples, sample_rate, pads, int(capture_pad),
                                 window_seconds=args.window_ms / 1000.0,
                                 control_offsets_seconds=tuple(args.control_offsets),
                                 minimum_controls=args.minimum_controls)
        finally:
            samples.close()
        report.update({
            "schema": "ld-pipeline-pad-audio-occupancy-v1",
            "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "episode": ep.code,
            "reference_capture": ep.reference,
            "selected_audio_capture": audio.get("capture"),
            "channels": channels,
            "source_audio": str(wav_path),
            "source_audio_sha256": sha256_file(wav_path),
            "stack_project": str(project_path),
            "stack_project_sha256": sha256_file(project_path),
            "reference_frame_map_source_id": _source_id,
        })
        report_path = Path(args.report_out) if args.report_out else ld.probe() / f"{ep.code}-pad-audio-occupancy.json"
        assert_writable_path(report_path, ld)
        if report_path.exists():
            raise PipelineError(f"refusing to overwrite PAD audio occupancy report: {report_path}")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if os.environ.get("LDP_INSTANCE_ROOT") and (ld.profile.get("shared") or {}).get("enabled") is True:
            encoded = redact_shared_text(encoded, ld)
        with report_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        print(f"PAD audio occupancy for {ep.code}: {report['status']}")
        print(f"  PAD windows={report['pad_summary']['count'] if report['pad_summary'] else 0}; "
              f"controls={report['control_summary']['count'] if report['control_summary'] else 0}; "
              f"median delta={report['median_pad_minus_control_db']}")
        print("  report only; no rate recommendation or manifest update")
        print(f"  report: {report_path}")
        return 0 if report["status"] == "OBSERVED" else 1
    except (PipelineError, OSError, ValueError) as exc:
        print(f"PAD AUDIO MEASUREMENT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
