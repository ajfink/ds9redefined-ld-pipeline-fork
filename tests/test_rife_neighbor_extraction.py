from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rife_padframes import extract_neighbors  # noqa: E402


class FakeLd:
    @staticmethod
    def ffmpeg() -> str:
        return "ffmpeg"


class RifeNeighborExtractionTests(unittest.TestCase):
    def test_avisynth_uses_exact_trim_not_timestamp_seek(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.avs"
            video.write_text("source", encoding="utf-8")
            captured = {}

            def fake_run(_ld, command, input_video, *, text, vs_args):
                captured.update(command=command, input_video=input_video, text=text, vs_args=vs_args)
                pattern = Path(command[-1])
                pattern.with_name("extract_100_01.png").write_bytes(b"previous")
                pattern.with_name("extract_100_02.png").write_bytes(b"next")
                return SimpleNamespace(returncode=0, stderr=b"")

            with patch("rife_padframes.run_source_ffmpeg", side_effect=fake_run):
                prev, nxt = extract_neighbors(FakeLd(), video, 100, root)

            self.assertIsNone(captured["vs_args"])
            self.assertIsNotNone(captured["input_video"])
            self.assertNotIn("-ss", captured["command"])
            self.assertIn("src.Trim(99, 101)", Path(captured["input_video"]).read_text(encoding="utf-8"))
            self.assertEqual(prev.read_bytes(), b"previous")
            self.assertEqual(nxt.read_bytes(), b"next")

    def test_vapoursynth_requests_exact_absolute_frame_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.vpy"
            video.write_text("source", encoding="utf-8")
            captured = {}

            def fake_run(_ld, command, _video, *, text, vs_args):
                captured.update(command=command, text=text, vs_args=vs_args)
                pattern = Path(command[-1])
                pattern.with_name("extract_100_01.png").write_bytes(b"previous")
                pattern.with_name("extract_100_02.png").write_bytes(b"next")
                return SimpleNamespace(returncode=0, stderr=b"")

            with patch("rife_padframes.run_source_ffmpeg", side_effect=fake_run):
                extract_neighbors(FakeLd(), video, 100, root)

            self.assertEqual(captured["vs_args"], ["-s", "99", "-e", "101"])
            self.assertNotIn("-ss", captured["command"])


if __name__ == "__main__":
    unittest.main()
