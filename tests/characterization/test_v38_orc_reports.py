"""Characterization: v38 tests/test_render_stack.py (evidence) and the Orc-report parts of
tests/test_select_stack_reference.py, ported verbatim against fstk.tools.orc."""
from __future__ import annotations

import json
import unittest

from fstk.core.errors import ArtifactError as PipelineError
from fstk.tools.orc import (
    expected_pair_count,
    parse_refresh_report,
    source_alignment_decision,
    validate_render_evidence,
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


class StackRenderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.labels = ["L1", "M1", "P1", "P2"]
        self.groups = [{"value": self.labels, "Count": len(self.labels)}]
        self.output = "\n".join([
            "tbc_source: Loaded TBC Source L1",
            "tbc_source: Loaded TBC Source M1",
            "tbc_source: Loaded TBC Source P1",
            "tbc_source: Loaded TBC Source P2",
            "StackerStage: inputs grouped by pressing over 16 frame(s) across the stack: {1,2,3,4}",
            'StackerStage report: {"inputs":[{"input":1},{"input":2},{"input":3},{"input":4}],"pairs":6}',
            "Unrecoverable samples: 0",
            "Successfully wrote 83,230 frames",
        ])

    def test_validates_loaded_sources_stacker_pairs_recovery_and_frames(self):
        result = validate_render_evidence(self.output, self.labels, 83230, 83230, self.groups)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["pair_count_check"], "pass: 6 all-pairs")

    def test_rejects_missing_source_and_unrecoverable_samples(self):
        output = self.output.replace("tbc_source: Loaded TBC Source P2\n", "").replace(
            "Unrecoverable samples: 0", "Unrecoverable samples: 2")
        result = validate_render_evidence(output, self.labels, 83230, 83230, self.groups)
        self.assertFalse(result["ok"])
        self.assertTrue(any("Loaded line" in issue for issue in result["issues"]))
        self.assertTrue(any("unrecoverable" in issue for issue in result["issues"]))

    def test_rejects_logged_and_decoded_frame_count_mismatch(self):
        result = validate_render_evidence(self.output, self.labels, 83230, 83170, self.groups)
        self.assertFalse(result["ok"])
        self.assertTrue(any("FFprobe frame count" in issue for issue in result["issues"]))

    def test_pair_count_requires_explicit_pressing_group_membership(self):
        self.assertEqual(expected_pair_count(4), 6)
        result = validate_render_evidence(self.output, self.labels, 83230, 83230, [])
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["pair_count_check"], "pass: 6 all-pairs")
        self.assertTrue(result["review_flags"])


class OrcRefreshReportTests(unittest.TestCase):
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
