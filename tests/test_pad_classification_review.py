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

    def test_load_requires_explicit_review_for_every_map_matched_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path, probe = self._fixture(Path(temporary))
            manifest, candidates, report = load_classification_evidence(
                "S01E12", "DS9", manifest_path, probe)
            self.assertEqual([row["frame"] for row in candidates], [100, 200])
            self.assertTrue(candidates[1]["residual_requires_acceptance"])
            self.assertEqual(report["original_cut_frames"], [100])
            self.assertEqual(report["rife_misidentification_warning"], RIFE_MISIDENTIFICATION_WARNING)
            self.assertNotIn("classification", candidates[0])
            self.assertEqual(manifest["identity"], "DS9:S01E12")

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
