from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from ldcommon import PipelineError  # noqa: E402
from prepare_retime_breaks import (build_candidate_report, detect_dvd_silences,
                                   resolve_dvd_audio, threshold_intervals)  # noqa: E402
from measure_ld_black import descriptor_luma_means  # noqa: E402
from measure_ld_black import descriptor_luma_means  # noqa: E402
import prepare_retime_breaks  # noqa: E402
from record_retime_breaks import (LockError, _manifest_lock, apply_candidates)  # noqa: E402
import record_retime_breaks  # noqa: E402
from retime_break_candidates import (CANDIDATE_SCHEMA, candidate_break_entries,
                                     pair_ld_intervals, sha256_file)  # noqa: E402


def align_for(intervals: list[tuple[float, float]], offset: float = 0.0) -> np.ndarray:
    rows = []
    for start, end in intervals:
        lo = int(start * 30)
        hi = int(end * 30)
        rows.extend([[frame, offset] for frame in range(lo, hi, 10)])
    rows = sorted({int(row[0]): row for row in rows}.values(), key=lambda row: row[0])
    return np.asarray(rows, dtype=np.float64)


class CandidatePairingTests(unittest.TestCase):
    def test_luma_reduction_matches_float64_reference(self):
        descriptors = np.asarray([[0.25, 1.25, 2.25], [10.5, 11.5, 12.5]], dtype=np.float32)
        expected = np.asarray(descriptors, dtype=np.float64).mean(axis=1)
        actual = descriptor_luma_means(descriptors)
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(actual.dtype, np.float64)

    def test_descriptor_luma_means_preserve_float64_results_without_full_conversion(self):
        descriptors = np.asarray([[0.25, 1.25, 2.25], [10.5, 11.5, 12.5]], dtype=np.float32)
        expected = np.asarray(descriptors, dtype=np.float64).mean(axis=1)
        actual = descriptor_luma_means(descriptors)
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(actual.dtype, np.float64)

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

    def test_candidate_report_requires_matching_series_and_threshold(self):
        report = {"schema": "ld-pipeline-ld-black-intervals-v2", "status": "CANDIDATE_ONLY",
                  "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                  "thresholds_luma": {"10.0": [], "15.0": [{"start_s": 1.0, "end_s": 2.0}]}}
        self.assertEqual(threshold_intervals(report, 15.0)[0]["start_s"], 1.0)
        with self.assertRaisesRegex(PipelineError, "no unique interval list"):
            threshold_intervals(report, 12.0)

    def test_dvd_silence_probe_uses_explicit_selector_and_no_seek(self):
        class FakeLd:
            def ffmpeg(self):
                return "ffmpeg"

        completed = SimpleNamespace(returncode=0, stderr="silence_start: 1\nsilence_end: 2\n")
        with patch("prepare_retime_breaks.resolve_dvd_audio", return_value=("0:a:1", 3.0)), \
                patch("prepare_retime_breaks.subprocess.run", return_value=completed) as run:
            selector, duration, intervals = detect_dvd_silences(
                FakeLd(), Path("dvd.mkv"), -40.0, 0.2)
        args = run.call_args.args[0]
        self.assertNotIn("-ss", args)
        self.assertEqual(args[args.index("-map") + 1], "0:a:1")
        self.assertIn("silencedetect=noise=-40dB:d=0.200000", args)
        self.assertEqual((selector, duration, intervals), ("0:a:1", 3.0, [(1.0, 2.0)]))

    def test_dvd_silence_probe_propagates_ffmpeg_failure(self):
        class FakeLd:
            def ffmpeg(self):
                return "ffmpeg"

        failed = SimpleNamespace(returncode=1, stderr="simulated decoder error")
        with patch("prepare_retime_breaks.resolve_dvd_audio", return_value=("0:a:0", 3.0)), \
                patch("prepare_retime_breaks.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(PipelineError, "simulated decoder error"):
                detect_dvd_silences(FakeLd(), Path("dvd.mkv"), -40.0, 0.2)

    def test_multiple_5_1_streams_require_explicit_verified_selector(self):
        class FakeLd:
            def ffprobe(self):
                return "ffprobe"
        metadata = {"streams": [
            {"codec_type": "audio", "channels": 6, "channel_layout": "5.1(side)"},
            {"codec_type": "audio", "channels": 6, "channel_layout": "5.1"},
        ], "format": {"duration": "10.0"}}
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(metadata), stderr="")
        with patch("prepare_retime_breaks.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(PipelineError, "pass --dvd-audio-selector explicitly"):
                resolve_dvd_audio(FakeLd(), Path("dvd.mkv"))
            self.assertEqual(resolve_dvd_audio(FakeLd(), Path("dvd.mkv"), "0:a:1"),
                             ("0:a:1", 10.0))
            with self.assertRaisesRegex(PipelineError, "does not identify a six-channel"):
                resolve_dvd_audio(FakeLd(), Path("dvd.mkv"), "0:a:2")

    def test_candidate_report_binds_series_inputs_and_pair_parameters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe"
            probe.mkdir()
            ld_report_path = probe / "S01E12-ld-black-intervals.json"
            master = probe / "master.npy"
            alignment = probe / "align.npy"
            assets_index = probe / "alignment-review" / "index.json"
            dvd = root / "dvd.mkv"
            master_source = root / "master.mkv"
            manifest = root / "S01E12.json"
            master.write_bytes(b"master descriptor bytes")
            np.save(alignment, align_for([(10.0, 11.0)]))
            dvd.write_bytes(b"dvd")
            master_source.write_bytes(b"master video")
            assets_index.parent.mkdir()
            assets_index.write_text(json.dumps({
                "schema": "dvd-alignment-review-assets-v1", "identity": "DS9:S01E12",
                "candidate_is_approval": False, "alignment_sha256": sha256_file(alignment),
            }), encoding="utf-8")
            manifest.write_text(json.dumps({"episode": "S01E12"}), encoding="utf-8")
            ld_report_path.write_text(json.dumps({
                "schema": "ld-pipeline-ld-black-intervals-v2",
                "status": "CANDIDATE_ONLY", "episode": "S01E12", "series": "DS9",
                "identity": "DS9:S01E12",
                "minimum_run_duration_s": 0.3,
                "source_descriptors_sha256": sha256_file(master),
                "thresholds_luma": {"15.0": [{"label": "run1", "start_s": 10.0,
                                                "end_s": 11.0, "start_frame": 300,
                                                "end_frame_exclusive": 330}]},
            }), encoding="utf-8")

            class FakeEpisode:
                code = "S01E12"
                series = "DS9"
                identity = "DS9:S01E12"
                data = {"video": {"dvd_alignment_review": {
                    "status": "APPROVED", "path": "probe/align.npy",
                    "sha256": sha256_file(alignment),
                    "master_source_sha256": sha256_file(master_source),
                    "dvd_source_sha256": sha256_file(dvd),
                    "review_assets_path": "probe/alignment-review/index.json",
                    "review_assets_sha256": sha256_file(assets_index),
                }}}

                def path(self, key):
                    return {"align_npy": alignment, "dvd": dvd,
                            "master_descriptors": master, "stack_master": master_source}[key]

            FakeEpisode.ld = SimpleNamespace(probe=lambda: probe)

            with patch.object(prepare_retime_breaks, "episode_manifest_path",
                              return_value=manifest):
                report = build_candidate_report(
                    FakeEpisode(), ld_report_path,
                    json.loads(ld_report_path.read_text(encoding="utf-8")), 15.0,
                    [(10.0, 11.0)], "0:a:1", 12.0,
                    dvd_silence_noise_db=-40.0,
                    dvd_silence_minimum_duration_s=0.2,
                    pair_tolerance_s=1.0, max_offset_spread_frames=1.0)
            self.assertEqual(report["identity"], "DS9:S01E12")
            self.assertEqual(report["candidates"][0]["pair_status"], "unique-pair")
            self.assertEqual(report["parameters"]["dvd_silence_noise_db"], -40.0)
            self.assertEqual(report["inputs"]["alignment_sha256"], sha256_file(alignment))

    def test_prepare_cli_preserves_zero_candidate_report_for_in_app_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe"
            probe.mkdir()
            ld_report_path = probe / "S01E12-ld-black-intervals.json"
            master = probe / "master.npy"
            alignment = probe / "align.npy"
            assets_index = probe / "alignment-review" / "index.json"
            dvd = root / "dvd.mkv"
            master_source = root / "master.mkv"
            manifest = root / "S01E12.json"
            master.write_bytes(b"master descriptor bytes")
            np.save(alignment, align_for([(10.0, 11.0)]))
            dvd.write_bytes(b"dvd source")
            master_source.write_bytes(b"master video")
            assets_index.parent.mkdir()
            assets_index.write_text(json.dumps({
                "schema": "dvd-alignment-review-assets-v1", "identity": "DS9:S01E12",
                "candidate_is_approval": False, "alignment_sha256": sha256_file(alignment),
            }), encoding="utf-8")
            original = json.dumps({"episode": "S01E12"}).encode()
            manifest.write_bytes(original)
            ld_report_path.write_text(json.dumps({
                "schema": "ld-pipeline-ld-black-intervals-v2", "status": "CANDIDATE_ONLY",
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "source_descriptors_sha256": sha256_file(master),
                "thresholds_luma": {"15.0": []},
            }), encoding="utf-8")

            class FakeLd:
                profile = {"shared": {"enabled": False}}
                def probe(self): return probe

            class FakeEpisode:
                code, series, identity, ld = "S01E12", "DS9", "DS9:S01E12", None
                data = {"video": {"dvd_alignment_review": {
                    "status": "APPROVED", "path": "probe/align.npy",
                    "sha256": sha256_file(alignment),
                    "master_source_sha256": sha256_file(master_source),
                    "dvd_source_sha256": sha256_file(dvd),
                    "review_assets_path": "probe/alignment-review/index.json",
                    "review_assets_sha256": sha256_file(assets_index),
                }}}
                def path(self, key):
                    return {"align_npy": alignment, "dvd": dvd,
                            "master_descriptors": master, "stack_master": master_source}[key]

            FakeEpisode.ld = FakeLd()

            out = probe / "candidate-report.json"
            argv = ["prepare_retime_breaks.py", "--episode", "S01E12",
                    "--ld-black-report", str(ld_report_path), "--ld-threshold", "15",
                    "--dvd-silence-noise-db", "-40", "--dvd-silence-min-duration", "0.2",
                    "--pair-tolerance-s", "1", "--max-offset-spread-frames", "1",
                    "--report-out", str(out)]
            with patch.object(prepare_retime_breaks, "Ld", return_value=FakeLd()), \
                    patch.object(prepare_retime_breaks, "load_episode", return_value=FakeEpisode()), \
                    patch.object(prepare_retime_breaks, "episode_manifest_path", return_value=manifest), \
                    patch.object(prepare_retime_breaks, "assert_writable_path", side_effect=lambda p, ld: Path(p)), \
                    patch.object(prepare_retime_breaks, "detect_dvd_silences",
                                 return_value=("0:a:0", 10.0, [])), \
                    patch.object(sys, "argv", argv):
                self.assertEqual(prepare_retime_breaks.main(), 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "NO_CANDIDATES")
            self.assertEqual(report["candidates"], [])
            self.assertEqual(manifest.read_bytes(), original)

    def test_empty_candidate_set_is_not_reported_as_success(self):
        from prepare_retime_breaks import build_candidate_report
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe"
            probe.mkdir()
            ld_report_path = probe / "ld-report.json"
            master = probe / "master.npy"
            alignment = probe / "align.npy"
            assets_index = probe / "alignment-review" / "index.json"
            dvd = root / "dvd.mkv"
            master_source = root / "master.mkv"
            manifest = root / "S01E12.json"
            master.write_bytes(b"master")
            ld_report_path.write_text("ld report", encoding="utf-8")
            np.save(alignment, align_for([(1.0, 2.0)]))
            dvd.write_bytes(b"dvd")
            master_source.write_bytes(b"master video")
            assets_index.parent.mkdir()
            assets_index.write_text(json.dumps({
                "schema": "dvd-alignment-review-assets-v1", "identity": "DS9:S01E12",
                "candidate_is_approval": False, "alignment_sha256": sha256_file(alignment),
            }), encoding="utf-8")
            manifest.write_text("{}", encoding="utf-8")
            class FakeEpisode:
                code, series, identity = "S01E12", "DS9", "DS9:S01E12"
                data = {"video": {"dvd_alignment_review": {
                    "status": "APPROVED", "path": "probe/align.npy",
                    "sha256": sha256_file(alignment),
                    "master_source_sha256": sha256_file(master_source),
                    "dvd_source_sha256": sha256_file(dvd),
                    "review_assets_path": "probe/alignment-review/index.json",
                    "review_assets_sha256": sha256_file(assets_index),
                }}}
                def path(self, key):
                    return {"align_npy": alignment, "dvd": dvd,
                            "master_descriptors": master, "stack_master": master_source}[key]
            FakeEpisode.ld = SimpleNamespace(probe=lambda: probe)
            with patch.object(prepare_retime_breaks, "episode_manifest_path", return_value=manifest):
                report = build_candidate_report(
                    FakeEpisode(), ld_report_path,
                    {"schema": "ld-pipeline-ld-black-intervals-v2",
                     "status": "CANDIDATE_ONLY", "episode": "S01E12", "series": "DS9",
                     "identity": "DS9:S01E12", "thresholds_luma": {"15.0": []}},
                    15, [], "0:a:0", 10,
                    dvd_silence_noise_db=-40, dvd_silence_minimum_duration_s=0.2,
                    pair_tolerance_s=1, max_offset_spread_frames=1)
            self.assertEqual(report["status"], "NO_CANDIDATES")
            self.assertEqual(report["candidates"], [])


def fixture_report(root: Path) -> tuple[dict, Path, Path, Path, Path]:
    probe = root / "probe"
    probe.mkdir()
    manifest = root / "S01E12.json"
    manifest_payload = {"episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                        "retime": {"breaks": [], "merged_music_through": []}}
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    ld_report = probe / "S01E12-ld-black-intervals.json"
    ld_report.write_text("ld-report\n", encoding="utf-8")
    alignment = probe / "align.npy"
    alignment.write_bytes(b"alignment evidence")
    dvd = root / "dvd.mkv"
    dvd.write_bytes(b"dvd source")
    report = {
        "schema": CANDIDATE_SCHEMA,
        "identity": "DS9:S01E12", "series": "DS9", "episode": "S01E12",
        "status": "REPORT_ONLY", "manifest_sha256": sha256_file(manifest),
        "inputs": {"ld_black_report_relative_path": ld_report.name,
                   "ld_black_report_sha256": sha256_file(ld_report),
                   "alignment_sha256": sha256_file(alignment),
                   "dvd_name": dvd.name, "dvd_size_bytes": dvd.stat().st_size,
                   "dvd_mtime_ns": dvd.stat().st_mtime_ns, "dvd_duration_s": 120.0},
        "parameters": {"ld_luma_threshold": 15.0},
        "candidates": [{"label": "B1", "pair_status": "unique-pair",
                        "ld_black": [10.0, 11.0],
                        "pair_candidates": [{"id": "D1", "start_s": 10.0, "end_s": 11.0}]},
                       {"label": "B2", "pair_status": "no-dvd-silence",
                        "ld_black": [20.0, 21.0], "pair_candidates": []}],
    }
    candidate_path = probe / "candidate.json"
    candidate_path.write_text(json.dumps(report), encoding="utf-8")
    return report, candidate_path, manifest, alignment, dvd


class CandidateRecordSafetyTests(unittest.TestCase):
    def _mock_cli(self, root: Path, args: list[str]):
        report, report_path, manifest, alignment, dvd = fixture_report(root)
        fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                  probe=lambda: root / "probe")
        fake_ep = SimpleNamespace(code="S01E12", series="DS9", identity="DS9:S01E12",
                                  path=lambda key: alignment if key == "align_npy" else dvd)
        return report, report_path, manifest, fake_ld, fake_ep, args

    def test_recorder_default_cli_is_dry_and_apply_requires_report_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, report_path, manifest, fake_ld, fake_ep, _ = self._mock_cli(root, [])
            original = manifest.read_bytes()
            with patch.object(record_retime_breaks, "Ld", return_value=fake_ld), \
                    patch.object(record_retime_breaks, "load_episode", return_value=fake_ep), \
                    patch.object(record_retime_breaks, "episode_manifest_path", return_value=manifest), \
                    patch.object(sys, "argv", ["record_retime_breaks.py", "--episode", "DS9:S01E12",
                                                "--candidate-report", str(report_path)]):
                self.assertEqual(record_retime_breaks.main(), 0)
            self.assertEqual(manifest.read_bytes(), original)
            with patch.object(record_retime_breaks, "Ld", return_value=fake_ld), \
                    patch.object(record_retime_breaks, "load_episode", return_value=fake_ep), \
                    patch.object(record_retime_breaks, "episode_manifest_path", return_value=manifest), \
                    patch.object(sys, "argv", ["record_retime_breaks.py", "--episode", "DS9:S01E12",
                                                "--candidate-report", str(report_path), "--select", "B1",
                                                "--apply", "--confirm-review", "DS9:S01E12",
                                                "--review-note", "reviewed candidate evidence"]):
                self.assertEqual(record_retime_breaks.main(), 2)
            self.assertEqual(manifest.read_bytes(), original)

    def test_recorder_cli_apply_requires_input_gate_and_writes_reviewed_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _report, report_path, manifest, fake_ld, fake_ep, _ = self._mock_cli(root, [])
            report_hash = sha256_file(report_path)
            completed = SimpleNamespace(returncode=0, stdout="inputs PASS\n", stderr="")
            with patch.object(record_retime_breaks, "Ld", return_value=fake_ld), \
                    patch.object(record_retime_breaks, "load_episode", return_value=fake_ep), \
                    patch.object(record_retime_breaks, "episode_manifest_path", return_value=manifest), \
                    patch.object(record_retime_breaks, "assert_writable_path", side_effect=lambda path, ld: Path(path)), \
                    patch.object(record_retime_breaks.subprocess, "run", return_value=completed) as run, \
                    patch.object(sys, "argv", ["record_retime_breaks.py", "--episode", "DS9:S01E12",
                                                "--candidate-report", str(report_path), "--select", "B1",
                                                "--apply", "--confirm-review", "DS9:S01E12",
                                                "--confirm-report-sha256", report_hash,
                                                "--review-note", "reviewed candidate images and match"]):
                self.assertEqual(record_retime_breaks.main(), 0)
            self.assertEqual([call.args[0][-1] for call in run.call_args_list],
                             ["inputs", "manifest"])
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(saved["retime"]["breaks"][0]["label"], "B1")
            self.assertIsNone(saved["retime"]["breaks"][0]["off_before"])
            self.assertTrue(list(root.glob("S01E12.json.bak-retime-breaks-*")))

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

    def test_apply_creates_verified_backup_and_adds_candidates_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, report_path, manifest, alignment, dvd = fixture_report(root)
            original_manifest = manifest.read_bytes()
            ld_report = root / "probe" / report["inputs"]["ld_black_report_relative_path"]
            fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                      probe=lambda: root / "probe")
            fake_ep = SimpleNamespace(code="S01E12", series="DS9", identity="DS9:S01E12",
                                      path=lambda key: alignment if key == "align_npy" else dvd)
            with patch("record_retime_breaks.assert_writable_path", side_effect=lambda path, ld: Path(path)):
                backup, target, entries = apply_candidates(
                    report, report_path, fake_ep, fake_ld, manifest, ["B1"], [],
                    "reviewed paired images and thresholds", sha256_file(report_path))
            self.assertEqual(target, manifest)
            self.assertEqual(backup.read_bytes(), original_manifest)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 1)
            self.assertEqual(data["retime"]["breaks"][0]["label"], "B1")
            self.assertIsNone(data["retime"]["breaks"][0]["off_before"])
            self.assertNotIn("B1", data["retime"]["merged_music_through"])

    def test_apply_records_merged_through_as_interval_objects_not_labels_or_breaks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, report_path, manifest, alignment, dvd = fixture_report(root)
            fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                      probe=lambda: root / "probe")
            fake_ep = SimpleNamespace(code="S01E12", series="DS9", identity="DS9:S01E12",
                                      path=lambda key: alignment if key == "align_npy" else dvd)
            with patch("record_retime_breaks.assert_writable_path", side_effect=lambda path, ld: Path(path)):
                _backup, _target, entries = apply_candidates(
                    report, report_path, fake_ep, fake_ld, manifest, [], ["B2"],
                    "reviewed music-through evidence", sha256_file(report_path))
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(entries, [])
            self.assertEqual(saved["retime"]["breaks"], [])
            merged = saved["retime"]["merged_music_through"]
            self.assertEqual(len(merged), 1)
            self.assertIsInstance(merged[0], dict)
            self.assertEqual(merged[0]["label"], "B2")
            self.assertEqual(merged[0]["ld_black"], [20.0, 21.0])
            self.assertIsNone(merged[0]["step"])

    def test_atomic_replace_failure_keeps_original_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, report_path, manifest, alignment, dvd = fixture_report(root)
            original = manifest.read_bytes()
            fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                      probe=lambda: root / "probe")
            fake_ep = SimpleNamespace(code="S01E12", series="DS9", identity="DS9:S01E12",
                                      path=lambda key: alignment if key == "align_npy" else dvd)
            with patch("record_retime_breaks.assert_writable_path", side_effect=lambda path, ld: Path(path)), \
                    patch("record_retime_breaks.os.replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    apply_candidates(report, report_path, fake_ep, fake_ld, manifest,
                                     ["B1"], [], "reviewed paired interval", sha256_file(report_path))
            self.assertEqual(manifest.read_bytes(), original)
            backups = list(root.glob("S01E12.json.bak-retime-breaks-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_stale_manifest_and_existing_breaks_refuse_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, report_path, manifest, alignment, dvd = fixture_report(root)
            manifest.write_text(json.dumps({"changed": True}), encoding="utf-8")
            fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                      probe=lambda: root / "probe")
            fake_ep = SimpleNamespace(code="S01E12", series="DS9", identity="DS9:S01E12",
                                      path=lambda key: alignment if key == "align_npy" else dvd)
            with patch("record_retime_breaks.assert_writable_path", side_effect=lambda path, ld: Path(path)):
                with self.assertRaisesRegex(PipelineError, "manifest changed since the candidate report"):
                    apply_candidates(report, report_path, fake_ep, fake_ld, manifest,
                                     ["B1"], [], "reviewed", sha256_file(report_path))

    def test_manifest_symlink_is_refused_before_backup_or_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, report_path, manifest, alignment, dvd = fixture_report(root)
            link = root / "manifest-link.json"
            try:
                link.symlink_to(manifest)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                      probe=lambda: root / "probe")
            fake_ep = SimpleNamespace(code="S01E12", series="DS9", identity="DS9:S01E12",
                                      path=lambda key: alignment if key == "align_npy" else dvd)
            with patch("record_retime_breaks.assert_writable_path", side_effect=lambda path, ld: Path(path)):
                with self.assertRaisesRegex(PipelineError, "missing/symlink manifest"):
                    apply_candidates(report, report_path, fake_ep, fake_ld, link, ["B1"], [],
                                     "reviewed", sha256_file(report_path))

    def test_apply_lock_refuses_a_second_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "manifest.json.retime-candidates.lock"
            with _manifest_lock(lock):
                with self.assertRaises(FileExistsError):
                    with _manifest_lock(lock):
                        self.fail("second candidate writer acquired the lock")


if __name__ == "__main__":
    unittest.main()
