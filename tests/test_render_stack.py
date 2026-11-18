from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render_stack import (expected_pair_count, move_existing_master_to_backup,
                          validate_render_evidence)  # noqa: E402


class StackRenderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.labels = ["L1", "M1", "P1", "P2"]
        self.groups = [{"value": self.labels, "Count": len(self.labels)}]
        self.output = "\n".join([
            "tbc_source: Loaded TBC Source L1",
            "tbc_source: Loaded TBC Source M1",
            "tbc_source: Loaded TBC Source P1",
            "tbc_source: Loaded TBC Source P2",
            "StackerStage: inputs grouped by pressing over 16 frame(s) across the stack: {1,2,3,4}",
            'StackerStage report: {"inputs":[{"input":1},{"input":2},{"input":3},{"input":4}],"pairs":6}',
            "Unrecoverable samples: 0",
            "Successfully wrote 83,230 frames",
        ])

    def test_validates_loaded_sources_stacker_pairs_recovery_and_frames(self):
        result = validate_render_evidence(self.output, self.labels, 83230, 83230, self.groups)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["pair_count_check"], "pass: 6 all-pairs")

    def test_rejects_missing_source_and_unrecoverable_samples(self):
        output = self.output.replace("tbc_source: Loaded TBC Source P2\n", "").replace(
            "Unrecoverable samples: 0", "Unrecoverable samples: 2")
        result = validate_render_evidence(output, self.labels, 83230, 83230, self.groups)
        self.assertFalse(result["ok"])
        self.assertTrue(any("Loaded line" in issue for issue in result["issues"]))
        self.assertTrue(any("unrecoverable" in issue for issue in result["issues"]))

    def test_rejects_logged_and_decoded_frame_count_mismatch(self):
        result = validate_render_evidence(self.output, self.labels, 83230, 83170, self.groups)
        self.assertFalse(result["ok"])
        self.assertTrue(any("FFprobe frame count" in issue for issue in result["issues"]))

    def test_pair_count_requires_explicit_pressing_group_membership(self):
        self.assertEqual(expected_pair_count(4), 6)
        result = validate_render_evidence(self.output, self.labels, 83230, 83230, [])
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["pair_count_check"], "pass: 6 all-pairs")
        self.assertTrue(result["review_flags"])

    def test_explicit_restart_moves_old_master_to_timestamped_backup(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            master = Path(temporary) / "stack.mov"
            master.write_bytes(b"prior reviewed master")

            backup = move_existing_master_to_backup(master, "restart-test")

            self.assertFalse(master.exists())
            self.assertEqual(backup.read_bytes(), b"prior reviewed master")


if __name__ == "__main__":
    unittest.main()
