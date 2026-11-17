from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
from rife_padframes import read_candidates  # noqa: E402


class RifeCandidateEvidenceTests(unittest.TestCase):
    def _write(self, root: Path, audit: str, repairs: str, fullres: str):
        audit_path = root / "audit.tsv"
        repair_path = root / "repair.txt"
        fullres_path = root / "fullres.tsv"
        audit_path.write_text(audit, encoding="utf-8")
        repair_path.write_text(repairs, encoding="utf-8")
        fullres_path.write_text(fullres, encoding="utf-8")
        return audit_path, repair_path, fullres_path

    def test_valid_candidate_requires_matching_full_raster_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write(
                Path(temporary),
                "frame\ttime\tmotion_score\tis_predicted_pad\n100\t4.17\t0.2\t1\n",
                "100\n",
                "frame\trgb_max\trgb_mean\n100\t0\t0.000000000\n",
            )
            self.assertEqual(read_candidates(*paths), [{
                "frame": 100, "time": 4.17, "motion": 0.2,
                "rgb_max": 0, "rgb_mean": 0.0,
            }])

    def test_duplicate_audit_rows_fail_instead_of_silently_replacing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write(
                Path(temporary),
                "frame\ttime\tmotion_score\tis_predicted_pad\n100\t4.17\t0.2\t1\n100\t4.17\t0.3\t1\n",
                "100\n",
                "frame\trgb_max\trgb_mean\n100\t0\t0\n",
            )
            with self.assertRaisesRegex(PipelineError, "duplicate frame 100"):
                read_candidates(*paths)

    def test_duplicate_or_negative_repair_indices_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write(
                Path(temporary),
                "frame\ttime\tmotion_score\tis_predicted_pad\n100\t4.17\t0.2\t1\n",
                "100\n100\n",
                "frame\trgb_max\trgb_mean\n100\t0\t0\n",
            )
            with self.assertRaisesRegex(PipelineError, "duplicate or negative"):
                read_candidates(*paths)

    def test_nan_full_raster_value_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write(
                Path(temporary),
                "frame\ttime\tmotion_score\tis_predicted_pad\n100\t4.17\t0.2\t1\n",
                "100\n",
                "frame\trgb_max\trgb_mean\n100\t0\tnan\n",
            )
            with self.assertRaisesRegex(PipelineError, "out-of-range pixel statistics"):
                read_candidates(*paths)


if __name__ == "__main__":
    unittest.main()
