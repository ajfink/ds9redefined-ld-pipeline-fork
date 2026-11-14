import csv
import tempfile
import unittest
from pathlib import Path

from scripts.rife_padframes import read_candidates


class SourcePadCandidateTests(unittest.TestCase):
    def test_only_repair_list_frames_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit, repair, fullres = (root / "audit.tsv", root / "repair.txt", root / "fullres.tsv")
            with audit.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(("frame", "time", "black_duration", "zero_max", "motion_score",
                            "baseline_percentile", "predicted_master_pad", "is_predicted_pad"))
                w.writerow((10, "0.417", "0.042", 0, "0.1", "1", 12, 1))
                # A genuine black/content blip must not become a replacement just because
                # it is zero-valued: it is absent from the map-matched repair list.
                w.writerow((20, "0.834", "0.042", 0, "0.1", "1", "", 0))
            repair.write_text("10\n", encoding="utf-8")
            with fullres.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(("frame", "rgb_max", "rgb_mean"))
                w.writerow((10, 0, 0.0))
                w.writerow((20, 0, 0.0))

            result = read_candidates(audit, repair, fullres)
            self.assertEqual([row["frame"] for row in result], [10])

    def test_empty_repair_list_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit, repair, fullres = (root / "audit.tsv", root / "repair.txt", root / "fullres.tsv")
            audit.write_text("frame\tis_predicted_pad\n", encoding="utf-8")
            repair.write_text("\n", encoding="utf-8")
            fullres.write_text("frame\trgb_max\trgb_mean\n", encoding="utf-8")
            self.assertEqual(read_candidates(audit, repair, fullres), [])


if __name__ == "__main__":
    unittest.main()
