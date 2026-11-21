"""Centre-referenced audio-rate fits remove the window-start origin bias (fstk finding 0001)."""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from measure_audio_rate import WINDOW, centre_referenced_fit, summarize_saved_report  # noqa: E402

TRUE_OFFSET = 0.37
PPM = 864.0
SLOPE = 1.0 + PPM * 1e-6


def _rows(with_centre: bool) -> list[dict]:
    rows = []
    for i in range(12):
        start = 100.0 + 120.0 * i
        # A start-referenced match lands where the window CENTRES coincide, so the matched
        # start is shifted by (SLOPE - 1) * WINDOW / 2 from the true mapping of the start.
        y_start = TRUE_OFFSET + SLOPE * start + (SLOPE - 1.0) * WINDOW / 2.0
        row = {"out_time_s": start, "ld_wav_time_s": start, "master_time_s": start,
               "picture_offset_frames": 0, "dvd_video_time_s": start, "matched_dvd_audio_time_s": y_start,
               "master_equivalent_time_s": y_start, "corr": 0.9, "runner_up_corr": 0.2, "search_edge": False}
        if with_centre:
            row["master_equivalent_centre_time_s"] = y_start + WINDOW / 2.0
        rows.append(row)
    return rows


def _write(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter="\t")
        w.writeheader()
        w.writerows(rows)


class CentreReferenceTests(unittest.TestCase):
    def test_start_pairs_are_biased_and_centre_pairs_are_not(self):
        rows = _rows(with_centre=True)
        with tempfile.TemporaryDirectory() as tmp:
            tsv = Path(tmp) / "rate.tsv"
            _write(rows, tsv)
            summary = summarize_saved_report(tsv, 0.5)
        start_intercept = summary["theil_sen"]["intercept_s"]
        centre = summary["centre_referenced"]
        self.assertTrue(centre["exact"])
        self.assertAlmostEqual(start_intercept - TRUE_OFFSET, (SLOPE - 1.0) * WINDOW / 2.0, places=6)
        self.assertAlmostEqual(centre["theil_sen"]["intercept_s"], TRUE_OFFSET, places=6)
        self.assertAlmostEqual(centre["theil_sen"]["rate_delta_ppm"], summary["theil_sen"]["rate_delta_ppm"],
                               places=3)

    def test_older_tsv_uses_labelled_picture_map_approximation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsv = Path(tmp) / "rate.tsv"
            _write(_rows(with_centre=False), tsv)
            centre = summarize_saved_report(tsv, 0.5)["centre_referenced"]
        self.assertFalse(centre["exact"])
        self.assertIn("picture-map slope", centre["approximation"])
        self.assertAlmostEqual(centre["theil_sen"]["intercept_s"], TRUE_OFFSET, places=6)

    def test_fit_helper_respects_the_used_mask(self):
        import numpy as np

        x = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
        y = x + WINDOW / 2.0 + 1.0
        y[2] += 50.0  # an outlier excluded by the mask
        use = np.array([True, True, False, True, True])
        fit = centre_referenced_fit(x, y, use)
        self.assertAlmostEqual(fit["ols"]["intercept_s"], 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
