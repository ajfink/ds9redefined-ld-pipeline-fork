from __future__ import annotations

import builtins
import os
import sys
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import qc_report  # noqa: E402
import verify_pad_identities  # noqa: E402
from ldcommon import PipelineError  # noqa: E402


class OpenCvQcDiagnosticsTests(unittest.TestCase):
    def test_identity_gate_reports_distribution_and_original_import_error(self):
        original_import = builtins.__import__

        def fail_cv2(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("dlopen failed: missing _iconv symbol")
            return original_import(name, *args, **kwargs)

        with patch.object(sys, "platform", "darwin"), \
                patch.dict(os.environ, {"DYLD_LIBRARY_PATH": "/vsenv/lib"}), \
                patch.object(builtins, "__import__", side_effect=fail_cv2), \
                patch.object(sys, "argv", ["verify_pad_identities.py", "--episode", "S01E11"]):
            with self.assertRaises(PipelineError) as caught:
                verify_pad_identities.main()

        message = str(caught.exception)
        self.assertIn("opencv-python-headless", message)
        self.assertIn("ImportError: dlopen failed: missing _iconv symbol", message)
        self.assertIn("DYLD_LIBRARY_PATH is set", message)

    def test_qc_identity_gate_strips_dyld_for_child_only(self):
        observed = {}

        def fake_sh(command, env=None):
            observed["command"] = command
            observed["env"] = env
            return CompletedProcess(command, 0, "PASS replacement identity\n", "")

        with patch.object(qc_report.sys, "platform", "darwin"), \
                patch.dict(os.environ, {"DYLD_LIBRARY_PATH": "/vsenv/lib"}), \
                patch.object(qc_report, "sh", side_effect=fake_sh):
            status, _ = qc_report.run_gate("verify_pad_identities.py", ["--episode", "S01E11"], False)
            self.assertEqual(os.environ["DYLD_LIBRARY_PATH"], "/vsenv/lib")

        self.assertEqual(status, "PASS")
        self.assertNotIn("DYLD_LIBRARY_PATH", observed["env"])

    def test_other_qc_gates_keep_their_existing_environment(self):
        observed = {}

        def fake_sh(command, env=None):
            observed["env"] = env
            return CompletedProcess(command, 0, "PASS\n", "")

        with patch.object(qc_report.sys, "platform", "darwin"), \
                patch.dict(os.environ, {"DYLD_LIBRARY_PATH": "/vsenv/lib"}), \
                patch.object(qc_report, "sh", side_effect=fake_sh):
            status, _ = qc_report.run_gate("scan_padfixed_black.py", ["--episode", "S01E11"], False)

        self.assertEqual(status, "PASS")
        self.assertIsNone(observed["env"])


if __name__ == "__main__":
    unittest.main()
