from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from process_utils import process_group_options, safe_console_write, terminate_process_tree  # noqa: E402
from run_stage import run_logged  # noqa: E402


class ProcessUtilityTests(unittest.TestCase):
    def test_safe_console_write_replaces_unencodable_glyphs(self):
        target = io.BytesIO()
        stream = io.TextIOWrapper(target, encoding="cp1252", errors="strict")
        safe_console_write("Orc sigma: σ\n", stream)
        stream.flush()
        self.assertIn(b"Orc sigma: ?", target.getvalue())

    def test_logged_child_emits_heartbeats_and_exit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "stage.log"
            captured = io.StringIO()
            code = "import time; print('child-start', flush=True); time.sleep(.05); print('child-end', flush=True)"
            with patch("sys.stdout", captured):
                rc = run_logged([sys.executable, "-c", code], os.environ.copy(), log,
                                heartbeat_seconds=0.01)
            text = log.read_text(encoding="utf-8")
            self.assertEqual(rc, 0)
            self.assertIn("child-start", text)
            self.assertIn("child-end", text)
            self.assertIn("EXIT CODE: 0", text)

    def test_child_unicode_does_not_abort_a_legacy_console(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "stage.log"
            target = io.BytesIO()
            legacy_console = io.TextIOWrapper(target, encoding="cp1252", errors="strict")
            code = "print('StackerStage sigma: σ', flush=True)"
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            with patch("sys.stdout", legacy_console):
                rc = run_logged([sys.executable, "-c", code], child_env, log,
                                heartbeat_seconds=0.01)
                legacy_console.flush()
            self.assertEqual(rc, 0)
            self.assertIn("StackerStage sigma: σ", log.read_text(encoding="utf-8"))
            self.assertIn(b"StackerStage sigma: ?", target.getvalue())

    def test_stale_progress_log_stops_only_the_stage_tree_and_reports_124(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage_log = root / "stage.log"
            progress_log = root / "orc.log"
            progress_log.write_text("started\n", encoding="utf-8")
            code = "import time; time.sleep(10)"
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = run_logged([sys.executable, "-c", code], os.environ.copy(), stage_log,
                                progress_path=progress_log, stall_seconds=0.08,
                                heartbeat_seconds=0.01)
            self.assertEqual(rc, 124)
            text = stage_log.read_text(encoding="utf-8")
            self.assertIn("watchdog", text.casefold())
            self.assertIn("EXIT CODE: 124", text)

    def test_process_group_options_are_platform_scoped(self):
        options = process_group_options()
        if sys.platform == "win32":
            self.assertIn("creationflags", options)
            self.assertNotIn("start_new_session", options)
        else:
            self.assertEqual(options, {"start_new_session": True})

    def test_terminate_process_tree_is_idempotent_for_finished_child(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], **process_group_options())
        child.wait(timeout=5)
        self.assertTrue(terminate_process_tree(child))


if __name__ == "__main__":
    unittest.main()
