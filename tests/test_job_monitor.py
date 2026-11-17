from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from job_monitor import STALL_WARNING_SECONDS, process_status_text  # noqa: E402


class JobMonitorTests(unittest.TestCase):
    def test_running_status_includes_pid_elapsed_and_output_idle(self):
        text, stalled = process_status_text("render", 123, 90, 30, True)
        self.assertFalse(stalled)
        self.assertIn("PID 123", text)
        self.assertIn("elapsed 1.5 min", text)
        self.assertIn("no console output for 0.5 min", text)

    def test_stall_notice_is_advisory_and_explicitly_preserves_process(self):
        text, stalled = process_status_text("render", 123, 3600,
                                            STALL_WARNING_SECONDS + 10, True)
        self.assertTrue(stalled)
        self.assertIn("no automatic kill or retry", text)


if __name__ == "__main__":
    unittest.main()
