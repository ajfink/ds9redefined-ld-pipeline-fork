from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_runbooks import OUTPUTS, generated_text  # noqa: E402


class VersioningArchiveBoundaryTests(unittest.TestCase):
    def test_retired_queue_and_changelog_files_are_not_shipped(self):
        self.assertEqual(list((ROOT / "Versioning").glob("V*-QUEUE.md")), [])
        self.assertFalse((ROOT / "Versioning" / "changelog.txt").exists())

    def test_readme_and_canonical_runbook_exclude_version_history_from_live_sources(self):
        for path in (ROOT / "README.md", ROOT / "DS9 LaserDisc OpenCode Runbook.md"):
            with self.subTest(path=path.name):
                text = " ".join(path.read_text(encoding="utf-8").casefold().split())
                self.assertIn("versioning/", text)
                self.assertIn("retrospective", text)
                self.assertIn("not live episode instructions", text)
                self.assertNotIn("v*-queue.md", text)
                self.assertNotIn("changelog.txt", text)

    def test_generated_model_runbooks_keep_versioning_out_of_live_evidence(self):
        for name in OUTPUTS:
            with self.subTest(name=name):
                output = " ".join(generated_text(name).casefold().split())
                self.assertIn("versioning/", output)
                self.assertIn("retrospective", output)
                self.assertNotIn("v*-queue.md", output)
                self.assertNotIn("changelog.txt", output)


if __name__ == "__main__":
    unittest.main()
