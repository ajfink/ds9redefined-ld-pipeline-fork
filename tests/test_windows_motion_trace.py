from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_motion_trace as motion  # noqa: E402
from fileutil import sha256_file  # noqa: E402


class WindowsMotionTraceTests(unittest.TestCase):
    def test_ffmpeg_fallback_emits_verified_n_minus_one_trace(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg/ffprobe are not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = root / "master.mkv", root / "manifest.json"
            generated = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=128x96:rate=30000/1001:duration=3",
                "-c:v", "ffv1", str(source),
            ], capture_output=True, text=True)
            if generated.returncode:
                self.skipTest(f"FFmpeg lossless synthetic source unavailable: {generated.stderr[-300:]}")
            probe_result = subprocess.run([
                ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames", "-of", "json", str(source),
            ], capture_output=True, text=True)
            frames = int(json.loads(probe_result.stdout)["streams"][0]["nb_read_frames"])
            manifest.write_text("{}", encoding="utf-8")

            class Episode:
                code, series, identity = "S01E12", "DS9", "DS9:S01E12"
                data = {"stack": {"frames": frames}}
                pass

            class Tools:
                profile = {"limits": {"min_free_gb": 0}}
                @staticmethod
                def ffmpeg():
                    return ffmpeg
                @staticmethod
                def ffprobe():
                    return ffprobe

            episode = Episode()
            ld = Tools()
            trace, log, report = root / "motion.txt", root / "decode.log", root / "report.json"
            with patch.object(motion, "episode_manifest_path", return_value=manifest), \
                    patch.object(motion, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)):
                result = motion.ffmpeg_motion_trace(ld, episode, source, trace, 3, log, report)
            self.assertEqual(result["status"], "VERIFIED")
            self.assertEqual(result["motion_values"], frames - 3 - 1)
            self.assertEqual(len(trace.read_text(encoding="utf-8").splitlines()), frames - 4)
            self.assertTrue(log.is_file())
            self.assertTrue(report.is_file())
            self.assertEqual(result["source_sha256"], sha256_file(source))


if __name__ == "__main__":
    unittest.main()
