from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from retime_break_review import RetimeBreakReviewError, RetimeBreakReviewWindow  # noqa: E402


class RetimeBreakReviewTests(unittest.TestCase):
    def test_report_is_identity_checked_and_no_candidates_are_preselected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate-report.json"
            payload = json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "VOY:S01E01",
                "requires_human_review": True,
                "manifest_updated": False,
                "candidates": [
                    {"label": "B1", "pair_status": "unique-pair",
                     "ld_black": [10.0, 11.0],
                     "pair_candidates": [{"id": "D1", "start_s": 10.0, "end_s": 11.0}]},
                    {"label": "B2", "pair_status": "no-dvd-silence",
                     "ld_black": [20.0, 21.0], "pair_candidates": []},
                ],
            }).encode("utf-8")
            path.write_bytes(payload)
            view = RetimeBreakReviewWindow(None, path, "VOY:S01E01", lambda *_args: True)
            self.assertEqual(view.report_sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(set(view.rows), {"B1", "B2"})
            self.assertEqual(view.decisions, {})
            with self.assertRaisesRegex(RetimeBreakReviewError, "report-only V15 report"):
                RetimeBreakReviewWindow(None, path, "DS9:S01E01", lambda *_args: True)

    def test_malformed_report_labels_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate-labels.json"
            path.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "requires_human_review": True,
                "manifest_updated": False,
                "candidates": [{"label": "B1"}, {"label": "B1"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RetimeBreakReviewError, "labels.*unique"):
                RetimeBreakReviewWindow(None, path, "DS9:S01E12", lambda *_args: True)

    def test_report_without_human_review_requirement_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unreviewable-report.json"
            path.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "manifest_updated": False, "candidates": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RetimeBreakReviewError, "report-only V15 report"):
                RetimeBreakReviewWindow(None, path, "DS9:S01E12", lambda *_args: True)


if __name__ == "__main__":
    unittest.main()
