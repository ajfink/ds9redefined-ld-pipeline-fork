from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import record_video_trim_review as recorder  # noqa: E402
from ldcommon import PipelineError  # noqa: E402
from fileutil import sha256_file  # noqa: E402


class VideoTrimReviewRecorderTests(unittest.TestCase):
    def _fixture(self, root: Path):
        probe = root / "probe"
        probe.mkdir()
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            "dvd_reference_available": False, "no_dvd_proceed_approved": True,
            "audio": {"capture": "P1", "pad_frames": 3}, "video": {},
        }), encoding="utf-8")
        source, native = root / "master.mkv", root / "ld.wav"
        source.write_bytes(b"stack master")
        native.write_bytes(b"verified native audio")
        ld = SimpleNamespace(probe=lambda: probe, profile={})
        class Episode:
            code, series, identity = "S01E12", "DS9", "DS9:S01E12"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["captures"] = [{"label": "P1"}]
            data["audio"].update({
                "efm_provenance_required": True,
                "efm_provenance": {"P1": [{"report_path": "references/provenance.json"}]},
                "capture_selection": {"capture": "P1", "selection_note": "verified selection"},
            })
            def path(self, key):
                return {"stack_master": source, "ld_native": native}[key]
        Episode.ld = ld
        return probe, manifest, source, native, Episode(), ld

    def test_no_dvd_review_records_only_explicit_output_frame_values_with_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, manifest, source, native, ep, ld = self._fixture(Path(temporary))
            report = probe / "trim-review.json"
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)), \
                    patch.object(recorder, "validate_efm_capture_provenance"):
                result = recorder.record_trim_review(
                    ep, ld, head_trim=12, tail_trim=4,
                    review_note="Reviewed source opening and credits",
                    expected_manifest_sha256=sha256_file(manifest),
                    expected_source_sha256=sha256_file(source),
                    expected_audio_sha256=sha256_file(native),
                    review_assets_path=None, expected_review_assets_sha256="",
                    report_out=report)
            updated = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(updated["video"]["head_trim"], 12)
            self.assertEqual(updated["video"]["tail_trim"], 4)
            self.assertEqual(updated["video"]["trim_review"]["status"], "APPROVED")
            self.assertEqual(updated["video"]["trim_review"]["audio_pad_frames"], 3)
            self.assertTrue(Path(result["manifest_backup"]).is_file())
            self.assertTrue(report.is_file())

    def test_stale_source_or_unconfirmed_no_dvd_route_refuses_trim_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, manifest, source, native, ep, ld = self._fixture(Path(temporary))
            ep.data["no_dvd_proceed_approved"] = False
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)), \
                    patch.object(recorder, "validate_efm_capture_provenance"):
                with self.assertRaisesRegex(PipelineError, "explicit no-DVD"):
                    recorder.record_trim_review(
                        ep, ld, head_trim=12, tail_trim=4,
                        review_note="Reviewed source opening and credits",
                        expected_manifest_sha256=sha256_file(manifest),
                        expected_source_sha256=sha256_file(source),
                        expected_audio_sha256=sha256_file(native),
                        review_assets_path=None, expected_review_assets_sha256="",
                        report_out=probe / "trim-review.json")


if __name__ == "__main__":
    unittest.main()
