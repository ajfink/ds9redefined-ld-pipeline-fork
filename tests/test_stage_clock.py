from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from stage_clock import ClockError, new_clock, transition  # noqa: E402


class StageClockTests(unittest.TestCase):
    def test_pause_wait_resume_and_stop_exclude_human_gate_time(self):
        clock = new_clock("S01E09", "reference-selection", "DEVICE-A",
                          r"D:\work\stage-instance", "shared-release",
                          now_ns=1_000_000_000, now_utc="start")
        transition(clock, "pause", now_ns=11_000_000_000, now_utc="question")
        transition(clock, "resume", now_ns=111_000_000_000, now_utc="answer")
        transition(clock, "stop", now_ns=116_000_000_000, now_utc="finish")

        self.assertEqual(clock["state"], "complete")
        self.assertEqual(clock["active_elapsed_seconds"], 15.0)
        self.assertEqual(clock["human_wait_seconds"], 100.0)
        self.assertEqual(clock["wall_elapsed_seconds"], 115.0)

    def test_transitions_reject_double_pause_and_resume_without_pause(self):
        clock = new_clock("S01E09", "stack", "DEVICE-A", "D:/work", "local-fallback",
                          now_ns=10, now_utc="start")
        with self.assertRaisesRegex(ClockError, "current state is 'running'"):
            transition(clock, "resume", now_ns=11, now_utc="bad-resume")
        transition(clock, "pause", now_ns=20, now_utc="pause")
        with self.assertRaisesRegex(ClockError, "current state is 'paused'"):
            transition(clock, "pause", now_ns=21, now_utc="double-pause")


if __name__ == "__main__":
    unittest.main()
