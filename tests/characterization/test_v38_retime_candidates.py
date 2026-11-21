"""Characterization: v38 tests/test_retime.py (10 tests), ported by tools/port_v38.py."""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError as PipelineError
from fstk.media.retime import CANDIDATE_SCHEMA, candidate_break_entries, pair_ld_intervals

ROOT = Path(__file__).resolve().parents[1]


def align_for(intervals: list[tuple[float, float]], offset: float = 0.0) -> np.ndarray:
    rows = []
    for start, end in intervals:
        lo = int(start * 30)
        hi = int(end * 30)
        rows.extend([[frame, offset] for frame in range(lo, hi, 10)])
    rows = sorted({int(row[0]): row for row in rows}.values(), key=lambda row: row[0])
    return np.asarray(rows, dtype=np.float64)


class CandidatePairingTests(unittest.TestCase):
    def test_unique_interval_pair_is_reported_without_offsets(self):
        ld = [{"label": "B1", "start_s": 10.0, "end_s": 11.0,
               "start_frame": 300, "end_frame_exclusive": 330}]
        result = pair_ld_intervals(ld, [(10.0, 11.0), (30.0, 31.0)],
                                   align_for([(10.0, 11.0)]),
                                   pair_tolerance_s=0.5, max_offset_spread_frames=2)
        self.assertEqual(result[0]["pair_status"], "unique-pair")
        self.assertEqual(result[0]["pair_candidates"][0]["id"], "D1")
        self.assertNotIn("off_before", result[0])
        self.assertNotIn("step", result[0])

    def test_multiple_dvd_silences_are_ambiguous_not_nearest_chosen(self):
        ld = [{"label": "B1", "start_s": 10.0, "end_s": 11.0,
               "start_frame": 300, "end_frame_exclusive": 330}]
        result = pair_ld_intervals(ld, [(9.5, 10.0), (11.0, 11.5)],
                                   align_for([(10.0, 11.0)]),
                                   pair_tolerance_s=0.5, max_offset_spread_frames=2)
        self.assertEqual(result[0]["pair_status"], "ambiguous-pair")
        self.assertEqual(len(result[0]["pair_candidates"]), 2)

    def test_many_to_one_dvd_silence_claims_mark_all_ld_candidates_ambiguous(self):
        ld = [
            {"label": "B1", "start_s": 10.0, "end_s": 11.0,
             "start_frame": 300, "end_frame_exclusive": 330},
            {"label": "B2", "start_s": 12.0, "end_s": 13.0,
             "start_frame": 360, "end_frame_exclusive": 390},
        ]
        result = pair_ld_intervals(ld, [(9.5, 13.5)], align_for([(10.0, 11.0), (12.0, 13.0)]),
                                   pair_tolerance_s=0.5, max_offset_spread_frames=1)
        self.assertEqual([row["pair_status"] for row in result],
                         ["many-to-one-collision", "many-to-one-collision"])

    def test_alignment_out_of_range_and_offset_transition_are_explicit(self):
        out = pair_ld_intervals(
            [{"label": "B1", "start_s": 1.0, "end_s": 1.5,
              "start_frame": 30, "end_frame_exclusive": 45}],
            [(1.0, 1.5)], np.asarray([[300.0, 0.0], [330.0, 0.0]]),
            pair_tolerance_s=1, max_offset_spread_frames=4)
        self.assertEqual(out[0]["pair_status"], "alignment-uncovered")

        transition = pair_ld_intervals(
            [{"label": "B2", "start_s": 10.0, "end_s": 11.0,
              "start_frame": 300, "end_frame_exclusive": 330}],
            [(10.0, 11.0)],
            np.asarray([[300.0, 0.0], [310.0, 1.0], [320.0, 12.0]]),
            pair_tolerance_s=1, max_offset_spread_frames=4)
        self.assertEqual(transition[0]["pair_status"], "offset-transition")

    def test_no_dvd_silence_is_not_automatically_classified_as_merged_music(self):
        result = pair_ld_intervals(
            [{"label": "B1", "start_s": 10.0, "end_s": 11.0,
              "start_frame": 300, "end_frame_exclusive": 330}], [],
            align_for([(10.0, 11.0)]), pair_tolerance_s=1,
            max_offset_spread_frames=1)
        self.assertEqual(result[0]["pair_status"], "no-dvd-silence")

    def test_bad_or_duplicate_interval_metadata_fails_closed(self):
        with self.assertRaisesRegex(PipelineError, "labels.*unique"):
            pair_ld_intervals([
                {"label": "B1", "start_s": 1, "end_s": 2},
                {"label": "B1", "start_s": 3, "end_s": 4}],
                [], align_for([(1, 2), (3, 4)]), pair_tolerance_s=1,
                max_offset_spread_frames=2)
        with self.assertRaisesRegex(PipelineError, "strictly increasing"):
            pair_ld_intervals([], [], np.asarray([[10, 0], [10, 1]]),
                              pair_tolerance_s=1, max_offset_spread_frames=2)



