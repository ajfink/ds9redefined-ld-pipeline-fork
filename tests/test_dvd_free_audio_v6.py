from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldcommon import PipelineError  # noqa: E402
from scripts import audio_timing_diagnostic as timing  # noqa: E402


class DvdFreePromotionTests(unittest.TestCase):
    def test_no_dvd_promotion_updates_manifest_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)
            episodes = bundle / "episodes"
            episodes.mkdir()
            manifest_path = episodes / "S01E99.json"
            original = {
                "episode": "S01E99",
                "dvd_reference_available": False,
                "no_dvd_proceed_approved": True,
                "deliverable_tracks": ["ld"],
                "mux_tracks": ["ld"],
                "audio": {"capture": "L1", "pad_frames": 0},
                "files": {},
            }
            raw = json.dumps(original, indent=2).encode("utf-8")
            manifest_path.write_bytes(raw)
            corrected = bundle / "S01E99-corrected.flac"
            synced = bundle / "S01E99-synced.flac"
            report_path = bundle / "timing.json"
            corrected.write_bytes(b"corrected")
            synced.write_bytes(b"synced")
            report_path.write_text("{}\n", encoding="utf-8")
            ep = SimpleNamespace(code="S01E99", data=original,
                                 ld=SimpleNamespace(profile={"shared": {"enabled": False}}))
            report = {
                "duration_candidate_effective_rate_hz": 44056.1,
                "experimental_candidate": {"output_media": {"duration": "100.0"}},
                "experimental_synced_candidate": {
                    "source_rate_intermediate_hz": 88112,
                    "effective_rate_hz": 44056.0,
                    "skip_s": 50.0,
                    "skip_method": "measured geometry",
                },
            }
            with patch.object(timing, "BUNDLE", bundle), \
                    patch("ldcommon.BUNDLE", bundle):
                backup = timing.promote_no_dvd_manifest(
                    ep, report, corrected, synced, report_path, 0.32,
                    timing.sha256_file(manifest_path))
            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), raw)
            self.assertEqual(updated["audio"]["rate_method"], "dvd-free-duration-pad")
            self.assertEqual(updated["audio"]["skip_s"], 50.0)
            self.assertEqual(updated["files"]["ld_synced"]["name"], synced.name)

    def test_promotion_refuses_large_endpoint_residual(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)
            (bundle / "episodes").mkdir()
            manifest = bundle / "episodes" / "S01E99.json"
            data = {"dvd_reference_available": False,
                    "no_dvd_proceed_approved": True,
                    "deliverable_tracks": ["ld"], "audio": {}, "files": {}}
            manifest.write_text(json.dumps(data), encoding="utf-8")
            ep = SimpleNamespace(code="S01E99", data=data,
                                 ld=SimpleNamespace(profile={"shared": {"enabled": False}}))
            corrected = bundle / "corrected.flac"
            synced = bundle / "synced.flac"
            corrected.write_bytes(b"x")
            synced.write_bytes(b"y")
            with patch.object(timing, "BUNDLE", bundle), \
                    patch("ldcommon.BUNDLE", bundle):
                with self.assertRaises(PipelineError):
                    timing.promote_no_dvd_manifest(
                        ep, {}, corrected, synced, bundle / "report.json", 1.01,
                        timing.sha256_file(manifest))


if __name__ == "__main__":
    unittest.main()
