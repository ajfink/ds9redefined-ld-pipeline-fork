from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.preflight_mac import count_y4m_frames, run_python_probe


class MacPreflightHelperTests(unittest.TestCase):
    def test_counts_binary_y4m_frames_without_scanning_pixel_text(self):
        header = b"YUV4MPEG2 W64 H48 F24000:1001 Ip A0:0 C420jpeg\n"
        payload_size = 64 * 48 + 2 * 32 * 24
        # Deliberately embed the ASCII marker in pixels; frame parsing must use
        # the geometry-derived payload length, not a newline regex.
        payload = (b"FRAME" * (payload_size // 5 + 1))[:payload_size]
        frame = b"FRAME\n" + payload
        self.assertEqual(count_y4m_frames(header + frame * 30), 30)

    def test_rejects_truncated_y4m_payload(self):
        header = b"YUV4MPEG2 W64 H48 F24000:1001 Ip A0:0 C420jpeg\n"
        with self.assertRaisesRegex(ValueError, "truncated Y4M frame payload"):
            count_y4m_frames(header + b"FRAME\nshort")

    def test_embedded_python_probe_is_valid_python(self):
        data, error = run_python_probe(Path(sys.executable), ["json"])
        self.assertEqual(error, "")
        self.assertIsNotNone(data)
        self.assertIn("python", data)
        self.assertEqual(data["missing_modules"], [])


if __name__ == "__main__":
    unittest.main()
