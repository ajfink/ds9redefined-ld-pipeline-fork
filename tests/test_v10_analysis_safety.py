from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from av_check_output import require_alignment_sample_coverage  # noqa: E402
from ldcommon import PipelineError  # noqa: E402
from measure_break_edges import (content_window_bounds, match_content_windows,
                                 report_exit_code)  # noqa: E402
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

    def test_close_pair_clipping_returns_explicit_empty_window_at_clearance_boundary(self):
        own = {"label": "B1", "ld_black": [100.0, 102.0]}
        for gap, has_window in ((4.1, True), (4.0, False), (3.9, False)):
            with self.subTest(gap=gap):
                neighbor = {"label": "B2", "ld_black": [102.0 + gap, 105.0 + gap]}
                bounds = content_window_bounds([own, neighbor], 0,
                                               window_s=48.0, fade_clear_s=2.0)
                self.assertEqual(bounds["after"] is not None, has_window)
                if bounds["after"] is not None:
                    self.assertLess(bounds["after"][0], bounds["after"][1])

    def test_empty_and_none_matches_are_unmeasured_without_subscript_crash(self):
        with patch("measure_break_edges.match_window", return_value=None) as match:
            empty = match_content_windows(None, None, None, (10.0, 20.0), 0, 0, 40, 10, 300)
        self.assertEqual(empty, {"status": "window-too-small", "before": None, "after": None})
        self.assertEqual(match.call_count, 1)

        with patch("measure_break_edges.match_window", return_value=None) as match:
            result = match_content_windows(None, None, (0.0, 10.0), (20.0, 30.0),
                                           0, 1, 40, 10, 300)
        self.assertEqual(result["status"], "window-too-small")
        self.assertIsNone(result["before"])
        self.assertIsNone(result["after"])
        self.assertEqual(match.call_count, 2)

    def test_tiny_valid_match_with_insufficient_texture_stays_unmeasured(self):
        low = {"offset": None, "score": None, "runner": None, "n": 1}
        with patch("measure_break_edges.match_window", side_effect=[low, low]):
            result = match_content_windows(None, None, (0.0, 0.05), (10.0, 10.05),
                                           0, 0, 40, 10, 300)
        self.assertEqual(result["status"], "insufficient-texture")
        self.assertIsNone(result["before"]["offset"])
        self.assertIsNone(result["after"]["offset"])

    def test_valid_side_is_preserved_when_only_the_other_window_is_clipped(self):
        measured = {"offset": -100, "score": 0.95, "runner": 0.90,
                    "margin": 0.05, "n": 1400}
        with patch("measure_break_edges.match_window", return_value=measured):
            result = match_content_windows(
                None, None, (0.0, 10.0), None, -100, 0, 40, 10, 300)
        self.assertEqual(result["status"], "partial-window")
        self.assertEqual(result["before"], measured)
        self.assertIsNone(result["after"])
        self.assertEqual(report_exit_code([result]), 1)

    def test_valid_side_is_preserved_when_the_other_match_is_rejected(self):
        measured = {"offset": -100, "score": 0.95, "runner": 0.90,
                    "margin": 0.05, "n": 1400}
        low = {"offset": None, "score": None, "runner": None, "n": 12}
        with patch("measure_break_edges.match_window", side_effect=[measured, low]):
            result = match_content_windows(
                None, None, (0.0, 10.0), (20.0, 30.0), -100, -90, 40, 10, 300)
        self.assertEqual(result["status"], "partial-measurement")
        self.assertEqual(result["before"], measured)
        self.assertEqual(result["after"], low)
        self.assertEqual(report_exit_code([result]), 1)

    def test_unresolved_edges_keep_the_report_exit_nonzero(self):
        self.assertEqual(report_exit_code([{"status": "measured"}]), 0)
        self.assertEqual(report_exit_code([{"status": "measured"},
                                           {"status": "window-too-small"}]), 1)
        self.assertEqual(report_exit_code([{"status": "insufficient-texture"}]), 1)
        self.assertEqual(report_exit_code([]), 1)

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
