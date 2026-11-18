from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from retime_break_review import RetimeBreakReviewError, RetimeBreakReviewWindow  # noqa: E402


class RetimeBreakReviewTests(unittest.TestCase):
    def test_report_is_identity_checked_and_no_candidates_are_preselected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate-report.json"
            payload = json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "VOY:S01E01",
                "requires_human_review": True,
                "manifest_updated": False,
                "candidates": [
                    {"label": "B1", "pair_status": "unique-pair",
                     "ld_black": [10.0, 11.0],
                     "pair_candidates": [{"id": "D1", "start_s": 10.0, "end_s": 11.0}]},
                    {"label": "B2", "pair_status": "no-dvd-silence",
                     "ld_black": [20.0, 21.0], "pair_candidates": []},
                ],
            }).encode("utf-8")
            path.write_bytes(payload)
            view = RetimeBreakReviewWindow(None, path, "VOY:S01E01", lambda *_args: True)
            self.assertEqual(view.report_sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(set(view.rows), {"B1", "B2"})
            self.assertEqual(view.decisions, {})
            with self.assertRaisesRegex(RetimeBreakReviewError, "report-only V15 report"):
                RetimeBreakReviewWindow(None, path, "DS9:S01E01", lambda *_args: True)

    def test_candidate_detail_is_structured_not_raw_report_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate-report.json"
            path.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "requires_human_review": True, "manifest_updated": False,
                "candidates": [{
                    "label": "B1", "pair_status": "unique-pair",
                    "ld_black": [10.0, 11.0],
                    "projected_dvd_interval_s": [12.0, 13.0],
                    "offset_spread_frames": 1,
                    "pair_candidates": [{"id": "D1", "start_s": 12.0,
                                         "end_s": 13.0, "distance_s": 0.2}],
                }],
            }), encoding="utf-8")
            view = RetimeBreakReviewWindow(None, path, "DS9:S01E12", lambda *_args: True)

            class Tree:
                @staticmethod
                def selection():
                    return ["candidate-0"]

            class Detail:
                def __init__(self):
                    self.value = ""
                def configure(self, **_kwargs): pass
                def delete(self, *_args): self.value = ""
                def insert(self, _index, value): self.value += value

            view.tree = Tree()
            view._label_0 = "B1"
            view.detail = Detail()
            view._show_selected_detail()
            self.assertIn("DVD audio-silence evidence", view.detail.value)
            self.assertIn("12.000–13.000s", view.detail.value)
            self.assertNotIn('"pair_candidates"', view.detail.value)
            self.assertNotIn('{\n', view.detail.value)

    def test_candidate_review_accepts_only_hash_bound_alignment_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = Path(temporary)
            report = probe / "candidate-report.json"
            alignment = probe / "alignment.npy"
            sample = probe / "paired.gif"
            alignment.write_bytes(b"alignment")
            sample.write_bytes(b"paired frames")
            assets = probe / "review-index.json"
            sample_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
            assets.write_text(json.dumps({
                "schema": "dvd-alignment-review-assets-v1",
                "identity": "DS9:S01E12", "candidate_is_approval": False,
                "alignment_sha256": hashlib.sha256(alignment.read_bytes()).hexdigest(),
                "review_points": [{
                    "asset_status": "READY", "master_time_s": 10.5,
                    "master_frame": 315, "dvd_frame": 327,
                    "offset_frames": 12, "playback_path": str(sample),
                    "playback_sha256": sample_hash,
                }],
            }), encoding="utf-8")
            report.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "requires_human_review": True, "manifest_updated": False,
                "inputs": {"alignment_sha256": hashlib.sha256(alignment.read_bytes()).hexdigest()},
                "candidates": [{"label": "B1", "ld_black": [10.0, 11.0],
                               "projected_dvd_interval_s": [12.0, 13.0],
                               "pair_status": "unique-pair", "pair_candidates": []}],
            }), encoding="utf-8")
            view = RetimeBreakReviewWindow(
                None, report, "DS9:S01E12", lambda *_args: True,
                alignment_assets_path=assets,
                alignment_assets_sha256=hashlib.sha256(assets.read_bytes()).hexdigest())
            self.assertIn("B1", view.samples_by_candidate)
            with self.assertRaisesRegex(RetimeBreakReviewError, "index changed"):
                RetimeBreakReviewWindow(
                    None, report, "DS9:S01E12", lambda *_args: True,
                    alignment_assets_path=assets, alignment_assets_sha256="stale")

    def test_candidate_review_loads_only_dvd_audio_auditions_bound_to_source_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dvd, wav, report, index = (root / "dvd.mkv", root / "D1.wav",
                                       root / "candidate.json", root / "audio-index.json")
            dvd.write_bytes(b"DVD source")
            wav.write_bytes(b"verified audio excerpt")
            dvd_sha = hashlib.sha256(dvd.read_bytes()).hexdigest()
            candidate_payload = {
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "requires_human_review": True, "manifest_updated": False,
                "manifest_sha256": "manifest-hash",
                "inputs": {
                    "dvd_name": dvd.name, "dvd_size_bytes": dvd.stat().st_size,
                    "dvd_mtime_ns": dvd.stat().st_mtime_ns, "dvd_sha256": dvd_sha,
                },
                "candidates": [{"label": "B1", "ld_black": [10, 11],
                                "pair_candidates": [{"id": "D1"}]}],
            }
            report.write_text(json.dumps(candidate_payload), encoding="utf-8")
            report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
            index.write_text(json.dumps({
                "schema": "retime-dvd-audio-review-v1",
                "identity": "DS9:S01E12", "candidate_is_approval": False,
                "candidate_report_sha256": report_sha,
                "manifest_sha256": "manifest-hash",
                "dvd_source": str(dvd), "dvd_source_sha256": dvd_sha,
                "samples": [{"id": "D1", "status": "READY", "path": str(wav),
                             "sha256": hashlib.sha256(wav.read_bytes()).hexdigest()}],
            }), encoding="utf-8")
            view = RetimeBreakReviewWindow(
                None, report, "DS9:S01E12", lambda *_args: True,
                dvd_audio_assets_path=index)
            self.assertIn("D1", view.dvd_audio_samples)
            wav.write_bytes(b"tampered")
            view = RetimeBreakReviewWindow(
                None, report, "DS9:S01E12", lambda *_args: True,
                dvd_audio_assets_path=index)
            self.assertEqual(view.dvd_audio_samples, {})

    def test_malformed_report_labels_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate-labels.json"
            path.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "requires_human_review": True,
                "manifest_updated": False,
                "candidates": [{"label": "B1"}, {"label": "B1"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RetimeBreakReviewError, "labels.*unique"):
                RetimeBreakReviewWindow(None, path, "DS9:S01E12", lambda *_args: True)

    def test_report_without_human_review_requirement_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unreviewable-report.json"
            path.write_text(json.dumps({
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "manifest_updated": False, "candidates": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RetimeBreakReviewError, "report-only V15 report"):
                RetimeBreakReviewWindow(None, path, "DS9:S01E12", lambda *_args: True)


if __name__ == "__main__":
    unittest.main()
