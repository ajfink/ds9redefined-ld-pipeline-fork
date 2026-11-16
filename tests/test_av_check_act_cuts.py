from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from av_check_output import HOP, MIN_SUB_S, WIN, corr_peak, split_at_cuts  # noqa: E402
from ldcommon import PipelineError  # noqa: E402


class ActCutWindowTests(unittest.TestCase):
    def test_unstraddled_point_preserves_the_original_window(self):
        kept, dropped = split_at_cuts(100.0, [(300.0, 302.0)])
        self.assertEqual(kept, [(100.0, WIN, 100.0)])
        self.assertEqual(dropped, [])

    def test_single_cut_yields_separate_pre_and_post_windows(self):
        kept, dropped = split_at_cuts(900.0, [(903.9, 904.9)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][0], 904.9)
        self.assertAlmostEqual(kept[0][1], 55.1)
        self.assertEqual(kept[0][2], 904.9)
        self.assertEqual(dropped, [(900.0, 903.9)])
        self.assertGreaterEqual(kept[0][1], MIN_SUB_S)
        self.assertTrue(kept[0][0] >= 904.9)

    def test_two_sufficient_sides_are_both_returned_with_local_anchors(self):
        kept, dropped = split_at_cuts(1900.0, [(1932.2, 1933.4)])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])
        self.assertEqual(kept[0][0], 1900.0)
        self.assertAlmostEqual(kept[0][1], 32.2)
        self.assertEqual(kept[0][2], 1900.0)
        self.assertEqual(kept[1][0], 1933.4)
        self.assertAlmostEqual(kept[1][1], 26.6)
        self.assertEqual(kept[1][2], 1933.4)
        self.assertLessEqual(kept[0][0] + kept[0][1], 1932.2)
        self.assertGreaterEqual(kept[1][0], 1933.4)

    def test_synthetic_step_is_measured_as_two_independent_correlations(self):
        rng = np.random.default_rng(20261116)
        ld_envelope = rng.normal(size=int(WIN / HOP))
        windows, dropped = split_at_cuts(0.0, [(30.0, 32.0)])
        self.assertEqual(dropped, [])
        self.assertEqual(len(windows), 2)
        expected_shifts = [5, 25]
        peaks = []
        for (start, duration, _anchor), shift in zip(windows, expected_shifts):
            i0, count = int(round(start / HOP)), int(round(duration / HOP))
            segment = ld_envelope[i0:i0 + count]
            dvd_window = np.concatenate((np.zeros(shift), segment, np.zeros(16)))
            peaks.append(int(np.argmax(corr_peak(segment, dvd_window))))
        self.assertEqual(peaks, expected_shifts)

    def test_multiple_cuts_are_split_and_short_content_is_reported(self):
        kept, dropped = split_at_cuts(100.0, [(120.0, 121.0), (150.0, 155.0)])
        self.assertEqual(kept, [(100.0, 20.0, 100.0), (121.0, 29.0, 121.0)])
        self.assertEqual(dropped, [(155.0, 160.0)])
        self.assertTrue(all(length >= MIN_SUB_S for _, length, _ in kept))

    def test_invalid_intervals_and_windows_fail_closed(self):
        with self.assertRaisesRegex(PipelineError, "invalid recorded black interval"):
            split_at_cuts(100.0, [(120.0, 119.0)])
        with self.assertRaisesRegex(PipelineError, "positive duration"):
            split_at_cuts(100.0, [], window_s=0)


if __name__ == "__main__":
    unittest.main()
