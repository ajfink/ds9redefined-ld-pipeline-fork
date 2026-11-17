from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pad_classification_review import (  # noqa: E402
    PadClassificationReviewError, load_classification_evidence,
)
from record_pad_classifications import RIFE_MISIDENTIFICATION_WARNING  # noqa: E402


class PadClassificationEvidenceTests(unittest.TestCase):
    def _fixture(self, root: Path):
        probe = root / "probe"
        probe.mkdir()
        manifest = root / "S01E12.json"
        manifest.write_text(json.dumps({
            "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            "video": {"frames": 1000},
            "dvd_reference_available": True,
            "cut_pad_images_choice": "not-applicable",
            "pads": {"cut_pad_frames": [100]},
        }), encoding="utf-8")
        with (probe / "S01E12-pad-audit.tsv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(("frame", "time", "motion_score", "is_predicted_pad"))
            writer.writerow((100, "4.170833", "0.666200", 1))
            writer.writerow((200, "8.341667", "0.001000", 1))
        (probe / "S01E12-pad-repair-frames.txt").write_text("100\n200\n", encoding="utf-8")
        with (probe / "S01E12-pad-fullres.tsv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(("frame", "rgb_max", "rgb_mean"))
            writer.writerow((100, 0, "0.000000000"))
            writer.writerow((200, 3, "0.000600000"))
        return manifest, probe

    def test_old_cut_and_missing_baseline_candidates_stay_in_human_review_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, probe = self._fixture(Path(temporary))
            manifest, candidates, report = load_classification_evidence(
                "S01E12", "DS9", manifest_path, probe)
            self.assertEqual([row["frame"] for row in candidates], [100, 200])
            self.assertTrue(candidates[1]["residual_requires_acceptance"])
            self.assertEqual(report["original_cut_frames"], [100])
            self.assertEqual(report["rife_misidentification_warning"], RIFE_MISIDENTIFICATION_WARNING)
            self.assertTrue(candidates[0]["requires_human_review"])
            self.assertFalse(candidates[0]["auto_classified_rife"])
            self.assertEqual(report["human_review_frames"], [100, 200])
            self.assertEqual(report["auto_classified_rife_frames"], [])
            self.assertEqual(manifest["identity"], "DS9:S01E12")

    def test_motion_baseline_below_p95_is_auto_non_cut_unless_prior_cut_or_residual(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, probe = self._fixture(root)
            audit_path = probe / "S01E12-pad-audit.tsv"
            audit_path.write_text(
                "frame\ttime\tmotion_score\tbaseline_percentile\tis_predicted_pad\n"
                "100\t4.170833\t0.666200\t60\t1\n"
                "200\t8.341667\t0.001000\t20\t1\n", encoding="utf-8")
            manifest, candidates, report = load_classification_evidence(
                "S01E12", "DS9", manifest_path, probe)
            self.assertTrue(candidates[0]["requires_human_review"])  # prior cut inventory
            self.assertFalse(candidates[1]["auto_classified_rife"])  # above-threshold residual

            manifest_path.write_text(json.dumps({
                **manifest, "pads": {"cut_pad_frames": [], "cut_pads": 0},
            }), encoding="utf-8")
            _manifest, candidates, report = load_classification_evidence(
                "S01E12", "DS9", manifest_path, probe)
            self.assertTrue(candidates[0]["auto_classified_rife"])
            self.assertFalse(candidates[0]["requires_human_review"])
            self.assertEqual(report["auto_classified_rife_frames"], [100])
            self.assertEqual(report["motion_review_policy"]["auto_rife_below_percentile"], 95.0)

    def test_exact_p95_boundary_stays_in_human_cut_review_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, probe = self._fixture(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["pads"] = {"cut_pad_frames": [], "cut_pads": 0}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (probe / "S01E12-pad-audit.tsv").write_text(
                "frame\ttime\tmotion_score\tbaseline_percentile\tis_predicted_pad\n"
                "100\t4.170833\t0.2\t94.99\t1\n"
                "200\t8.341667\t0.3\t95.00\t1\n", encoding="utf-8")
            with (probe / "S01E12-pad-fullres.tsv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, delimiter="\t")
                writer.writerow(("frame", "rgb_max", "rgb_mean"))
                writer.writerow((100, 0, 0))
                writer.writerow((200, 0, 0))

            _manifest, candidates, report = load_classification_evidence(
                "S01E12", "DS9", manifest_path, probe)

            self.assertTrue(candidates[0]["auto_classified_rife"])
            self.assertFalse(candidates[1]["auto_classified_rife"])
            self.assertEqual(report["auto_classified_rife_frames"], [100])
            self.assertEqual(report["human_review_frames"], [200])

    def test_duplicate_repair_frame_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, probe = self._fixture(Path(temporary))
            (probe / "S01E12-pad-repair-frames.txt").write_text("100\n100\n", encoding="utf-8")
            with self.assertRaisesRegex(PadClassificationReviewError, "duplicate or negative"):
                load_classification_evidence("S01E12", "DS9", manifest, probe)

    def test_candidate_without_full_raster_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, probe = self._fixture(Path(temporary))
            (probe / "S01E12-pad-fullres.tsv").write_text(
                "frame\trgb_max\trgb_mean\n100\t0\t0\n", encoding="utf-8")
            with self.assertRaisesRegex(PadClassificationReviewError, "full-raster evidence"):
                load_classification_evidence("S01E12", "DS9", manifest, probe)


if __name__ == "__main__":
    unittest.main()
