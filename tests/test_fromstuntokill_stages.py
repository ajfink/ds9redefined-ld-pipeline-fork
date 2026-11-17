from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_stage import STAGES, make_action  # noqa: E402


class FromStunToKillV15StageTests(unittest.TestCase):
    def test_gui_intake_and_stack_stages_are_allowlisted_with_review_boundaries(self):
        self.assertIn("init-local-episode", STAGES)
        self.assertIn("render-stack", STAGES)
        self.assertIn("prepare-retime-breaks", STAGES)
        self.assertIn("record-retime-breaks", STAGES)
        self.assertTrue(STAGES["render-stack"].get("review_gate"))
        self.assertTrue(STAGES["prepare-retime-breaks"].get("review_gate"))

    def test_local_initializer_and_stack_renderer_use_bounded_python_helpers(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        local = make_action("init-local-episode", "S01E01", profile,
                            ["--series", "VOY", "--local-source", r"D:\captures",
                             "--confirmed-title", "Caretaker"], None, "VOY")
        self.assertTrue(local[1].endswith("init_local_episode.py"))
        self.assertEqual(local[local.index("--episode") + 1], "S01E01")
        self.assertIn("--series", local)

        render = make_action("render-stack", "S01E01", profile,
                             ["--planned-gib", "25", "--report-out", r"D:\probe\render.json"], None, "VOY")
        self.assertTrue(render[1].endswith("render_stack.py"))
        self.assertEqual(render[render.index("--episode") + 1], "S01E01")
        self.assertEqual(render[-4:], ["--planned-gib", "25", "--report-out", r"D:\probe\render.json"])


if __name__ == "__main__":
    unittest.main()
