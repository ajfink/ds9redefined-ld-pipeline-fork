from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from efm_report_table import collect, parse_report, render  # noqa: E402
from ldcommon import PipelineError  # noqa: E402


SYNTHETIC_REPORT = """\
Overall assessment : POOR
Disc duration : 46:08:21 (207,542 sections)
Tracks recovered : 2
Q-channel timecodes : 207,542 (100.00 %)
Samples decoded : 244,593,888
Audio integrity (programme area)
Concealed (interp) : 1,234 (0.10 %)
Silenced (muted) : 2,345 (0.20 %)
Data loss (post-C2) : 0.0073 %
C1 uncorrectable : 45 (0.01 %)
C2 uncorrectable : 12 (0.00 %)
Missing (interpolated) : 7
Sections out-of-order / uncorrectable : 0 / 0
Timeline resyncs : 0
Padding : 0
Q start 00:00:00
Q end 00:46:08
"""


class EfmReportTableTests(unittest.TestCase):
    def test_parses_report_fields_and_renders_selection_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "L1-efm.wav.txt"
            report.write_text(SYNTHETIC_REPORT, encoding="utf-8")
            row = parse_report(report)
        self.assertEqual(row["capture"], "L1")
        self.assertEqual(row["disc_duration"], "46:08:21")
        self.assertEqual(row["sections"], 207542)
        self.assertEqual(row["post_c2_pct"], 0.0073)
        self.assertEqual(row["concealed"], 1234)
        self.assertEqual(row["muted"], 2345)
        self.assertEqual(row["c1_unc"], 45)
        self.assertEqual(row["c2_unc"], 12)
        self.assertEqual(row["missing"], 7)
        self.assertEqual(row["out_of_order"], 0)
        self.assertEqual(row["sections_uncorrectable"], 0)
        table = render([row])
        self.assertIn("post-C2%", table)
        self.assertIn("0.0073", table)
        self.assertIn("1234", table)
        self.assertIn("207,542 (100%)", table)

    def test_collect_is_format_driven_and_requires_matching_report_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "L1-efm.wav.txt").write_text(SYNTHETIC_REPORT, encoding="utf-8")
            (directory / "P1-efm.wav.txt").write_text(SYNTHETIC_REPORT, encoding="utf-8")
            rows = collect(directory)
            self.assertEqual([row["capture"] for row in rows], ["L1", "P1"])
            (directory / "L1-efm.wav.txt").unlink()
            (directory / "P1-efm.wav.txt").unlink()
            with self.assertRaisesRegex(PipelineError, "no EFMSink reports"):
                collect(directory)

    def test_collects_archived_device_log_names_and_uses_report_capture_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for label in ("L1", "P1"):
                report = directory / f"S01E11-efm-report-{label}.txt"
                report.write_text(SYNTHETIC_REPORT, encoding="utf-8")
            rows = collect(directory)
            self.assertEqual([row["capture"] for row in rows], ["L1", "P1"])
            self.assertEqual(rows[0]["q_timecodes"], "207,542 (100.00 %)")
            self.assertIn("Q timecodes", render(rows))

    def test_q_continuity_summary_is_compact_in_comparison_table(self):
        row = {"capture": "P1", "q_timecodes": "Valid and contiguous"}
        self.assertIn("valid+contiguous", render([row]))

    def test_capture_identity_can_be_read_from_efmsink_output_path(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "archived-report.txt"
            text = SYNTHETIC_REPORT.replace(
                "Samples decoded : 244,593,888",
                "Output file : /Users/example/work/audio/S01E11-P1-efm.wav\n"
                "Samples decoded : 244,593,888")
            report.write_text(text, encoding="utf-8")
            self.assertEqual(parse_report(report)["capture"], "P1")

    def test_prefers_canonical_reports_when_archived_copies_are_also_present(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "L1-efm.wav.txt").write_text(SYNTHETIC_REPORT, encoding="utf-8")
            (directory / "S01E11-efm-report-P1.txt").write_text(SYNTHETIC_REPORT, encoding="utf-8")
            rows = collect(directory)
            self.assertEqual([row["capture"] for row in rows], ["L1"])


if __name__ == "__main__":
    unittest.main()
