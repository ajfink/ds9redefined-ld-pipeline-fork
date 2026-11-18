from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from streamutil import configure_console, start_line_reader  # noqa: E402


class StreamUtilityTests(unittest.TestCase):
    def test_line_reader_preserves_lines_eof_and_thread_name(self):
        lines, reader = start_line_reader(io.StringIO("first\nsecond\n"),
                                          name="stable-diagnostic-reader")
        self.assertEqual(reader.name, "stable-diagnostic-reader")
        self.assertEqual(lines.get(timeout=2), "first\n")
        self.assertEqual(lines.get(timeout=2), "second\n")
        self.assertIsNone(lines.get(timeout=2))
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())

    def test_console_configuration_replaces_unencodable_diagnostics(self):
        class Stream:
            def __init__(self):
                self.options = {}

            def reconfigure(self, **options):
                self.options.update(options)

        out, err = Stream(), Stream()
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            configure_console()
        self.assertEqual(out.options, {"errors": "replace"})
        self.assertEqual(err.options, {"errors": "replace"})


if __name__ == "__main__":
    unittest.main()
