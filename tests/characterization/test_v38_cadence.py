"""Characterization: v38 tests/test_cadence.py (3 tests), ported by tools/port_v38.py."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fstk.media.cadence import analyze_tfm_rows, load_motion_trace, parse_tfm_log

ROOT = Path(__file__).resolve().parents[1]


class CadenceAnalysisTests(unittest.TestCase):
    def test_parser_requires_complete_contiguous_zero_based_log_coverage(self):
        text = "\n".join(f"{i} {('abcde'[i % 5])} [{20 if i % 5 else 90}]"
                         for i in range(25))
        rows = parse_tfm_log(text, expected_frames=25)
        self.assertEqual(rows[0]["frame"], 0)
        self.assertIsNone(rows[0]["flag"])
        self.assertEqual(len(rows), 25)
        with self.assertRaisesRegex(ValueError, "expected exactly 25"):
            parse_tfm_log("\n".join(f"{i} c [0]" for i in range(24)), expected_frames=25)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            parse_tfm_log("0 c [0]\n2 c [0]")

    def test_analysis_emits_machine_readable_metrics_and_flagged_intervals(self):
        rows = []
        for frame in range(1100):
            code = "abcde"[frame % 5]
            mic = 90 if 100 <= frame <= 205 else 10
            if 300 <= frame <= 302:
                code = "u"
            rows.append({"frame": frame, "match_code": code, "flag": None, "mic": mic})
        motion = [0.0] * 1099
        motion[400:950] = [20.0] * 550
        summary = analyze_tfm_rows(rows, motion_values=motion)
        self.assertEqual(summary["frame_count"], 1100)
        self.assertGreater(summary["unmatched_count"], 0)
        self.assertGreater(summary["combed_count"], 0)
        self.assertGreater(summary["insert_count"], 0)
        self.assertEqual(len(summary["windows_30s"]), 2)
        self.assertTrue(any("unmatched-frames" in row["reasons"]
                            for row in summary["review_intervals"]))
        self.assertTrue(summary["visual_approval_required"])
        self.assertGreater(summary["windows_30s"][0]["motion_p90"], 0)

    def test_motion_trace_parser_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "motion.txt"
            path.write_text("0.0\n15.5\n", encoding="utf-8")
            self.assertEqual(load_motion_trace(str(path)), [0.0, 15.5])
            path.write_text("0.0\nnoise\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-numeric"):
                load_motion_trace(str(path))
