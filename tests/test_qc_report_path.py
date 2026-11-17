from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
from qc_report import qc_report_destination  # noqa: E402


class QcReportPathTests(unittest.TestCase):
    def test_default_path_is_run_unique_and_nonexistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = Path(temporary)
            path = qc_report_destination(probe, "S01E12", None, "run-123")
            self.assertEqual(path.name, "S01E12-qc-report-run-123.md")
            self.assertFalse(path.exists())

    def test_explicit_existing_path_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = Path(temporary)
            existing = probe / "qc.md"
            existing.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "refusing to overwrite"):
                qc_report_destination(probe, "S01E12", str(existing), "run-123")
            self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
