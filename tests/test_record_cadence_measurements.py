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

import record_cadence_measurements as recorder  # noqa: E402
import validate_episode as validator  # noqa: E402
from fileutil import sha256_file  # noqa: E402


class CadenceMeasurementRecorderTests(unittest.TestCase):
    def _fixture(self, root: Path):
        probe, avs = root / "probe", root / "avs"
        probe.mkdir()
        avs.mkdir()
        manifest = root / "manifest.json"
        master, native = root / "master.mov", root / "native.wav"
        master.write_bytes(b"master")
        native.write_bytes(b"native audio")
        trim_review = {
            "status": "APPROVED", "head_trim": 2, "tail_trim": 1,
            "audio_pad_frames": 3, "stack_master_sha256": sha256_file(master),
            "native_audio_sha256": sha256_file(native),
            "capture": "P1", "efm_provenance_sha256": "provenance",
        }
        manifest.write_text(json.dumps({
            "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            "audio": {"pad_frames": 3, "capture": "P1", "efm_provenance_required": True,
                      "efm_provenance": {"P1": [{"report_sha256": "provenance"}]},
                      "capture_selection": {"capture": "P1"}},
            "captures": [{"label": "P1"}],
            "video": {"head_trim": 2, "tail_trim": 1, "trim_review": trim_review},
            "files": {},
        }), encoding="utf-8")
        trim_digest = recorder._digest_object(trim_review)
        tfm_log, motion, di_map, summary, motion_report = (
            probe / "tfm.log", probe / "motion.txt", probe / "di-map.txt",
            probe / "summary.txt", probe / "motion-report.json")
        tfm_log.write_text("0 a [0]\n1 b [0]\n", encoding="utf-8")
        motion.write_text("0.0\n", encoding="utf-8")
        di_map.write_text("[]\n", encoding="utf-8")
        summary.write_text("synthetic cadence summary\n", encoding="utf-8")
        motion_report.write_text(json.dumps({
            "schema": "motion-trace-evidence-v1", "status": "VERIFIED",
        }), encoding="utf-8")
        map_script, review_avs = avs / "cadence.avs", probe / "review.avs"
        map_script.write_text("return BlankClip(length=2)\n", encoding="utf-8")
        review_avs.write_text("return BlankClip(length=2)\n", encoding="utf-8")
        verification = probe / "coverage.json"
        verification.write_text(json.dumps({
            "schema": "full-side-cadence-evidence-v1", "status": "VERIFIED",
            "identity": "DS9:S01E12", "tfm_log_sha256": sha256_file(tfm_log),
        }), encoding="utf-8")
        metrics = probe / "metrics.json"
        metrics_payload = {
            "schema": "cadence-measurements-v1", "status": "CANDIDATE_ONLY",
            "candidate_is_approval": False, "identity": "DS9:S01E12", "frame_count": 2,
            "period5_agreement_pct": 0.0, "unmatched_count": 0, "combed_count": 0,
            "combed_spans": [], "insert_count": 0, "repair_frame_count": 0,
            "repair_frame_pct": 0.0, "review_intervals": [],
            "inputs": {
                "manifest_sha256": sha256_file(manifest),
                "trim_review_sha256": trim_digest,
                "stack_master_path": str(master), "stack_master_sha256": sha256_file(master),
                "native_audio_path": str(native), "native_audio_sha256": sha256_file(native),
                "tfm_log_path": str(tfm_log), "tfm_log_sha256": sha256_file(tfm_log),
                "verification_report_path": str(verification),
                "verification_report_sha256": sha256_file(verification),
                "motion_trace_path": str(motion), "motion_trace_sha256": sha256_file(motion),
                "motion_report_path": str(motion_report),
                "motion_report_sha256": sha256_file(motion_report),
                "map_script_path": str(map_script), "map_script_sha256": sha256_file(map_script),
                "di_map_path": str(di_map), "di_map_sha256": sha256_file(di_map),
                "legacy_summary_path": str(summary), "legacy_summary_sha256": sha256_file(summary),
            },
        }
        metrics.write_text(json.dumps(metrics_payload), encoding="utf-8")
        review_dir = probe / "review-assets"
        review_dir.mkdir()
        gif = review_dir / "sample.gif"
        gif.write_bytes(b"GIF sample")
        contact = review_dir / "contact.png"
        contact.write_bytes(b"contact")
        index = review_dir / "index.json"
        index_payload = {
            "schema": "cadence-review-assets-v1",
            "identity": "DS9:S01E12", "candidate_is_approval": False,
            "manifest_path": str(manifest), "manifest_sha256": sha256_file(manifest),
            "metrics_path": str(metrics), "metrics_sha256": sha256_file(metrics),
            "verification_report_path": str(verification),
            "verification_report_sha256": sha256_file(verification),
            "review_avs_path": str(review_avs), "review_avs_sha256": sha256_file(review_avs),
            "map_script_path": str(map_script), "map_script_sha256": sha256_file(map_script),
            "legacy_summary_path": str(summary), "legacy_summary_sha256": sha256_file(summary),
            "source_path": str(master), "source_sha256": sha256_file(master),
            "native_audio_path": str(native), "native_audio_sha256": sha256_file(native),
            "motion_report_path": str(motion_report),
            "motion_report_sha256": sha256_file(motion_report),
            "tfm_log_path": str(tfm_log), "tfm_log_sha256": sha256_file(tfm_log),
            "motion_trace_path": str(motion), "motion_trace_sha256": sha256_file(motion),
            "di_map_path": str(di_map), "di_map_sha256": sha256_file(di_map),
            "frame_count": 2,
            "review_intervals": [{
                "id": "review-001", "sample_kind": "review-required", "status": "READY",
                "playback_path": str(gif), "playback_sha256": sha256_file(gif),
            }],
            "contact_sheet": str(contact), "contact_sheet_sha256": sha256_file(contact),
        }
        index.write_text(json.dumps(index_payload), encoding="utf-8")
        ld = SimpleNamespace(
            profile={},
            probe=lambda: probe,
            root=lambda name: {"probe": probe, "avs": avs}[name],
        )

        class Episode:
            code, series, identity = "S01E12", "DS9", "DS9:S01E12"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            def __init__(self):
                self.ld = ld
            def path(self, key):
                return {"stack_master": master}[key]

        return probe, avs, manifest, index, Episode(), ld

    def test_approved_cadence_review_records_only_verified_metrics_with_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, _avs, manifest, index, ep, ld = self._fixture(Path(temporary))
            output = probe / "approved-cadence-review.json"
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)), \
                    patch.object(recorder, "validate_efm_capture_provenance"):
                result = recorder.record_review(
                    ep, ld, index, decision="APPROVED",
                    decisions={"review-001": "accepted"},
                    review_note="All flagged samples reviewed",
                    expected_manifest_sha256=sha256_file(manifest),
                    expected_index_sha256=sha256_file(index), report_out=output)
            updated = json.loads(manifest.read_text(encoding="utf-8"))
            video = updated["video"]
            self.assertEqual(video["tfm_unmatched"], 0)
            self.assertEqual(video["cadence_review"]["status"], "APPROVED")
            self.assertEqual(updated["files"]["avs_encode"]["name"], "cadence.avs")
            self.assertTrue(Path(result["manifest_backup"]).is_file())
            self.assertTrue(output.is_file())

            class VerifiedEpisode:
                code, series, identity = "S01E12", "DS9", "DS9:S01E12"
                data = updated
                def __init__(self):
                    self.ld = ld
                def path(self, key):
                    if key == "stack_master":
                        return index.parent.parent.parent / "master.mov"
                    if key == "ld_native":
                        return index.parent.parent.parent / "native.wav"
                    file = updated["files"][key]
                    return {"probe": probe, "avs": _avs}[file["root"]] / file["name"]

            _avs = Path(temporary) / "avs"
            with patch.object(validator, "validate_efm_capture_provenance"):
                current, reason = validator.cadence_review_is_current(VerifiedEpisode(), updated, ld)
            self.assertTrue(current, reason)

    def test_rejected_cadence_review_does_not_update_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, _avs, manifest, index, ep, ld = self._fixture(Path(temporary))
            original = manifest.read_bytes()
            output = probe / "rejected-cadence-review.json"
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)), \
                    patch.object(recorder, "validate_efm_capture_provenance"):
                result = recorder.record_review(
                    ep, ld, index, decision="REJECTED",
                    decisions={"review-001": "unresolved"},
                    review_note="Combing remains unresolved",
                    expected_manifest_sha256=sha256_file(manifest),
                    expected_index_sha256=sha256_file(index), report_out=output)
            self.assertEqual(manifest.read_bytes(), original)
            self.assertFalse(result["manifest_updated"])


if __name__ == "__main__":
    unittest.main()
