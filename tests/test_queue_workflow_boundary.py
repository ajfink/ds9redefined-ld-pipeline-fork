from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_runbooks import OUTPUTS, generated_text  # noqa: E402


class QueueWorkflowBoundaryTests(unittest.TestCase):
    def test_all_queue_documents_start_with_a_live_workflow_warning(self):
        for name in ("V9-QUEUE.md", "V10-QUEUE.md", "V11-QUEUE.md", "V12-QUEUE.md"):
            with self.subTest(name=name):
                lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
                self.assertTrue(lines[0].startswith("> **NOT FOR LIVE WORKFLOW USE.**"))
                warning = " ".join(line.replace(">", " ").strip() for line in lines[:5]).casefold()
                self.assertTrue("do not cite or apply" in warning or
                                "do not read, cite, or apply" in warning)

    def test_readme_and_canonical_runbook_exclude_queues_from_live_sources(self):
        for path in (ROOT / "README.md", ROOT / "DS9 LaserDisc OpenCode Runbook.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("V*-QUEUE.md", text)
                self.assertTrue("Live-run authority" in text or "Live-workflow source boundary" in text)
                self.assertIn("Do not read, cite, or", text)
                self.assertIn("active", text.casefold())

    def test_generated_model_runbooks_keep_the_queue_boundary(self):
        for name in OUTPUTS:
            with self.subTest(name=name):
                output = generated_text(name)
                self.assertIn("V*-QUEUE.md", output)
                self.assertIn("Live-run authority", output)


if __name__ == "__main__":
    unittest.main()
