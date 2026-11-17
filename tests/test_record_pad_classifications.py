from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import record_pad_classifications as recorder  # noqa: E402
from ldcommon import PipelineError  # noqa: E402


class RecordPadClassificationsTests(unittest.TestCase):
    def test_report_inputs_require_unchanged_probe_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = Path(temporary) / "probe"
            probe.mkdir()
            evidence = {}
            for key, name in (("pad_audit", "audit.tsv"),
                              ("repair_frames", "repair.txt"),
                              ("fullres_report", "fullres.tsv")):
                path = probe / name
                path.write_text("test evidence\n", encoding="utf-8")
                evidence[key] = str(path)
                evidence[key + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            decision_path = probe / "review.json"
            decision_path.write_text("{}\n", encoding="utf-8")
            report = {"schema": recorder.SCHEMA, "status": "HUMAN_REVIEWED",
                      "identity": "DS9:S01E12", "evidence": evidence}

            paths = recorder._report_inputs(report, decision_path, probe, "DS9:S01E12")

            self.assertEqual(paths[:3], tuple(Path(evidence[key]) for key in
                                               ("pad_audit", "repair_frames", "fullres_report")))
            self.assertEqual(paths[3], hashlib.sha256(decision_path.read_bytes()).hexdigest())
            (probe / "audit.tsv").write_text("modified evidence\n", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "changed after human review"):
                recorder._report_inputs(report, decision_path, probe, "DS9:S01E12")

    def test_report_evidence_must_stay_in_episode_probe_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            outside = root / "outside.tsv"
            outside.write_text("evidence\n", encoding="utf-8")
            report = {"schema": recorder.SCHEMA, "status": "HUMAN_REVIEWED",
                      "identity": "DS9:S01E12", "evidence": {
                          "pad_audit": str(outside), "pad_audit_sha256": "unused"}}
            decision = probe / "review.json"
            decision.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "must be in this episode's probe root"):
                recorder._report_inputs(report, decision, probe, "DS9:S01E12")

    def test_rife_warning_is_exact_and_unambiguous(self):
        self.assertEqual(
            recorder.RIFE_MISIDENTIFICATION_WARNING,
            "ONLY generate as an interpolated frame if this pad frame was incorrectly identified as preceding a scene cut.")

    def test_apply_commits_human_lists_and_plan_paths_after_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            instance = root / "device" / "episodes" / "S01E12"
            plans = instance / "plans"
            plans.mkdir(parents=True)
            audit = probe / "audit.tsv"
            audit.write_text(
                "frame\ttime\tmotion_score\tis_predicted_pad\n"
                "10\t0.4\t0.1\t1\n11\t0.5\t0.2\t1\n", encoding="utf-8")
            repairs = probe / "repair.txt"
            repairs.write_text("10\n11\n", encoding="utf-8")
            fullres = probe / "fullres.tsv"
            fullres.write_text(
                "frame\trgb_max\trgb_mean\n10\t0\t0\n11\t0\t0\n", encoding="utf-8")
            manifest_path = instance / "manifest" / "S01E12.json"
            manifest_path.parent.mkdir()
            manifest = {
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "dvd_reference_available": False, "no_dvd_proceed_approved": True,
                "cut_pad_images_choice": "skip",
                "pads": {"cut_pads": 1, "cut_pad_frames": [10]},
            }
            manifest_original = json.dumps(manifest, indent=2) + "\n"
            manifest_path.write_text(manifest_original, encoding="utf-8")
            note = "reviewed all real source flanks"
            report = {
                "schema": recorder.SCHEMA, "status": "HUMAN_REVIEWED",
                "identity": "DS9:S01E12", "episode": "S01E12", "series": "DS9",
                "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "dvd_reference_available": False, "cut_pad_images_choice": "skip",
                "original_cut_frames": [10], "reviewed_by": recorder.getpass.getuser(),
                "review_note": note,
                "rife_misidentification_warning": recorder.RIFE_MISIDENTIFICATION_WARNING,
                "evidence": {
                    "pad_audit": str(audit), "pad_audit_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
                    "repair_frames": str(repairs), "repair_frames_sha256": hashlib.sha256(repairs.read_bytes()).hexdigest(),
                    "fullres_report": str(fullres), "fullres_report_sha256": hashlib.sha256(fullres.read_bytes()).hexdigest(),
                },
                "candidates": [
                    {"frame": 10, "classification": "cut", "cut_replacement": "hold",
                     "confirmed_hold": True, "accept_residual": False},
                    {"frame": 11, "classification": "rife", "cut_replacement": "",
                     "confirmed_hold": False, "accept_residual": False},
                ],
            }
            decision_path = plans / "review.json"
            decision_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            digest = hashlib.sha256(decision_path.read_bytes()).hexdigest()
            ep = SimpleNamespace(identity="DS9:S01E12", code="S01E12", series="DS9")
            ld = SimpleNamespace(probe=lambda: probe)

            with patch.object(recorder, "assert_writable_path"), \
                    patch.dict("os.environ", {"LDP_INSTANCE_ROOT": str(instance)}, clear=False):
                _manifest, committed_path = recorder.record_reviewed_classifications(
                    decision_path, ep, ld, manifest_path, digest, note, apply=True)

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            review = updated["pads"]["pad_classification_review"]
            committed = json.loads(committed_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["pads"]["cut_pad_frames"], [10])
            self.assertEqual(review["rife_frames"], [11])
            self.assertEqual(review["original_cut_frames"], [10])
            self.assertEqual(review["decision_report_path"], "plans/review.json")
            self.assertEqual(review["committed_report_path"], "plans/review-committed.json")
            self.assertEqual(committed["status"], "COMMITTED")
            self.assertEqual(committed["decision_sha256"], digest)
            self.assertTrue(list(manifest_path.parent.glob("S01E12.json.bak-pad-classifications-*")))


if __name__ == "__main__":
    unittest.main()
