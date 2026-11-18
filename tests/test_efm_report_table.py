from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from efm_report_table import collect, parse_report, render, validate_report_identity  # noqa: E402
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

    def test_episode_mode_rejects_cross_episode_efm_report_with_same_capture_label(self):
        with tempfile.TemporaryDirectory() as temp:
            audio_root = Path(temp)
            episode_dir = audio_root / "S01E11"
            episode_dir.mkdir()
            report_path = episode_dir / "L1-efm.wav.txt"
            wrong_output = audio_root / "S01E10" / "L1-efm.wav"
            report_path.write_text(
                SYNTHETIC_REPORT.replace(
                    "Samples decoded : 244,593,888",
                    f"Output file : {wrong_output}\n"
                    f"Report file : {wrong_output}.txt\n"
                    "Samples decoded : 244,593,888"),
                encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "belongs outside its episode folder"):
                collect(episode_dir, episode="S01E11", expected_labels={"L1"})

    def test_episode_mode_requires_complete_unique_manifest_capture_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            episode_dir = Path(temp) / "S01E11"
            episode_dir.mkdir()
            for label in ("L1", "P1"):
                output = episode_dir / f"{label}-efm.wav"
                report_path = episode_dir / f"{label}-efm.wav.txt"
                report_path.write_text(
                    SYNTHETIC_REPORT.replace(
                        "Samples decoded : 244,593,888",
                        f"Output file : {output}\nReport file : {output}.txt\n"
                        "Samples decoded : 244,593,888"),
                    encoding="utf-8")
            rows = collect(episode_dir, episode="S01E11", expected_labels={"L1", "P1"})
            self.assertEqual([row["capture"] for row in rows], ["L1", "P1"])
            with self.assertRaisesRegex(PipelineError, "inventory mismatch"):
                collect(episode_dir, episode="S01E11", expected_labels={"L1", "P1", "M1"})

    def test_episode_mode_requires_output_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            episode_dir = Path(temp) / "S01E11"
            episode_dir.mkdir()
            report_path = episode_dir / "L1-efm.wav.txt"
            report_path.write_text(SYNTHETIC_REPORT, encoding="utf-8")
            row = parse_report(report_path)
            with self.assertRaisesRegex(PipelineError, "lacks its Output file provenance"):
                validate_report_identity([row], "S01E11", {"L1"}, episode_dir)

    def test_prefers_canonical_reports_when_archived_copies_are_also_present(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "L1-efm.wav.txt").write_text(SYNTHETIC_REPORT, encoding="utf-8")
            (directory / "S01E11-efm-report-P1.txt").write_text(SYNTHETIC_REPORT, encoding="utf-8")
            rows = collect(directory)
            self.assertEqual([row["capture"] for row in rows], ["L1"])


if __name__ == "__main__":
    unittest.main()
