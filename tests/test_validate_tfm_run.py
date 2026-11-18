from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_tfm_run import parse_session_log, verify_tfm_run  # noqa: E402


class TfmSessionVerificationTests(unittest.TestCase):
    def test_frames_are_read_from_session_log_not_decision_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decision = root / "full-tfm-log.txt"
            session = root / "tfm-session.log"
            # The decision log can contain frame-like text but is not the frame-count source.
            decision.write_text("TFM decisions for frame=999\n", encoding="utf-8")
            session.write_text("frame=83134 fps=140.0 time=00:46:13.9\nffmpeg exit=0 elapsed=397.3s\n",
                               encoding="utf-8")
            result = verify_tfm_run(decision, session, 83134)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["decoded_frames"], 83134)
        self.assertEqual(result["ffmpeg_exit_code"], 0)

    def test_false_positive_check_fails_when_decision_log_exists_but_session_has_no_frame_stats(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decision = root / "full-tfm-log.txt"
            session = root / "tfm-session.log"
            decision.write_text("TFM output is non-empty\n", encoding="utf-8")
            session.write_text("TFM-LOG STEP FAIL\n", encoding="utf-8")
            result = verify_tfm_run(decision, session, 83134)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("no FFmpeg frame=" in failure for failure in result["failures"]))
        self.assertTrue(any("no explicit FFmpeg exit-code" in failure for failure in result["failures"]))

    def test_wrong_frame_count_and_nonzero_exit_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decision = root / "full-tfm-log.txt"
            session = root / "tfm-session.log"
            decision.write_text("decisions\n", encoding="utf-8")
            session.write_text("frame=100\nffmpeg exit=1\n", encoding="utf-8")
            result = verify_tfm_run(decision, session, 200)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("exited with 1" in failure for failure in result["failures"]))
        self.assertTrue(any("expected 200" in failure for failure in result["failures"]))

    def test_parser_uses_last_progress_and_exit_records(self):
        self.assertEqual(parse_session_log(
            "frame=12\nframe=20\nffmpeg exit=0\n"), (20, 0))


if __name__ == "__main__":
    unittest.main()
