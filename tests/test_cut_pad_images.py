from __future__ import annotations

import sys
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_device_scoped_image_requires_matching_provenance_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            folder = bundle / "devices" / "LEGION-A1B2C3D4" / "episodes" / "S01E07" / "cut-pad-images"
            folder.mkdir(parents=True)
            png = folder / "DVD-00-12-42.762-f00018288.png"
            payload = b"PNG-test-payload"
            png.write_bytes(payload)
            sidecar = png.with_suffix(".provenance.json")
            provenance = {
                "episode": "S01E07",
                "output_pad_frame": 18222,
                "dvd_timestamp": "00:12:42.762",
                "source_dvd_sha256": "a" * 64,
                "png_sha256": hashlib.sha256(payload).hexdigest(),
                "reviewed_episode_scene_and_pad_mapping": True,
            }
            sidecar.write_text(json.dumps(provenance), encoding="utf-8")
            frame, _, timestamp, dvd_frame = parse_shared_cut_image(
                f"18222={png}", "S01E07", bundle)
            self.assertEqual((frame, timestamp, dvd_frame), (18222, "00:12:42.762", 18288))
            sidecar.unlink()
            with self.assertRaisesRegex(PipelineError, "provenance sidecar"):
                parse_shared_cut_image(f"18222={png}", "S01E07", bundle)

    def test_local_fallback_cut_pad_images_are_accepted_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "ld-pipeline"
            local = Path(tmp) / "work" / "logs" / "ld-pipeline-local" / "LEGION" / "episodes" / "S01E07"
            folder = local / "cut-pad-images"
            folder.mkdir(parents=True)
            png = folder / "DVD-00-12-42.762-f00018288.png"
            payload = b"fallback-cut-pad-png"
            png.write_bytes(payload)
            png.with_suffix(".provenance.json").write_text(json.dumps({
                "episode": "S01E07",
                "output_pad_frame": 18222,
                "dvd_timestamp": "00:12:42.762",
                "source_dvd_sha256": "a" * 64,
                "png_sha256": hashlib.sha256(payload).hexdigest(),
                "reviewed_episode_scene_and_pad_mapping": True,
            }), encoding="utf-8")
            with patch.dict(os.environ, {"LDP_CUT_PAD_IMAGES": str(folder)}):
                result = parse_shared_cut_image(f"18222={png}", "S01E07", bundle)
            self.assertEqual((result[0], result[2], result[3]),
                             (18222, "00:12:42.762", 18288))


if __name__ == "__main__":
    unittest.main()
