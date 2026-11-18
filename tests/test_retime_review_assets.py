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

import build_retime_review_assets as assets  # noqa: E402


class RetimeAudioReviewAssetTests(unittest.TestCase):
    def test_sequential_dvd_silence_audio_clip_is_verified_and_report_bound(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg/ffprobe are not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            dvd, manifest = root / "dvd.mkv", root / "manifest.json"
            report = probe / "candidates.json"
            manifest.write_text(json.dumps({
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            }), encoding="utf-8")
            generated = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i",
                "anullsrc=channel_layout=5.1:sample_rate=48000",
                "-t", "12", "-c:a", "ac3", str(dvd),
            ], capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            digest = assets.sha256_file
            report.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "identity": "DS9:S01E12", "status": "REPORT_ONLY",
                "manifest_updated": False,
                "manifest_sha256": digest(manifest),
                "inputs": {"dvd_audio_selector": "0:a:0",
                           "dvd_sha256": digest(dvd)},
                "dvd_silence_intervals": [{"id": "D1", "start_s": 5.0, "end_s": 7.0}],
                "candidates": [{"label": "B1", "projected_dvd_interval_s": [5.0, 7.0],
                                "pair_candidates": [{"id": "D1"}]}],
            }), encoding="utf-8")

            class Episode:
                code, series, identity = "S01E12", "DS9", "DS9:S01E12"
                data = {}
                class LdRef:
                    def probe(self):
                        return probe
                ld = LdRef()
                def path(self, key):
                    return {"dvd": dvd}[key]

            class Tools:
                @staticmethod
                def ffmpeg():
                    return ffmpeg
                @staticmethod
                def ffprobe():
                    return ffprobe
                @staticmethod
                def probe():
                    return probe

            output = probe / "audio-review"
            with patch.object(assets, "assert_writable_path",
                              side_effect=lambda path, _ld=None: Path(path)), \
                    patch.object(assets, "episode_manifest_path", return_value=manifest):
                index_path = assets.build_audio_samples(
                    Episode(), report, output, max_clips=16, padding_s=2.0,
                    max_clip_s=20.0, ld=Tools())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual({row["id"] for row in index["samples"]}, {"D1", "LD-B1"})
            self.assertTrue(all(row["status"] == "READY" for row in index["samples"]))
            self.assertTrue(all(Path(row["path"]).is_file() for row in index["samples"]))
            self.assertTrue(all(row["sample_rate"] == 48000 for row in index["samples"]))


if __name__ == "__main__":
    unittest.main()
