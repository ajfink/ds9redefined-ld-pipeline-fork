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
        self.assertIn("record-pad-classifications", STAGES)
        self.assertIn("verify-efm-capture", STAGES)
        self.assertIn("record-efm-capture", STAGES)
        self.assertTrue(STAGES["render-stack"].get("review_gate"))
        self.assertTrue(STAGES["prepare-retime-breaks"].get("review_gate"))
        self.assertFalse(STAGES["record-pad-classifications"].get("review_gate"))
        self.assertTrue(STAGES["record-efm-capture"].get("review_gate"))

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

    def test_pad_classification_recorder_is_allowlisted_as_a_python_stage(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "record-pad-classifications", "S01E12", profile,
            ["--decision-file", r"D:\device\plans\review.json", "--apply"], None)
        self.assertTrue(command[1].endswith("record_pad_classifications.py"))
        self.assertEqual(command[command.index("--episode") + 1], "S01E12")
        self.assertIn("--decision-file", command)

    def test_efm_provenance_verifier_is_allowlisted(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "verify-efm-capture", "S01E13", profile,
            ["--capture", "P3", "--orc-log", r"D:\logs\efm.log",
             "--wav", r"D:\audio\P3.wav", "--efm-report", r"D:\audio\P3.wav.txt",
             "--report-out", r"D:\references\P3-provenance.json",
             "--confirm-manifest-sha256", "a" * 64], None)
        self.assertTrue(command[1].endswith("verify_efm_capture.py"))
        self.assertIn("--capture", command)
        self.assertIn("--orc-log", command)


if __name__ == "__main__":
    unittest.main()
