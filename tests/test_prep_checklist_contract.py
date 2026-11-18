from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PrepChecklistContractTests(unittest.TestCase):
    def test_v13_live_readme_and_runbook_require_work_ahead_checklist(self):
        for relative in ("README.md", "DS9 LaserDisc OpenCode Runbook.md"):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8").casefold()
                self.assertIn("work ahead while", text)
                self.assertIn("episode-prep-checklist.template.md", text)
                self.assertTrue("download manifest verifies" in text or
                                "download-manifest.csv` verifies" in text)

    def test_version_archive_does_not_ship_planning_queues(self):
        self.assertEqual(list((ROOT / "Versioning").glob("V*-QUEUE.md")), [])

    def test_prep_template_records_evidence_and_exact_unblock_gates(self):
        text = (ROOT / "templates" / "episode-prep-checklist.template.md").read_text(
            encoding="utf-8").casefold()
        self.assertIn("allowed while transfer", text)
        self.assertIn("blocked until the download manifest", text)
        self.assertIn("exact blocker or next action", text)
        self.assertIn("do not establish ld measurements", text)
        self.assertIn("not workflow authority", text)


if __name__ == "__main__":
    unittest.main()
