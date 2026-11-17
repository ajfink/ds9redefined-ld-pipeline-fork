from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
from rife_padframes import validate_human_classification  # noqa: E402


class RifeHumanClassificationGateTests(unittest.TestCase):
    def _evidence(self, root: Path):
        audit = root / "audit.tsv"
        repair = root / "repair.txt"
        fullres = root / "fullres.tsv"
        audit.write_text("frame\tis_predicted_pad\n10\t1\n11\t1\n", encoding="utf-8")
        repair.write_text("10\n11\n", encoding="utf-8")
        fullres.write_text("frame\trgb_max\trgb_mean\n10\t0\t0\n11\t0\t0\n", encoding="utf-8")
        review = {
            "schema": "ld-pipeline-pad-classification-review-v1",
            "decision_sha256": "a" * 64, "reviewed_by": "operator",
            "decision_status": "HUMAN_REVIEWED",
            "review_note": "reviewed each source flank", "original_cut_frames": [10],
            "cut_frames": [10], "rife_frames": [11], "hold_frames": [], "cut_images": {},
            "human_review_frames": [10, 11], "auto_classified_rife_frames": [],
            "motion_review_policy": {"name": "source-baseline-percentile-v1",
                                     "auto_rife_below_percentile": 95.0},
            "accepted_residual_frames": [], "misidentified_cut_reclassifications": [],
            "audit_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
            "repair_sha256": hashlib.sha256(repair.read_bytes()).hexdigest(),
            "fullres_sha256": hashlib.sha256(fullres.read_bytes()).hexdigest(),
        }
        manifest = {"pads": {"cut_pads": 1, "cut_pad_frames": [10],
                             "pad_classification_review": review}}
        return manifest, audit, repair, fullres

    def test_rife_requires_completed_human_classification_for_every_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, audit, repair, fullres = self._evidence(root)
            validate_human_classification(manifest, {10, 11}, audit, repair, fullres,
                                          set(), set(), set())
            manifest["pads"].pop("pad_classification_review")
            with self.assertRaisesRegex(PipelineError, "must be recorded before RIFE"):
                validate_human_classification(manifest, {10, 11}, audit, repair, fullres,
                                              set(), set(), set())

    def test_stale_inputs_or_changed_cut_policy_stop_before_rife(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, audit, repair, fullres = self._evidence(root)
            repair.write_text("10\n11\n12\n", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "changed after human"):
                validate_human_classification(manifest, {10, 11}, audit, repair, fullres,
                                              set(), set(), set())
            manifest, audit, repair, fullres = self._evidence(root)
            with self.assertRaisesRegex(PipelineError, "decisions disagree"):
                validate_human_classification(manifest, {10, 11}, audit, repair, fullres,
                                              {10}, set(), set())

    def test_empty_candidate_set_needs_no_rife_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, audit, repair, fullres = self._evidence(root)
            validate_human_classification(manifest, set(), audit, repair, fullres,
                                          set(), set(), set())

    def test_device_plan_reports_are_hash_checked_before_rife(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, audit, repair, fullres = self._evidence(root)
            instance = root / "device" / "episodes" / "S01E12"
            plans = instance / "plans"
            plans.mkdir(parents=True)
            decision = {
                "schema": "ld-pipeline-pad-classification-review-v1", "status": "HUMAN_REVIEWED",
                "identity": "DS9:S01E12", "review_note": "reviewed each source flank",
                "reviewed_by": "operator", "original_cut_frames": [10],
                "human_review_frames": [10, 11], "auto_classified_rife_frames": [],
                "evidence": {
                    "pad_audit_sha256": manifest["pads"]["pad_classification_review"]["audit_sha256"],
                    "repair_frames_sha256": manifest["pads"]["pad_classification_review"]["repair_sha256"],
                    "fullres_report_sha256": manifest["pads"]["pad_classification_review"]["fullres_sha256"],
                },
                "candidates": [{"frame": 10, "classification": "cut", "decision_source": "human"},
                               {"frame": 11, "classification": "rife", "decision_source": "human"}],
            }
            decision_path = plans / "decision.json"
            decision_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
            decision_sha = hashlib.sha256(decision_path.read_bytes()).hexdigest()
            committed = {
                "schema": "ld-pipeline-pad-classification-review-v1", "status": "COMMITTED",
                "identity": "DS9:S01E12", "decision_sha256": decision_sha,
                "cut_frames": [10], "rife_frames": [11],
            }
            committed_path = plans / "decision-committed.json"
            committed_path.write_text(json.dumps(committed) + "\n", encoding="utf-8")
            review = manifest["pads"]["pad_classification_review"]
            review["decision_sha256"] = decision_sha
            review["decision_report_path"] = "plans/decision.json"
            review["committed_report_path"] = "plans/decision-committed.json"

            validate_human_classification(
                manifest, {10, 11}, audit, repair, fullres, set(), set(), set(),
                identity="DS9:S01E12", instance_root=instance)

            decision_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "decision_report_path"):
                validate_human_classification(
                    manifest, {10, 11}, audit, repair, fullres, set(), set(), set(),
                    identity="DS9:S01E12", instance_root=instance)

    def test_motion_baseline_autos_as_rife_only_when_source_percentile_is_below_p95(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit.tsv"
            audit.write_text(
                "frame\tmotion_score\tbaseline_percentile\tis_predicted_pad\n"
                "20\t0.25\t72.5\t1\n", encoding="utf-8")
            repair = root / "repair.txt"
            repair.write_text("20\n", encoding="utf-8")
            fullres = root / "fullres.tsv"
            fullres.write_text("frame\trgb_max\trgb_mean\n20\t0\t0\n", encoding="utf-8")
            instance = root / "device" / "episodes" / "S01E12"
            plans = instance / "plans"
            plans.mkdir(parents=True)
            evidence = {
                "pad_audit_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
                "repair_frames_sha256": hashlib.sha256(repair.read_bytes()).hexdigest(),
                "fullres_report_sha256": hashlib.sha256(fullres.read_bytes()).hexdigest(),
            }
            note = ("Automatic non-cut classification: source-baseline motion percentile below "
                    "95; cut-like/uncertain candidates require human review.")
            decision = {
                "schema": "ld-pipeline-pad-classification-review-v1", "status": "AUTO_CLASSIFIED",
                "identity": "DS9:S01E12", "review_note": note,
                "reviewed_by": "automatic-motion-baseline-p95", "original_cut_frames": [],
                "human_review_frames": [], "auto_classified_rife_frames": [20],
                "evidence": evidence,
                "candidates": [{"frame": 20, "classification": "rife",
                                "decision_source": "motion-baseline-p95",
                                "motion_score": 0.25, "baseline_percentile": 72.5}],
            }
            decision_path = plans / "decision.json"
            decision_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
            digest = hashlib.sha256(decision_path.read_bytes()).hexdigest()
            committed = {"schema": "ld-pipeline-pad-classification-review-v1",
                         "status": "COMMITTED", "identity": "DS9:S01E12",
                         "decision_sha256": digest, "cut_frames": [], "rife_frames": [20]}
            committed_path = plans / "decision-committed.json"
            committed_path.write_text(json.dumps(committed) + "\n", encoding="utf-8")
            review = {
                "schema": "ld-pipeline-pad-classification-review-v1", "decision_status": "AUTO_CLASSIFIED",
                "decision_sha256": digest, "decision_report_path": "plans/decision.json",
                "committed_report_path": "plans/decision-committed.json",
                "reviewed_by": "automatic-motion-baseline-p95", "review_note": note,
                "original_cut_frames": [], "cut_frames": [], "rife_frames": [20],
                "hold_frames": [], "cut_images": {}, "accepted_residual_frames": [],
                "human_review_frames": [], "auto_classified_rife_frames": [20],
                "misidentified_cut_reclassifications": [], "motion_review_policy": {
                    "name": "source-baseline-percentile-v1", "auto_rife_below_percentile": 95.0},
                "audit_sha256": evidence["pad_audit_sha256"],
                "repair_sha256": evidence["repair_frames_sha256"],
                "fullres_sha256": evidence["fullres_report_sha256"],
            }
            manifest = {"pads": {"cut_pad_frames": [], "cut_pads": 0,
                                 "pad_classification_review": review}}
            validate_human_classification(
                manifest, {20}, audit, repair, fullres, set(), set(), set(),
                identity="DS9:S01E12", instance_root=instance)

            audit.write_text(
                "frame\tmotion_score\tbaseline_percentile\tis_predicted_pad\n"
                "20\t0.25\t96\t1\n", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "not eligible"):
                validate_human_classification(
                    manifest, {20}, audit, repair, fullres, set(), set(), set(),
                    identity="DS9:S01E12", instance_root=instance)


if __name__ == "__main__":
    unittest.main()
