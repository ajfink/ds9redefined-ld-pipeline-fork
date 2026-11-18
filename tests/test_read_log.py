from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.read_log import decode_log  # noqa: E402


class ReadLogTests(unittest.TestCase):
    def test_decodes_utf16le_powershell_log(self):
        self.assertEqual(decode_log("frame=12 Ω\n".encode("utf-16")), "frame=12 Ω\n")

    def test_decodes_utf8_log(self):
        self.assertEqual(decode_log("frame=12 Ω\n".encode("utf-8-sig")), "frame=12 Ω\n")

    def test_decodes_utf16le_lines_appended_to_utf8_log(self):
        mixed = b"\xef\xbb\xbfutf8 line\n" + "frame=22\n".encode("utf-16-le")
        self.assertEqual(decode_log(mixed), "utf8 line\nframe=22\n")


if __name__ == "__main__":
    unittest.main()
