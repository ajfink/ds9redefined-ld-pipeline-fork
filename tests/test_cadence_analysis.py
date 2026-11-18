from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cadence_analysis import analyze_tfm_rows, load_motion_trace, parse_tfm_log  # noqa: E402
import generate_cadence_evidence as generator  # noqa: E402
import build_cadence_review_assets as review_assets  # noqa: E402
from generate_cadence_evidence import _approved_trim_review  # noqa: E402
from ldcommon import PipelineError  # noqa: E402
from fileutil import sha256_file  # noqa: E402


class CadenceAnalysisTests(unittest.TestCase):
    def test_parser_requires_complete_contiguous_zero_based_log_coverage(self):
        text = "\n".join(f"{i} {('abcde'[i % 5])} [{20 if i % 5 else 90}]"
                         for i in range(25))
        rows = parse_tfm_log(text, expected_frames=25)
        self.assertEqual(rows[0]["frame"], 0)
        self.assertIsNone(rows[0]["flag"])
        self.assertEqual(len(rows), 25)
        with self.assertRaisesRegex(ValueError, "expected exactly 25"):
            parse_tfm_log("\n".join(f"{i} c [0]" for i in range(24)), expected_frames=25)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            parse_tfm_log("0 c [0]\n2 c [0]")

    def test_analysis_emits_machine_readable_metrics_and_flagged_intervals(self):
        rows = []
        for frame in range(1100):
            code = "abcde"[frame % 5]
            mic = 90 if 100 <= frame <= 205 else 10
            if 300 <= frame <= 302:
                code = "u"
            rows.append({"frame": frame, "match_code": code, "flag": None, "mic": mic})
        motion = [0.0] * 1099
        motion[400:950] = [20.0] * 550
        summary = analyze_tfm_rows(rows, motion_values=motion)
        self.assertEqual(summary["frame_count"], 1100)
        self.assertGreater(summary["unmatched_count"], 0)
        self.assertGreater(summary["combed_count"], 0)
        self.assertGreater(summary["insert_count"], 0)
        self.assertEqual(len(summary["windows_30s"]), 2)
        self.assertTrue(any("unmatched-frames" in row["reasons"]
                            for row in summary["review_intervals"]))
        self.assertTrue(summary["visual_approval_required"])
        self.assertGreater(summary["windows_30s"][0]["motion_p90"], 0)

    def test_motion_trace_parser_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "motion.txt"
            path.write_text("0.0\n15.5\n", encoding="utf-8")
            self.assertEqual(load_motion_trace(str(path)), [0.0, 15.5])
            path.write_text("0.0\nnoise\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-numeric"):
                load_motion_trace(str(path))

    def test_visual_asset_selection_retains_flagged_and_clean_intervals(self):
        selected = review_assets._selected_frames({
            "review_intervals": [
                {"start_frame": 100, "end_frame_inclusive": 190,
                 "start_s": 3.3, "end_s": 6.3, "reasons": ["combed frames"]},
                {"start_frame": 500, "end_frame_inclusive": 650,
                 "start_s": 16.7, "end_s": 21.7, "reasons": ["period-5 deviations"]},
            ],
            "clean_samples": [{
                "start_frame": 800, "end_frame_inclusive": 1700,
                "start_s": 26.7, "end_s": 56.7, "reasons": ["representative clean interval"],
            }],
        }, 32)
        self.assertEqual(len(selected), 3)
        self.assertEqual([row["sample_kind"] for row in selected],
                         ["review-required", "review-required", "representative-clean"])
        self.assertEqual(selected[1]["center_frame"], 575)

    def test_cadence_gate_requires_hash_bound_visual_trim_review_not_just_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master, native = root / "master.mov", root / "native.wav"
            master.write_bytes(b"master")
            native.write_bytes(b"native")
            review = {
                "status": "APPROVED", "head_trim": 12, "tail_trim": 4,
                "frame_domain": "23.976 output frames; values entered after visual adjudication",
                "audio_pad_frames": 3, "stack_master_sha256": sha256_file(master),
                "native_audio_sha256": sha256_file(native), "review_note": "visual review",
                "capture": "P1", "efm_provenance_sha256": "provenance",
            }
            ep = SimpleNamespace(
                data={"video": {"head_trim": 12, "tail_trim": 4, "trim_review": review},
                      "audio": {"pad_frames": 3, "capture": "P1",
                                "efm_provenance": {"P1": [{"report_sha256": "provenance"}]}},
                      "dvd_reference_available": False, "no_dvd_proceed_approved": True},
                path=lambda _key: None,
            )
            self.assertEqual(_approved_trim_review(ep, master, native), review)
            ep.data["video"].pop("trim_review")
            with self.assertRaisesRegex(PipelineError, "visual trim-review marker"):
                _approved_trim_review(ep, master, native)

    def test_tfm_generation_requires_full_null_decode_and_contiguous_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master, native, manifest = root / "master.mov", root / "native.wav", root / "manifest.json"
            master.write_bytes(b"master")
            native.write_bytes(b"native")
            manifest.write_text("{}\n", encoding="utf-8")
            trim_review = {
                "status": "APPROVED", "head_trim": 2, "tail_trim": 1,
                "frame_domain": "23.976 output frames; values entered after visual adjudication",
                "audio_pad_frames": 2, "stack_master_sha256": sha256_file(master),
                "native_audio_sha256": sha256_file(native), "capture": "P1",
                "efm_provenance_sha256": "provenance", "review_note": "reviewed trims",
            }

            class Episode:
                code, series, identity = "S01E12", "DS9", "DS9:S01E12"
                data = {
                    "video": {"head_trim": 2, "tail_trim": 1, "trim_review": trim_review},
                    "audio": {"capture": "P1", "pad_frames": 2,
                              "efm_provenance_required": True,
                              "efm_provenance": {"P1": [{"report_sha256": "provenance"}]},
                              "capture_selection": {"capture": "P1"}},
                    "captures": [{"label": "P1"}],
                    "stack": {"frames": 10},
                    "dvd_reference_available": False,
                    "no_dvd_proceed_approved": True,
                }
                def __init__(self, ld):
                    self.ld = ld
                def path(self, key):
                    return {"stack_master": master, "ld_native": native}[key]

            class Tools:
                profile = {}
                @staticmethod
                def ffprobe():
                    return "ffprobe"
                @staticmethod
                def ffmpeg():
                    return "ffmpeg"

            for decoded_frames, expected_status in ((8, "VERIFIED"), (7, "INCOMPLETE")):
                avs, review_avs = root / f"tfm-{decoded_frames}.avs", root / f"review-{decoded_frames}.avs"
                log = root / f"tfm-{decoded_frames}.log"
                decode_log = root / f"decode-{decoded_frames}.log"
                report = root / f"report-{decoded_frames}.json"

                def fake_run(command, **_kwargs):
                    if command[0] == "ffprobe":
                        return SimpleNamespace(
                            returncode=0, stdout=json.dumps({"streams": [{"nb_read_frames": "10"}]}),
                            stderr="")
                    log.write_text("\n".join(
                        f"{i} {'abcde'[i % 5]} [10]" for i in range(8)) + "\n", encoding="utf-8")
                    return SimpleNamespace(
                        returncode=0, stdout=f"frame={decoded_frames}\nprogress=end\n", stderr="")

                with patch.object(generator, "episode_manifest_path", return_value=manifest), \
                        patch.object(generator, "assert_writable_path",
                                     side_effect=lambda path, _ld: Path(path)), \
                        patch.object(generator, "validate_efm_capture_provenance"), \
                        patch.object(generator.subprocess, "run", side_effect=fake_run):
                    if expected_status == "VERIFIED":
                        result = generator.generate(
                            Episode(Tools()), Tools(), avs_out=avs, review_avs_out=review_avs,
                            tfm_log_out=log, decode_log_out=decode_log, report_out=report)
                        self.assertEqual(result, report)
                    else:
                        with self.assertRaisesRegex(PipelineError, "incomplete"):
                            generator.generate(
                                Episode(Tools()), Tools(), avs_out=avs, review_avs_out=review_avs,
                                tfm_log_out=log, decode_log_out=decode_log, report_out=report)
                payload = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(payload["expected_tfm_frames"], 8)
                self.assertEqual(payload["tfm_rows"], 8)


if __name__ == "__main__":
    unittest.main()
