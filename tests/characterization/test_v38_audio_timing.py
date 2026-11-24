"""Characterization: v38 tests/test_audio_timing_v5.py, ported verbatim except the two duration tests
rewritten for ADR 0004 (CLV time-code PADs removed from the stack)."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from fstk.media.audio_timing import (
    NO_DVD_TOLERANCE_FRAMES,
    diagnostic_skip,
    no_dvd_audio_plan_gib,
    rate_filter,
    robust_line_fit,
    sample_plan,
    summarize_saved_report,
    summarize_timing,
)


class V5AudioTimingTests(unittest.TestCase):
    # ADR 0004 (deliberate change from v38): Orc's CLV time-code PAD frames are removed from the stack
    # master, so the duration model compares audio with the pad-free stack directly and the PAD
    # counts are gone. Same E06/E07 measurements as v38's tests, with all 84 PADs removed.
    def test_e07_duration_against_pad_free_stack(self):
        samples = 122_172_213
        report = summarize_timing(44_100, samples, 83_110 - 84)
        self.assertAlmostEqual(report["audio_minus_projected_content_frames"], 1.3216579, places=5)
        self.assertLessEqual(abs(report["audio_minus_projected_content_frames"]), NO_DVD_TOLERANCE_FRAMES)
        # the duration-derived rate is now within 0.1 Hz-scale of the disc's own 44.1 kHz
        self.assertAlmostEqual(report["duration_candidate_effective_rate_hz"], 44_100.0, delta=1.0)
        self.assertFalse(report["is_approval"])

    def test_e06_duration_against_pad_free_stack(self):
        samples = round(2776.250794 * 44_100)
        report = summarize_timing(44_100, samples, 83_288 - 84)
        self.assertAlmostEqual(report["audio_minus_projected_content_frames"], 0.32, delta=0.02)
        self.assertLessEqual(abs(report["audio_minus_projected_content_frames"]), NO_DVD_TOLERANCE_FRAMES)

    def test_no_soxr_build_uses_recorded_default_resampler_fallback(self):
        filt, name = rate_filter(88_112, use_soxr=False)
        self.assertIn("asetrate=88112", filt)
        self.assertNotIn("soxr", filt)
        self.assertEqual(name, "FFmpeg default resampler (swr)")

    def test_no_dvd_audio_space_plan_uses_measured_native_wav_size(self):
        # E07 native WAV is 488,688,896 bytes; the plan budgets two outputs and
        # one temporary by multiplying the actual source size with margin.
        self.assertAlmostEqual(no_dvd_audio_plan_gib(488_688_896), 3 * 488_688_896 / 1024**3)

    def test_sample_accurate_skip_and_video_sample_target(self):
        start, target = sample_plan(50.316933, 65_282)
        self.assertEqual(start, 2_218_976)
        self.assertEqual(target, round(65_282 * 1001 * 44_100 / 24_000))

    def test_robust_dvd_fit_resists_one_bad_window(self):
        x = np.arange(15, dtype=np.float64) * 180.0 + 100.0
        y = 0.4 + 1.000864 * x
        y[7] += 2.5
        slope, intercept, mad, loo = robust_line_fit(x, y)
        self.assertAlmostEqual((slope - 1.0) * 1e6, 864.0, delta=0.1)
        self.assertGreaterEqual(mad, 0)
        self.assertLess(loo[1] - loo[0], 10e-6)

    def test_raw_provisional_skip_is_only_a_window_center(self):
        self.assertAlmostEqual(diagnostic_skip(50.316933, 0), 50.316933)
        self.assertAlmostEqual(diagnostic_skip(49.2492, 1), 49.2158333333, places=8)
        self.assertEqual(diagnostic_skip(50.0, 2, 49.9), 49.9)

    def test_saved_dvd_report_can_be_summarized_without_media_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "windows.tsv"
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(("ld_wav_time_s", "master_equivalent_time_s", "corr", "search_edge"))
                for i in range(6):
                    x = 100.0 + i * 200
                    w.writerow((x, 0.25 + 1.0008 * x, 0.9, "False"))
            summary = summarize_saved_report(path, 0.5)
            self.assertFalse(summary["dvd_opened"])
            self.assertEqual(summary["accepted_window_count"], 6)
            self.assertAlmostEqual(summary["theil_sen"]["rate_delta_ppm"], 800.0, delta=0.1)
            self.assertEqual(len(summary["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
