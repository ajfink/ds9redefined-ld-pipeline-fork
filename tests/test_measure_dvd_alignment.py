from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.measure_dvd_alignment import (PipelineError, measured_anchor_frames,
                                           validate_anchor_coverage)  # noqa: E402


class AlignmentAnchorCoverageTests(unittest.TestCase):
    def test_accepts_anchors_inside_sampled_range(self):
        validate_anchor_coverage(1490, 82950, [1515, 50000])

    def test_rejects_anchor_before_range_with_matched_origin_guidance(self):
        with self.assertRaisesRegex(PipelineError, "unmatched LD-only front matter"):
            validate_anchor_coverage(3000, 82950, [1515])

    def test_rejects_anchor_after_range(self):
        with self.assertRaisesRegex(PipelineError, "outside the sampled alignment range"):
            validate_anchor_coverage(1490, 82950, [83000])

    def test_manifest_delta_is_added_as_a_required_anchor(self):
        anchors = measured_anchor_frames({"delta_s": 50.5505}, [100])
        self.assertEqual(anchors, [100, 1515])


if __name__ == "__main__":
    unittest.main()
