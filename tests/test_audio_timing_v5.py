import unittest
import sys
from pathlib import Path
import csv
import tempfile
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.audio_timing_diagnostic import (NO_DVD_PAD_TOLERANCE_FRAMES,
                                             no_dvd_audio_plan_gib, rate_filter,
                                             summarize_timing)
from scripts.build_ld_synced import sample_plan
from scripts.measure_audio_rate import diagnostic_skip, robust_line_fit, summarize_saved_report


class V5AudioTimingTests(unittest.TestCase):
    def test_duration_candidate_and_pad_counts_remain_separate(self):
        # E07's native L1 WAV and measured stack dimensions. The frame map has
        # 84 PAD_1 nodes, one before the measured output head trim.
        samples = 122_172_213
        report = summarize_timing(44_100, samples, 83_110, 84, 83)
        self.assertAlmostEqual(report["duration_candidate_effective_rate_hz"], 44_056.1, delta=0.1)
        self.assertEqual(report["map_pad_count_full"], 84)
        self.assertEqual(report["map_pad_count_projected_into_output"], 83)
        self.assertAlmostEqual(report["audio_minus_map_content_frames"], 1.3216579, places=5)
        self.assertAlmostEqual(report["audio_minus_projected_content_frames"], 0.3216579, places=5)
        self.assertFalse(report["is_approval"])

    def test_e06_no_dvd_contrast_keeps_intrinsic_rate_and_pad_stretch_distinct(self):
        # E06 measured ~0 intrinsic rate change but needed a separate PAD stretch.
        # The duration/PAD model is an effective timeline rate, not an intrinsic-disc fit.
        samples = round(2776.250794 * 44_100)
        report = summarize_timing(44_100, samples, 83_288, 84, 83)
        self.assertAlmostEqual(report["duration_candidate_effective_rate_hz"], 44_055.69, delta=0.02)
        self.assertAlmostEqual(report["audio_minus_projected_content_frames"], -0.68, delta=0.02)
        self.assertLessEqual(abs(report["audio_minus_projected_content_frames"]),
                             NO_DVD_PAD_TOLERANCE_FRAMES)

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
