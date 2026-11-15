from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from retime_dvd_assets import (coalesce_silence_intervals,
                               parse_silence_intervals,
                               verify_expected_gaps)  # noqa: E402
from qc_report import retime_gap_verification_status  # noqa: E402


class RetimeAudioVerificationTests(unittest.TestCase):
    def test_touching_silence_events_are_coalesced_but_nearby_pauses_are_not(self):
        self.assertEqual(coalesce_silence_intervals([(1.0, 2.0), (2.0, 3.0),
                                                     (3.01, 4.0)]),
                         [(1.0, 3.0), (3.01, 4.0)])

    def test_ffmpeg_silencedetect_split_at_touch_is_reassembled(self):
        log = """
[silencedetect] silence_start: 303.445
[silencedetect] silence_end: 306.021 | silence_duration: 2.576
[silencedetect] silence_start: 306.021
[silencedetect] silence_end: 306.159 | silence_duration: 0.138
"""
        self.assertEqual(parse_silence_intervals(log), [(303.445, 306.159)])

    def test_reviewed_b1_shaped_gap_passes_after_full_contiguous_cut(self):
        result = verify_expected_gaps(
            [{"label": "B1", "start_s": 303.469, "end_s": 305.626, "duration_s": 2.157}],
            [(303.445, 305.624)],
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["gaps"][0]["status"], "PASS")

    def test_silence_tail_left_in_next_segment_fails_duration_tolerance(self):
        result = verify_expected_gaps(
            [{"label": "B1", "start_s": 303.469, "end_s": 305.626, "duration_s": 2.157}],
            [(303.445, 305.779)],
        )
        self.assertEqual(result["status"], "FAIL")

    def test_subresolution_gap_is_limited_not_passed(self):
        result = verify_expected_gaps(
            [{"label": "B8", "start_s": 2669.2, "end_s": 2669.223, "duration_s": 0.023}],
            [(2669.2, 2669.224)],
        )
        self.assertEqual(result["status"], "LIMIT")
        self.assertEqual(result["gaps"][0]["status"], "LIMIT")

    def test_absent_output_silence_fails(self):
        result = verify_expected_gaps(
            [{"label": "B3", "start_s": 416.0, "end_s": 416.7, "duration_s": 0.7}],
            [],
        )
        self.assertEqual(result["status"], "FAIL")

    def test_one_detected_interval_cannot_satisfy_two_planned_gaps(self):
        result = verify_expected_gaps(
            [{"label": "B1", "start_s": 10.0, "end_s": 11.0, "duration_s": 1.0},
             {"label": "B2", "start_s": 10.0, "end_s": 11.0, "duration_s": 1.0}],
            [(10.0, 11.0)],
        )
        self.assertEqual([row["status"] for row in result["gaps"]], ["PASS", "FAIL"])

    def test_delivery_qc_keeps_limits_and_missing_reports_non_pass(self):
        status, _ = retime_gap_verification_status({
            "status": "LIMIT", "gaps": [{"status": "LIMIT"}]}, True)
        self.assertEqual(status, "WARN")
        status, _ = retime_gap_verification_status(None, True)
        self.assertEqual(status, "WARN")
        status, _ = retime_gap_verification_status({
            "status": "PASS", "gaps": [{"status": "PASS"}]}, True)
        self.assertEqual(status, "PASS")
        status, _ = retime_gap_verification_status({
            "status": "FAIL", "gaps": [{"status": "FAIL"}]}, True)
        self.assertEqual(status, "FAIL")


if __name__ == "__main__":
    unittest.main()
