from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render_stack import ORC_STALL_WARNING_SECONDS, progress_heartbeat  # noqa: E402


class RenderStackWatchdogTests(unittest.TestCase):
    def test_heartbeat_reports_process_and_sink_without_killing(self):
        text, stalled = progress_heartbeat(600, 590, 4200, True, 1234)
        self.assertFalse(stalled)
        self.assertIn("process_alive=True", text)
        self.assertIn("pid=4200", text)
        self.assertIn("master_bytes=1234", text)
        self.assertIn("no automatic kill/retry", text)

    def test_stall_is_advisory_after_configured_no_progress_window(self):
        text, stalled = progress_heartbeat(ORC_STALL_WARNING_SECONDS + 5, 0, 4200, True, 0)
        self.assertTrue(stalled)
        self.assertIn("no log/output progress=1205s", text)


if __name__ == "__main__":
    unittest.main()
