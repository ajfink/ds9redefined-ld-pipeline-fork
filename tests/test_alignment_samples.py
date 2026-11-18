from __future__ import annotations

import json
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_alignment_samples as samples  # noqa: E402


class AlignmentSampleTests(unittest.TestCase):
    def test_fade_fit_candidates_keep_a_b_as_separate_report_only_pairs(self):
        report = {
            "schema": "fade-midpoint-fit-v1",
            "fit_A_master_vs_wav": {
                "theil_sen_b": 1.001,
                "pairs": [
                    {"wav_s": 20.0, "y_s": 19.8, "resid_s": 0.8},
                    {"wav_s": 40.0, "y_s": 39.7, "resid_s": -0.4},
                    {"wav_s": 60.0, "y_s": 59.6, "resid_s": 0.2},
                ],
            },
            "fit_B_dvd_vs_wav": {
                "theil_sen_b": 0.999,
                "pairs": [{"wav_s": 20.0, "y_s": 21.0, "resid_s": 0.1}],
            },
        }
        candidates, deferred = samples.collect_candidates(
            [(Path("fade-fit.json"), report)], dvd_map=None, max_samples=8)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(row["id"].startswith("fade-fit-a-") for row in candidates))
        self.assertIn("no visually approved alignment map", deferred[0])
        self.assertEqual(candidates[0]["wav_time_s"], 40.0)
        self.assertEqual(candidates[0]["master_time_s"], 39.7)
        self.assertFalse(candidates[0]["candidate_is_approval"])

    def test_raw_picture_audio_summary_previews_only_report_flagged_windows(self):
        report = {
            "schema": "dvd-picture-audio-rate-summary-v2",
            "candidate_is_approval": False,
            "min_corr_threshold": 0.5,
            "theil_sen": {"slope_master_s_per_wav_s": 1.0002},
            "windows": [
                {"ld_wav_time_s": 20, "master_equivalent_time_s": 21,
                 "corr": 0.8, "search_edge": False},
                {"ld_wav_time_s": 40, "master_equivalent_time_s": 41,
                 "corr": 0.3, "search_edge": False},
                {"ld_wav_time_s": 60, "master_equivalent_time_s": 61,
                 "corr": 0.7, "search_edge": True},
            ],
        }
        candidates, _deferred = samples.collect_candidates(
            [(Path("rate-summary.json"), report)], dvd_map=None, max_samples=8)
        self.assertEqual([row["wav_time_s"] for row in candidates], [60.0, 40.0])
        self.assertTrue(candidates[0]["search_edge"])
        self.assertTrue(all(not row["candidate_is_approval"] for row in candidates))

    def test_dvd_fit_b_uses_reviewed_map_to_get_master_preview_slope(self):
        report = {
            "schema": "fade-midpoint-fit-v1",
            "fit_A_master_vs_wav": {"error": "not enough pairs"},
            "fit_B_dvd_vs_wav": {
                "theil_sen_b": 2.0,
                "pairs": [
                    {"wav_s": 20.0, "y_s": 40.0, "resid_s": 0},
                    {"wav_s": 40.0, "y_s": 80.0, "resid_s": 0},
                    {"wav_s": 60.0, "y_s": 120.0, "resid_s": 0},
                ],
            },
        }
        dvd = np.array([0.0, 200.0])
        master = np.array([0.0, 100.0])
        candidates, deferred = samples.collect_candidates(
            [(Path("fade-fit.json"), report)], dvd_map=(dvd, master), max_samples=8)
        self.assertFalse(deferred)
        self.assertEqual(len(candidates), 2)
        self.assertAlmostEqual(candidates[0]["candidate_slope"], 1.0)
        self.assertAlmostEqual(candidates[0]["master_time_s"], 40.0)
        self.assertAlmostEqual(candidates[0]["dvd_reference_time_s"], 80.0)

    def test_preview_probe_requires_both_streams_and_matching_timestamps(self):
        payload = {
            "format": {"duration": "8.000"},
            "streams": [
                {"codec_type": "video", "start_time": "0.000", "duration": "8.000"},
                {"codec_type": "audio", "start_time": "0.000", "duration": "7.990"},
            ],
        }
        with patch.object(samples.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr="")):
            result = samples._verify_preview("ffprobe", Path("preview.mp4"), 8.0)
        self.assertAlmostEqual(result["video_start_s"], 0.0)
        self.assertAlmostEqual(result["audio_duration_s"], 7.99)

    def test_generated_candidate_sample_is_verified_and_does_not_edit_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master, wav = root / "master.mp4", root / "native.wav"
            manifest, report = root / "manifest.json", root / "fade.json"
            master.write_bytes(b"video source")
            wav.write_bytes(b"native audio source")
            manifest.write_text('{"audio": {}}\n', encoding="utf-8")
            report.write_text(json.dumps({
                "schema": "fade-midpoint-fit-v1",
                "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
                "episode": "S01E12",
                "fit_A_master_vs_wav": {
                    "theil_sen_b": 1.001,
                    "pairs": [{"wav_s": 30.0, "y_s": 31.0, "resid_s": 0.01}],
                },
                "fit_B_dvd_vs_wav": {"error": "no fit"},
            }), encoding="utf-8")

            class Episode:
                code = "S01E12"
                data = {"files": {}}
                def __init__(self):
                    self.manifest = manifest
                def path(self, key):
                    return {"stack_master": master, "ld_native": wav}[key]

            class Tools:
                @staticmethod
                def ffprobe():
                    return "ffprobe"
                @staticmethod
                def ffmpeg():
                    return "ffmpeg"

            def fake_run(command, **_kwargs):
                if "-filter_complex" in command:
                    Path(command[-1]).write_bytes(b"preview")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                payload = {
                    "format": {"duration": "100.0" if str(command[-1]) in
                               (str(master), str(wav)) else "8.0"},
                    "streams": [
                        {"codec_type": "video", "start_time": "0", "duration": "8"},
                        {"codec_type": "audio", "start_time": "0", "duration": "8"},
                    ],
                }
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            output = root / "samples"
            before_manifest = manifest.read_bytes()
            with patch.object(samples, "assert_writable_path", side_effect=lambda path, _ld: Path(path)), \
                    patch.object(samples.subprocess, "run", side_effect=fake_run):
                index_path = samples.build_samples(
                    Episode(), [report], output, 8.0, 8, Tools())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["samples"][0]["status"], "READY")
            self.assertTrue(Path(index["samples"][0]["path"]).is_file())
            self.assertEqual(manifest.read_bytes(), before_manifest)

    def test_real_ffmpeg_synthetic_alignment_preview_smoke(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg/ffprobe are not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master, wav = root / "master.mp4", root / "native.wav"
            manifest, report = root / "manifest.json", root / "fade.json"
            generated_video = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=20",
                "-f", "lavfi", "-i", "sine=frequency=750:sample_rate=44100:duration=20",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(master),
            ], capture_output=True, text=True)
            if generated_video.returncode:
                self.skipTest(f"FFmpeg synthetic H.264 generation unavailable: {generated_video.stderr[-300:]}")
            generated_wav = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-v", "error", "-f", "lavfi",
                "-i", "sine=frequency=750:sample_rate=44100:duration=20",
                "-ac", "2", "-c:a", "pcm_s16le", str(wav),
            ], capture_output=True, text=True)
            self.assertEqual(generated_wav.returncode, 0, generated_wav.stderr)
            manifest.write_text('{"audio": {}}\n', encoding="utf-8")
            report.write_text(json.dumps({
                "schema": "fade-midpoint-fit-v1",
                "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
                "episode": "S01E12",
                "fit_A_master_vs_wav": {
                    "theil_sen_b": 1.001,
                    "pairs": [{"wav_s": 10.0, "y_s": 10.0, "resid_s": 0.0}],
                },
                "fit_B_dvd_vs_wav": {"error": "no fit"},
            }), encoding="utf-8")

            class Episode:
                code = "S01E12"
                data = {"files": {}}
                def __init__(self):
                    self.manifest = manifest
                def path(self, key):
                    return {"stack_master": master, "ld_native": wav}[key]

            class Tools:
                @staticmethod
                def ffprobe():
                    return ffprobe
                @staticmethod
                def ffmpeg():
                    return ffmpeg

            with patch.object(samples, "assert_writable_path",
                              side_effect=lambda path, _ld: Path(path)):
                index_path = samples.build_samples(
                    Episode(), [report], root / "samples", 8.0, 8, Tools())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["samples"][0]["status"], "READY", index["samples"])
            self.assertLessEqual(
                index["samples"][0]["verification"]["duration_tolerance_s"], 0.23)


if __name__ == "__main__":
    unittest.main()
