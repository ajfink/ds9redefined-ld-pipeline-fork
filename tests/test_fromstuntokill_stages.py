from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_stage import STAGES, make_action  # noqa: E402


class FromStunToKillV15StageTests(unittest.TestCase):
    def test_gui_intake_and_stack_stages_are_allowlisted_with_review_boundaries(self):
        self.assertIn("init-local-episode", STAGES)
        self.assertIn("render-stack", STAGES)
        self.assertIn("prepare-retime-breaks", STAGES)
        self.assertIn("record-retime-breaks", STAGES)
        self.assertIn("record-pad-classifications", STAGES)
        self.assertIn("verify-efm-capture", STAGES)
        self.assertIn("decode-efm-captures", STAGES)
        self.assertIn("fade-midpoint-fit", STAGES)
        self.assertIn("alignment-samples", STAGES)
        self.assertIn("measure-dvd-alignment", STAGES)
        self.assertIn("alignment-review-assets", STAGES)
        self.assertIn("record-dvd-alignment-review", STAGES)
        self.assertIn("retime-review-assets", STAGES)
        self.assertIn("record-video-trim-review", STAGES)
        self.assertIn("cadence-evidence", STAGES)
        self.assertIn("cadence-analysis", STAGES)
        self.assertIn("cadence-review-assets", STAGES)
        self.assertIn("record-cadence-review", STAGES)
        self.assertIn("record-video-trim-review", STAGES)
        self.assertIn("cadence-evidence", STAGES)
        self.assertIn("cadence-analysis", STAGES)
        self.assertIn("cadence-review-assets", STAGES)
        self.assertIn("record-cadence-review", STAGES)
        self.assertIn("audio-promote-no-dvd", STAGES)
        self.assertIn("build-ld-synced", STAGES)
        self.assertIn("record-efm-capture", STAGES)
        self.assertTrue(STAGES["render-stack"].get("review_gate"))
        self.assertFalse(STAGES["prepare-retime-breaks"].get("review_gate"))
        self.assertFalse(STAGES["record-pad-classifications"].get("review_gate"))
        self.assertTrue(STAGES["record-efm-capture"].get("review_gate"))

    def test_local_initializer_and_stack_renderer_use_bounded_python_helpers(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        local = make_action("init-local-episode", "S01E01", profile,
                            ["--series", "VOY", "--local-source", r"D:\captures",
                             "--confirmed-title", "Caretaker"], None, "VOY")
        self.assertTrue(local[1].endswith("init_local_episode.py"))
        self.assertEqual(local[local.index("--episode") + 1], "S01E01")
        self.assertIn("--series", local)

        render = make_action("render-stack", "S01E01", profile,
                             ["--planned-gib", "25", "--report-out", r"D:\probe\render.json"], None, "VOY")
        self.assertTrue(render[1].endswith("render_stack.py"))
        self.assertEqual(render[render.index("--episode") + 1], "S01E01")
        self.assertEqual(render[-4:], ["--planned-gib", "25", "--report-out", r"D:\probe\render.json"])

    def test_alignment_sample_stage_uses_the_allowlisted_candidate_builder(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "alignment-samples", "S01E12", profile,
            ["--series", "VOY", "--report", r"D:\probe\fit.json",
             "--out-dir", r"D:\probe\samples"], None, "VOY")
        self.assertTrue(command[1].endswith("build_alignment_samples.py"))
        self.assertEqual(command[command.index("--episode") + 1], "S01E12")
        self.assertIn(r"D:\probe\fit.json", command)

    def test_alignment_review_assets_and_decision_stages_are_allowlisted(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        assets = make_action(
            "alignment-review-assets", "S01E12", profile,
            ["--series", "VOY", "--alignment", r"D:\probe\alignment.npy",
             "--out-dir", r"D:\probe\review"], None, "VOY")
        self.assertTrue(assets[1].endswith("build_alignment_review_assets.py"))
        decision = make_action(
            "record-dvd-alignment-review", "S01E12", profile,
            ["--series", "VOY", "--decision", "REJECTED"], None, "VOY")
        self.assertTrue(decision[1].endswith("record_dvd_alignment_review.py"))
        audio = make_action(
            "retime-review-assets", "S01E12", profile,
            ["--series", "VOY", "--candidate-report", r"D:\probe\candidates.json",
             "--out-dir", r"D:\probe\audio-samples"], None, "VOY")
        self.assertTrue(audio[1].endswith("build_retime_review_assets.py"))
        cadence = make_action(
            "cadence-evidence", "S01E12", profile,
            ["--series", "VOY", "--avs-out", r"D:\probe\cadence.avs"], None, "VOY")
        self.assertTrue(cadence[1].endswith("generate_cadence_evidence.py"))
        cadence = make_action(
            "cadence-evidence", "S01E12", profile,
            ["--series", "VOY", "--avs-out", r"D:\probe\tfm.avs"], None, "VOY")
        self.assertTrue(cadence[1].endswith("generate_cadence_evidence.py"))

    def test_pad_classification_recorder_is_allowlisted_as_a_python_stage(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "record-pad-classifications", "S01E12", profile,
            ["--decision-file", r"D:\device\plans\review.json", "--apply"], None)
        self.assertTrue(command[1].endswith("record_pad_classifications.py"))
        self.assertEqual(command[command.index("--episode") + 1], "S01E12")
        self.assertIn("--decision-file", command)

    def test_efm_provenance_verifier_is_allowlisted(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "verify-efm-capture", "S01E13", profile,
            ["--capture", "P3", "--orc-log", r"D:\logs\efm.log",
             "--wav", r"D:\audio\P3.wav", "--efm-report", r"D:\audio\P3.wav.txt",
             "--report-out", r"D:\references\P3-provenance.json",
             "--confirm-manifest-sha256", "a" * 64], None)
        self.assertTrue(command[1].endswith("verify_efm_capture.py"))
        self.assertIn("--capture", command)
        self.assertIn("--orc-log", command)

    def test_orc_efm_decode_batch_is_allowlisted(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "decode-efm-captures", "S01E13", profile,
            ["--capture", "L1", "--capture", "P3", "--run-token", "abcdefgh",
             "--confirm-manifest-sha256", "a" * 64], None)
        self.assertTrue(command[1].endswith("decode_efm_captures.py"))
        self.assertEqual(command[command.index("--episode") + 1], "S01E13")
        self.assertEqual(command.count("--capture"), 2)

    def test_fade_midpoint_fit_is_an_allowlisted_read_evidence_stage(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "fade-midpoint-fit", "S01E13", profile,
            ["--out", r"D:\probe\fade-fit.json"], None)
        self.assertTrue(command[1].endswith("fade_midpoint_fit.py"))
        self.assertIn("--out", command)

    def test_no_dvd_promotion_passes_planned_new_outputs_and_manifest_hash(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action(
            "audio-promote-no-dvd", "S01E13", profile,
            ["--corrected-out", r"D:\audio\corrected.flac",
             "--synced-out", r"D:\audio\synced.flac",
             "--confirm-manifest-sha256", "a" * 64],
            r"D:\probe\audio-timing.json")
        self.assertTrue(command[1].endswith("audio_timing_diagnostic.py"))
        self.assertEqual(command.count("--report-out"), 1)
        self.assertIn("--corrected-out", command)
        self.assertIn("--synced-out", command)
        self.assertIn("--confirm-manifest-sha256", command)



if __name__ == "__main__":
    unittest.main()
