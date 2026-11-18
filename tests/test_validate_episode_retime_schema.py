from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_episode import alignment_coverage, retime_schema_issues  # noqa: E402


class RetimeManifestSchemaTests(unittest.TestCase):
    def test_merged_music_through_strings_fail_manifest_shape_validation(self):
        issues = retime_schema_issues({
            "breaks": [],
            "merged_music_through": ["B5", "B9"],
        })
        self.assertTrue(any("must be an object" in issue for issue in issues))

    def test_reported_candidate_intervals_may_leave_adjudication_values_null(self):
        issues = retime_schema_issues({
            "breaks": [{
                "label": "B1",
                "ld_black": [10.0, 12.0],
                "dvd_silence": [9.0, 11.5],
                "off_before": None,
                "off_after": None,
                "step": None,
                "gap_s": None,
            }],
            "merged_music_through": [{
                "label": "X1", "ld_black": [20.0, 21.0], "step": None,
            }],
        })
        self.assertEqual(issues, [])
        gated = retime_schema_issues({
            "breaks": [{
                "label": "B1", "ld_black": [10.0, 12.0],
                "dvd_silence": [9.0, 11.5],
                "off_before": None, "off_after": None, "step": None, "gap_s": None,
            }],
            "merged_music_through": [],
        }, require_adjudicated=True)
        self.assertTrue(any("needs measured offsets and step" in issue for issue in gated))

    def test_legacy_adjudicated_break_may_leave_gap_s_null_but_step_is_required(self):
        issues = retime_schema_issues({
            "breaks": [{
                "label": "B1", "ld_black": [100.0, 104.0],
                "dvd_silence": [90.0, 92.002],
                "off_before": -1500, "off_after": -1538,
                "step": -38, "gap_s": None,
            }],
            "merged_music_through": [],
        }, require_adjudicated=True)
        self.assertEqual(issues, [])

    def test_adjudicated_gap_formula_uses_dvd_interval_and_offset_step(self):
        # DVD span 2.002s, step -38 frames -> gap = 2.002 + 38/FPS.
        expected_gap = 2.002 + 38 / (30000 / 1001)
        issues = retime_schema_issues({
            "breaks": [{
                "label": "B1", "ld_black": [100.0, 104.0],
                "dvd_silence": [90.0, 92.002],
                "off_before": -1500, "off_after": -1538,
                "step": -38, "gap_s": expected_gap + 0.001,
            }],
            "merged_music_through": [],
        }, require_adjudicated=True)
        self.assertEqual(issues, [])

    def test_gap_formula_mismatch_is_rejected(self):
        issues = retime_schema_issues({
            "breaks": [{
                "label": "B1", "ld_black": [100.0, 104.0],
                "dvd_silence": [90.0, 92.002],
                "off_before": -1500, "off_after": -1538,
                "step": -38, "gap_s": 1.0,
            }],
            "merged_music_through": [],
        }, require_adjudicated=True)
        self.assertTrue(any("gap_s disagrees" in issue for issue in issues))


class AlignmentCoverageTests(unittest.TestCase):
    def test_measured_delta_anchor_must_be_in_sampled_master_range(self):
        alignment = np.asarray([[1569.0, -1504.0], [1600.0, -1500.0], [2000.0, -1490.0]])
        covered, detail = alignment_coverage(alignment, 1565 / (30000 / 1001))
        self.assertFalse(covered)
        self.assertIn("--first-master-frame", detail)
        covered, detail = alignment_coverage(alignment, 1570 / (30000 / 1001))
        self.assertTrue(covered)
        self.assertIn("covers anchor", detail)

    def test_alignment_coverage_rejects_unordered_or_malformed_samples(self):
        covered, detail = alignment_coverage(np.asarray([[10.0, 0.0], [10.0, 1.0]]), 1.0)
        self.assertFalse(covered)
        self.assertIn("finite/increasing", detail)
        covered, detail = alignment_coverage(np.asarray([1.0, 2.0]), 1.0)
        self.assertFalse(covered)
        self.assertIn("Nx2", detail)


if __name__ == "__main__":
    unittest.main()