class CandidateRecordSafetyTests(unittest.TestCase):
    def test_default_preview_and_selected_entries_leave_offsets_null(self):
        report = {"schema": CANDIDATE_SCHEMA, "parameters": {}, "inputs": {"dvd_duration_s": 100.0},
                  "candidates": [{"label": "B1", "pair_status": "unique-pair",
                                  "ld_black": [1.0, 2.0],
                                  "pair_candidates": [{"id": "D1", "start_s": 1.0, "end_s": 2.0}]}]}
        entries, merged = candidate_break_entries(
            report, ["B1"], [], reviewed_by="tester", review_note="reviewed evidence",
            report_sha256="a" * 64)
        self.assertEqual(merged, [])
        self.assertIsNone(entries[0]["off_before"])
        self.assertIsNone(entries[0]["off_after"])
        self.assertIsNone(entries[0]["step"])
        self.assertIsNone(entries[0]["gap_s"])
        self.assertEqual(entries[0]["_candidate_provenance"]["review_note"], "reviewed evidence")

    def test_ambiguous_pair_and_unreviewed_merge_cannot_be_recorded(self):
        ambiguous = {"schema": CANDIDATE_SCHEMA, "parameters": {}, "inputs": {"dvd_duration_s": 100.0},
                     "candidates": [{"label": "B1", "pair_status": "ambiguous-pair",
                                     "ld_black": [1.0, 2.0],
                                     "pair_candidates": [{"id": "D1"}, {"id": "D2"}]}]}
        with self.assertRaisesRegex(PipelineError, "not a unique pair"):
            candidate_break_entries(ambiguous, ["B1"], [], reviewed_by="x",
                                    review_note="human review", report_sha256="a" * 64)
        no_silence = {"schema": CANDIDATE_SCHEMA, "parameters": {}, "inputs": {"dvd_duration_s": 100.0},
                      "candidates": [{"label": "B1", "pair_status": "ambiguous-pair",
                                      "ld_black": [1.0, 2.0], "pair_candidates": []}]}
        with self.assertRaisesRegex(PipelineError, "not an unmatched candidate"):
            candidate_break_entries(no_silence, [], ["B1"], reviewed_by="x",
                                    review_note="human review", report_sha256="a" * 64)

    def test_explicit_merged_music_record_is_flagged_and_keeps_offsets_null(self):
        report = {"schema": CANDIDATE_SCHEMA, "parameters": {}, "inputs": {"dvd_duration_s": 100.0},
                  "candidates": [{"label": "B2", "pair_status": "no-dvd-silence",
                                  "ld_black": [3.0, 4.0], "pair_candidates": []}]}
        entries, merged = candidate_break_entries(
            report, [], ["B2"], reviewed_by="tester",
            review_note="DVD audio has measured music through this black",
            report_sha256="b" * 64)
        self.assertEqual([row["label"] for row in merged], ["B2"])
        self.assertEqual(entries, [])
        self.assertNotIn("dvd_silence", merged[0])
        self.assertIsNone(merged[0]["step"])

    def test_many_selected_candidates_cannot_claim_same_dvd_silence(self):
        report = {"schema": CANDIDATE_SCHEMA, "parameters": {}, "inputs": {"dvd_duration_s": 100.0},
                  "candidates": [
                      {"label": "B1", "pair_status": "unique-pair", "ld_black": [1.0, 2.0],
                       "pair_candidates": [{"id": "D1", "start_s": 1.0, "end_s": 2.0}]},
                      {"label": "B2", "pair_status": "unique-pair", "ld_black": [3.0, 4.0],
                       "pair_candidates": [{"id": "D1", "start_s": 1.0, "end_s": 2.0}]},
                  ]}
        with self.assertRaisesRegex(PipelineError, "shares DVD silence"):
            candidate_break_entries(report, ["B1", "B2"], [], reviewed_by="x",
                                    review_note="human review", report_sha256="a" * 64)
