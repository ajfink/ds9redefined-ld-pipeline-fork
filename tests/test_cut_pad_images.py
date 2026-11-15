from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldcommon import PipelineError  # noqa: E402
from scripts.rife_padframes import parse_shared_cut_image  # noqa: E402


class SharedCutPadImageTests(unittest.TestCase):
    def test_timestamped_episode_image_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            folder = bundle / "Cut Pad Images" / "S01E07"
            folder.mkdir(parents=True)
            png = folder / "DVD-00-12-42.762-f00018288.png"
            png.write_bytes(b"test stub")
            frame, path, timestamp, dvd_frame = parse_shared_cut_image(f"18222={png}", "S01E07", bundle)
            self.assertEqual(frame, 18222)
            self.assertEqual(path, png.resolve())
            self.assertEqual(timestamp, "00:12:42.762")
            self.assertEqual(dvd_frame, 18288)

    def test_wrong_episode_and_untimestamped_images_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            other = bundle / "Cut Pad Images" / "S01E04"
            other.mkdir(parents=True)
            png = other / "DVD-00-18-14.191-f00026234.png"
            png.write_bytes(b"test stub")
            with self.assertRaises(PipelineError):
                parse_shared_cut_image(f"26160={png}", "S01E07", bundle)
            bad = bundle / "Cut Pad Images" / "S01E07"
            bad.mkdir()
            untimestamped = bad / "pad.png"
            untimestamped.write_bytes(b"test stub")
            with self.assertRaises(PipelineError):
                parse_shared_cut_image(f"18222={untimestamped}", "S01E07", bundle)


if __name__ == "__main__":
    unittest.main()
