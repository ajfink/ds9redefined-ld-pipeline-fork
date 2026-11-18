from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cadence_review_window import CadenceReviewWindow  # noqa: E402
from workflow_app_model import WorkflowAppError  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CadenceReviewWindowTests(unittest.TestCase):
    def test_cadence_candidate_samples_are_hash_checked_before_visual_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for name in ("manifest.json", "metrics.json", "coverage.json", "review.avs",
                         "map.avs", "master.mov", "native.wav", "motion-report.json",
                         "tfm.log", "motion.txt", "di-map.txt", "sample.gif"):
                path = root / name
                path.write_bytes(name.encode("utf-8"))
                files[name] = path
            files["metrics.json"].write_text(json.dumps({
                "frame_count": 2, "period5_agreement_pct": 100.0,
                "unmatched_count": 0, "combed_count": 0, "insert_count": 0,
                "repair_frame_count": 0,
            }), encoding="utf-8")
            index = root / "index.json"
            index.write_text(json.dumps({
                "schema": "cadence-review-assets-v1",
                "identity": "DS9:S01E12", "candidate_is_approval": False,
                "manifest_path": str(files["manifest.json"]),
                "manifest_sha256": sha(files["manifest.json"]),
                "metrics_path": str(files["metrics.json"]),
                "metrics_sha256": sha(files["metrics.json"]),
                "verification_report_path": str(files["coverage.json"]),
                "verification_report_sha256": sha(files["coverage.json"]),
                "review_avs_path": str(files["review.avs"]),
                "review_avs_sha256": sha(files["review.avs"]),
                "map_script_path": str(files["map.avs"]),
                "map_script_sha256": sha(files["map.avs"]),
                "source_path": str(files["master.mov"]),
                "source_sha256": sha(files["master.mov"]),
                "native_audio_path": str(files["native.wav"]),
                "native_audio_sha256": sha(files["native.wav"]),
                "motion_report_path": str(files["motion-report.json"]),
                "motion_report_sha256": sha(files["motion-report.json"]),
                "tfm_log_path": str(files["tfm.log"]), "tfm_log_sha256": sha(files["tfm.log"]),
                "motion_trace_path": str(files["motion.txt"]),
                "motion_trace_sha256": sha(files["motion.txt"]),
                "di_map_path": str(files["di-map.txt"]), "di_map_sha256": sha(files["di-map.txt"]),
                "review_intervals": [{
                    "id": "interval-1", "sample_kind": "review-required",
                    "playback_path": str(files["sample.gif"]),
                    "playback_sha256": sha(files["sample.gif"]),
                }],
            }), encoding="utf-8")
            window = CadenceReviewWindow(object(), index, "DS9:S01E12", lambda *_args: True)
            self.assertIn("interval-1", window.rows)
            self.assertEqual(window.decisions, {})

            files["sample.gif"].write_bytes(b"changed")
            with self.assertRaisesRegex(WorkflowAppError, "cadence playback sample"):
                CadenceReviewWindow(object(), index, "DS9:S01E12", lambda *_args: True)


if __name__ == "__main__":
    unittest.main()
