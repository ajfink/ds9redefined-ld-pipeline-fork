from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import regen_plain_text  # noqa: E402


class RegenPlainTextTests(unittest.TestCase):
    def test_check_matches_generated_crlf_bytes_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runbook.md"
            output = root / "plain.txt"
            source.write_text("# Title\n\nA **bold** line.\n", encoding="utf-8")
            with patch.object(regen_plain_text, "BUNDLE", root), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(regen_plain_text.main([
                    "--source", str(source), "--output", str(output)]), 0)
            expected = output.read_bytes()
            self.assertIn(b"\r\n", expected)
            with patch.object(regen_plain_text, "BUNDLE", root), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(regen_plain_text.main([
                    "--source", str(source), "--output", str(output), "--check"]), 0)
            self.assertEqual(output.read_bytes(), expected)

    def test_check_reports_stale_or_missing_output_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runbook.md"
            output = root / "plain.txt"
            source.write_text("# Current\n", encoding="utf-8")
            output.write_bytes(b"stale user data\n")
            stderr = io.StringIO()
            with patch.object(regen_plain_text, "BUNDLE", root), \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(regen_plain_text.main([
                    "--source", str(source), "--output", str(output), "--check"]), 1)
            self.assertEqual(output.read_bytes(), b"stale user data\n")
            self.assertIn("stale or missing", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
