from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from measure_audio_rate import unique_report_pair  # noqa: E402


class AudioRateReportPathTests(unittest.TestCase):
    def test_allocates_one_unique_suffix_for_both_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "audio-rate.tsv"
            summary = root / "audio-rate.summary.json"
            out.write_text("preserved", encoding="utf-8")
            with patch("measure_audio_rate.datetime") as clock:
                clock.now.return_value.strftime.return_value = "20261116T120000000000Z"
                candidate_out, candidate_summary = unique_report_pair(out, summary)
            self.assertEqual(candidate_out.name, "audio-rate-20261116T120000000000Z.tsv")
            self.assertEqual(candidate_summary.name, "audio-rate.summary-20261116T120000000000Z.json")
            self.assertTrue(out.is_file())
            self.assertFalse(candidate_out.exists())
            self.assertFalse(candidate_summary.exists())

    def test_skips_a_timestamp_collision_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "audio-rate.tsv"
            summary = root / "audio-rate.summary.json"
            (root / "audio-rate-20261116T120000000000Z.tsv").write_text("old", encoding="utf-8")
            with patch("measure_audio_rate.datetime") as clock:
                clock.now.return_value.strftime.return_value = "20261116T120000000000Z"
                candidate_out, candidate_summary = unique_report_pair(out, summary)
            self.assertEqual(candidate_out.name, "audio-rate-20261116T120000000000Z-1.tsv")
            self.assertEqual(candidate_summary.name, "audio-rate.summary-20261116T120000000000Z-1.json")
            self.assertEqual((root / "audio-rate-20261116T120000000000Z.tsv").read_text(), "old")


if __name__ == "__main__":
    unittest.main()
