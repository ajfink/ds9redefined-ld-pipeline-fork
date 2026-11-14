from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.select_stack_reference import (  # noqa: E402
    PipelineError,
    parse_refresh_report,
    source_alignment_decision,
)


def e05_l1_first_report() -> dict:
    """Minimal source-alignment excerpt from the preserved S01E05 Orc refresh."""
    return {
        "project": "stack-full-s01e05-l1first-refresh.orcprj",
        "completed": True,
        "saved": True,
        "nodes": [
            {
                "node": "Source Align",
                "stage": "source_align",
                "tool": "source_alignment",
                "status": "success",
                "statistics": {
                    "sourceCount": 4,
                    "suggestedReference": 2,
                    "referenceRunnerUp": 1,
                    "stackSigma": 1.489456721,
                    "equalSigma": 1.601192541,
                    "bestSingleSigma": 2.23750634,
                    "fitTrusted": True,
                    "fellBackToMean": False,
                    "pressingGroups": 2,
                    "worstMisfit": 0.3688925713,
                },
                "parameter_changes": {"alignmentMap": "1+2, 2+0, 3+35, 4+29"},
                "sources": [
                    {"id": 1, "label": "TBC Source L1", "stage": "tbc_source"},
                    {"id": 2, "label": "TBC Source M1", "stage": "tbc_source"},
                    {"id": 3, "label": "TBC Source P1", "stage": "tbc_source"},
                    {"id": 4, "label": "TBC Source P2", "stage": "tbc_source"},
                ],
            }
        ],
    }


class SelectStackReferenceTests(unittest.TestCase):
    def test_parses_json_report_after_orc_progress_text(self):
        payload = "refresh progress...\n" + json.dumps(e05_l1_first_report(), indent=2)
        self.assertTrue(parse_refresh_report([payload])["completed"])

    def test_maps_orc_suggestion_to_capture_label(self):
        decision = source_alignment_decision(
            e05_l1_first_report(), ["L1", "M1", "P1", "P2"]
        )
        self.assertEqual(decision["suggested_label"], "M1")
        self.assertEqual(decision["runner_up_label"], "L1")
        self.assertEqual(decision["alignment_map"], "1+2, 2+0, 3+35, 4+29")

    def test_rejects_incomplete_refresh(self):
        report = e05_l1_first_report()
        report["saved"] = False
        with self.assertRaises(PipelineError):
            source_alignment_decision(report, ["L1", "M1", "P1", "P2"])

    def test_rejects_source_count_mismatch(self):
        with self.assertRaises(PipelineError):
            source_alignment_decision(e05_l1_first_report(), ["L1", "M1", "P1"])

    def test_rejects_bad_recommendation_index(self):
        report = e05_l1_first_report()
        report["nodes"][0]["statistics"]["suggestedReference"] = 5
        with self.assertRaises(PipelineError):
            source_alignment_decision(report, ["L1", "M1", "P1", "P2"])


if __name__ == "__main__":
    unittest.main()
