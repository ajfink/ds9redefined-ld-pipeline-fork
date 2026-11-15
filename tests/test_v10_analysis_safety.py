from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from av_check_output import require_alignment_sample_coverage  # noqa: E402
from ldcommon import PipelineError  # noqa: E402
from measure_break_edges import content_window_bounds  # noqa: E402
from measure_ld_black import best_overlap, luma_runs  # noqa: E402


class V10MeasurementSafetyTests(unittest.TestCase):
    def test_alignment_offset_refuses_head_or_tail_extrapolation(self):
        require_alignment_sample_coverage(100.0, 2500.0, 100.0)
        require_alignment_sample_coverage(100.0, 2500.0, 2500.0)
        for target in (99.999, 2500.001):
            with self.subTest(target=target), self.assertRaisesRegex(PipelineError, "outside the sampled alignment range"):
                require_alignment_sample_coverage(100.0, 2500.0, target)

    def test_break_windows_clip_away_adjacent_black_intervals(self):
        breaks = [
            {"label": "B2", "ld_black": [100.0, 110.0]},
            {"label": "B3", "ld_black": [130.0, 140.0]},
        ]
        before_b3 = content_window_bounds(breaks, 1, window_s=48.0, fade_clear_s=2.0)
        after_b2 = content_window_bounds(breaks, 0, window_s=48.0, fade_clear_s=2.0)
        self.assertEqual(before_b3["before"], (112.0, 128.0))
        self.assertEqual(after_b2["after"], (112.0, 128.0))
        self.assertTrue(before_b3["before_clipped"])
        self.assertTrue(after_b2["after_clipped"])

    def test_overlapping_manifest_blacks_are_rejected(self):
        breaks = [
            {"label": "B1", "ld_black": [10.0, 20.0]},
            {"label": "B2", "ld_black": [19.0, 30.0]},
        ]
        with self.assertRaisesRegex(PipelineError, "intervals overlap"):
            content_window_bounds(breaks, 0, window_s=10.0, fade_clear_s=1.0)

    def test_luma_runs_are_reported_at_multiple_thresholds_without_selecting_one(self):
        means = [40, 9, 8, 12, 14, 18, 4, 4, 4, 30]
        at_10 = luma_runs(means, 10, fps=1, min_duration_s=2)
        at_15 = luma_runs(means, 15, fps=1, min_duration_s=2)
        self.assertEqual([(r["start_frame"], r["end_frame_exclusive"]) for r in at_10], [(1, 3), (6, 9)])
        self.assertEqual([(r["start_frame"], r["end_frame_exclusive"]) for r in at_15], [(1, 5), (6, 9)])
        self.assertEqual(at_15[0]["duration_s"], 4.0)

    def test_luma_candidate_match_reports_overlap_without_selecting_a_threshold(self):
        runs = luma_runs([20, 8, 7, 6, 20], 10, fps=1, min_duration_s=1)
        match = best_overlap(runs, 1.0, 4.0)
        self.assertEqual((match["start_frame"], match["end_frame_exclusive"]), (1, 4))
        self.assertEqual(match["overlap_seconds"], 3.0)


if __name__ == "__main__":
    unittest.main()
