from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workflow_app_model import WorkflowAppError  # noqa: E402
from run_workflow import load_plan  # noqa: E402
from workflow_workbenches import (build_audio_rate_plan, build_cadence_map_plan,
                                  build_delivery_plan, build_motion_trace_plan,
                                   build_pad_audio_occupancy_plan,
                                   build_efm_decode_plan,
                                   build_audio_evidence_plan,
                                   build_alignment_samples_plan,
                                   build_video_trim_review_plan,
                                   build_cadence_evidence_plan,
                                   build_cadence_review_decision_plan,
                                   build_no_dvd_audio_promotion_plan,
                                   build_ld_synced_plan,
                                   AudioTimingEvidenceWindow,
                                   AlignmentSampleReviewWindow)  # noqa: E402


class WorkflowWorkbenchPlanTests(unittest.TestCase):
    def _context(self, root: Path) -> dict:
        instance = root / "devices" / "LENOVO-ABCD1234" / "episodes" / "S01E12"
        instance.mkdir(parents=True)
        probe = root / "probe"
        probe.mkdir()
        manifest = instance / "manifest.json"
        manifest.write_text("{}\n", encoding="utf-8")
        profile = root / "machine.json"
        profile.write_text("{}\n", encoding="utf-8")
        downloads = root / "downloads"
        downloads.mkdir()
        files = {}
        for key in ("ld_native", "ld_corrected", "ld_synced", "dvd", "align_npy",
                    "stack_project", "stack_master",
                    "avs_padfix", "padfixed_video", "pre_upscale", "avs_encode",
                    "tfm_log", "motion", "cadence_summary", "di_map"):
            path = root / f"{key}.dat"
            if key not in ("padfixed_video", "pre_upscale", "avs_encode", "cadence_summary", "di_map"):
                path.write_bytes(key.encode("ascii"))
            files[key] = str(path)
        return {
            "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            "title": "Vortex", "profile": profile, "manifest": manifest,
            "instance": instance, "probe": probe, "downloads": downloads,
            "work_root": root / "work",
            "captures": [], "file_paths": files,
            "manifest_data": {
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "audio": {"pad_frames": 7},
                "video": {"delta_s": 0.4, "head_trim": 2, "tail_trim": 0},
                "dvd_reference_available": True,
                "deliverable": "pre_upscale", "ld_audio_source": "ld_native",
                "mux_tracks": ["ld"],
                "files": {key: {"name": path.name, "root": "out"}
                          for key, path in ((key, Path(value)) for key, value in files.items())},
            },
            "platform": "windows",
        }

    def test_audio_rate_diagnostic_plan_is_report_only_and_input_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            plan = build_audio_rate_plan(context, raw_provisional=True, skip_s=0.45,
                                         points=[100.0, 700.0])
            step = plan["steps"][0]
            self.assertEqual(plan["identity"], "DS9:S01E12")
            self.assertEqual(step["stage"], "measure-audio-rate")
            self.assertIn("--raw-provisional", step["args"])
            self.assertIn("--skip-s", step["args"])
            self.assertEqual(len(step["outputs"]), 2)
            self.assertNotIn(str(context["manifest"]), step["outputs"])
            self.assertIn("report-only", plan["decisions"][1]["value"])

    def test_raw_provisional_can_use_manifest_geometry_without_a_user_skip_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = build_audio_rate_plan(self._context(Path(temporary)), raw_provisional=True)
            args = plan["steps"][0]["args"]
            self.assertIn("--raw-provisional", args)
            self.assertNotIn("--skip-s", args)

    def test_audio_evidence_bundle_runs_available_methods_without_promoting(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["audio"]["capture"] = "P1"
            context["manifest_data"]["video"]["delta_s"] = 0.4
            alignment = Path(context["file_paths"]["align_npy"])
            context["manifest_data"]["video"]["dvd_alignment_review"] = {
                "status": "APPROVED", "path": str(alignment),
                "sha256": hashlib.sha256(alignment.read_bytes()).hexdigest(),
            }
            plan = build_audio_evidence_plan(context)
            stages = [step["stage"] for step in plan["steps"]]
            self.assertEqual(stages, ["audio-diagnostic", "fade-midpoint-fit", "measure-audio-rate"])
            rate = next(step for step in plan["steps"] if step["stage"] == "measure-audio-rate")
            self.assertIn("--raw-provisional", rate["args"])
            self.assertNotIn("--skip-s", rate["args"])
            self.assertEqual(
                next(row["value"] for row in plan["decisions"]
                     if row["id"] == "dvd-backed-audio-promotion"),
                "report-only; no automatic rate/origin promotion")
            plan_path = Path(temporary) / "audio-evidence-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            loaded, _raw = load_plan(plan_path)
            self.assertEqual([step["stage"] for step in loaded["steps"]], stages)

    def test_alignment_sample_plan_binds_candidate_reports_and_is_manifest_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            report = Path(context["probe"]) / "fade-fit.json"
            report.write_text(json.dumps({
                "schema": "fade-midpoint-fit-v1", "episode": "S01E12",
            }), encoding="utf-8")
            plan = build_alignment_samples_plan(context, [report])
            step = plan["steps"][0]
            self.assertEqual(step["stage"], "alignment-samples")
            self.assertIn(str(report.resolve()), step["inputs"])
            self.assertIn(str(Path(context["file_paths"]["stack_master"]).resolve()), step["inputs"])
            self.assertIn(str(Path(context["file_paths"]["ld_native"]).resolve()), step["inputs"])
            self.assertIn("alignment-samples-", Path(step["outputs"][0]).name)
            self.assertTrue(any("no manifest" in row["value"]
                                for row in plan["decisions"]))
            self.assertNotIn(str(Path(context["manifest"]).resolve()), step["outputs"])

    def test_alignment_sample_plan_rejects_reports_outside_probe_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            report = Path(temporary) / "outside.json"
            report.write_text('{"schema":"fade-midpoint-fit-v1"}\n', encoding="utf-8")
            with self.assertRaisesRegex(WorkflowAppError, "probe folder"):
                build_alignment_samples_plan(context, [report])

    def test_alignment_sample_review_loads_only_ready_media_for_visual_audition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = root / "candidate.mp4"
            sample.write_bytes(b"preview")
            video, audio = root / "master.mkv", root / "native.wav"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            index = root / "index.json"
            index.write_text(json.dumps({
                "schema": "alignment-samples-v1",
                "candidate_is_approval": False,
                "warning": "Decision aids only",
                "video_source": str(video),
                "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "audio_source": str(audio),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "reports": [],
                "samples": [
                    {"id": "ready", "status": "READY", "path": str(sample),
                     "output_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
                     "label": "fit A"},
                    {"id": "deferred", "status": "DEFERRED", "path": str(root / "missing.mp4")},
                ],
                "deferred": [],
            }), encoding="utf-8")
            window = AlignmentSampleReviewWindow(object(), index)
            self.assertIsNone(window.error)
            self.assertEqual(list(window.samples), ["ready"])
            self.assertIn("Decision aids only", window.warning)
            sample.write_bytes(b"modified preview")
            changed = AlignmentSampleReviewWindow(object(), index)
            self.assertEqual(changed.samples, {})

    def test_trim_review_plan_records_manual_output_domain_values_without_conversion(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["dvd_reference_available"] = False
            context["manifest_data"]["no_dvd_proceed_approved"] = True
            context["manifest_data"]["audio"].update({
                "capture": "P1", "efm_provenance_required": True,
                "efm_provenance": {"P1": [{"report_sha256": "verified"}]},
                "capture_selection": {"capture": "P1"},
            })
            plan = build_video_trim_review_plan(
                context, 12, 4, "Reviewed LD opening and credits", replace_existing=True)
            step = plan["steps"][0]
            self.assertEqual(step["stage"], "record-video-trim-review")
            self.assertIn("--confirm-visual-review", step["args"])
            self.assertIn("output-frame", plan["decisions"][0]["value"])
            self.assertIn(str(Path(context["manifest"]).resolve()), step["outputs"])

    def test_cadence_evidence_plan_requires_exact_visual_trim_review_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["platform"] = "windows"
            context["manifest_data"]["audio"].update({
                "capture": "P1", "efm_provenance_required": True,
                "efm_provenance": {"P1": [{"report_sha256": "provenance"}]},
                "capture_selection": {
                    "capture": "P1", "verified_provenance_sha256": "provenance",
                    "alignment_map": None, "selection_note": "verified selected capture",
                },
            })
            context["manifest_data"]["stack"] = {"frames": 10000}
            context["manifest_data"]["video"].update({"head_trim": 12, "tail_trim": 4})
            master = Path(context["file_paths"]["stack_master"])
            native = Path(context["file_paths"]["ld_native"])
            context["manifest_data"]["video"]["trim_review"] = {
                "status": "APPROVED", "head_trim": 12, "tail_trim": 4,
                "frame_domain": "23.976 output frames; values entered after visual adjudication",
                "audio_pad_frames": 7,
                "capture": "P1", "efm_provenance_sha256": "provenance",
                "stack_master_sha256": hashlib.sha256(master.read_bytes()).hexdigest(),
                "native_audio_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
                "review_note": "visually adjudicated",
            }
            context["manifest_data"]["dvd_reference_available"] = False
            context["manifest_data"]["no_dvd_proceed_approved"] = True
            plan = build_cadence_evidence_plan(context)
            self.assertEqual([step["stage"] for step in plan["steps"]], [
                "cadence-evidence", "motion-trace", "build-map",
                "cadence-analysis", "cadence-review-assets",
            ])
            self.assertIn("--trim-head", plan["steps"][1]["args"])
            self.assertIn("-TrimStart", plan["steps"][2]["args"])
            self.assertFalse(any(step["stage"] == "encode-final" for step in plan["steps"]))
            saved = Path(temporary) / "cadence-evidence-plan.json"
            saved.write_text(json.dumps(plan), encoding="utf-8")
            loaded, _raw = load_plan(saved)
            self.assertEqual([step["stage"] for step in loaded["steps"]], [
                "cadence-evidence", "motion-trace", "build-map",
                "cadence-analysis", "cadence-review-assets",
            ])

            context["manifest_data"]["video"]["trim_review"]["tail_trim"] = 3
            with self.assertRaisesRegex(WorkflowAppError, "visual trim-review marker"):
                build_cadence_evidence_plan(context)

    def test_cadence_review_plan_requires_every_visual_interval_accepted_for_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["video"]["trim_review"] = {"status": "APPROVED"}
            index = Path(context["probe"]) / "cadence-review" / "index.json"
            index.parent.mkdir()
            metrics = index.parent / "metrics.json"
            coverage = index.parent / "coverage.json"
            metrics.write_text("{}", encoding="utf-8")
            coverage.write_text("{}", encoding="utf-8")
            index.write_text(json.dumps({
                "schema": "cadence-review-assets-v1", "identity": "DS9:S01E12",
                "candidate_is_approval": False,
                "manifest_sha256": hashlib.sha256(Path(context["manifest"]).read_bytes()).hexdigest(),
                "metrics_path": str(metrics), "verification_report_path": str(coverage),
                "review_intervals": [{"id": "r1"}, {"id": "r2"}],
            }), encoding="utf-8")

            class Ld:
                @staticmethod
                def root(_name):
                    return Path(temporary)

            context["ld"] = Ld()
            with patch("ldcommon.episode_manifest_path", return_value=Path(context["manifest"])):
                with self.assertRaisesRegex(WorkflowAppError, "every visual interval"):
                    build_cadence_review_decision_plan(
                        context, index, "APPROVED", {"r1": "accepted", "r2": "unresolved"},
                        "Reviewed cadence intervals",
                        hashlib.sha256(index.read_bytes()).hexdigest(),
                        source_rate_acknowledged=False)
                plan = build_cadence_review_decision_plan(
                    context, index, "APPROVED", {"r1": "accepted", "r2": "accepted"},
                    "Reviewed cadence intervals",
                    hashlib.sha256(index.read_bytes()).hexdigest(),
                    source_rate_acknowledged=False)
            self.assertEqual(plan["steps"][0]["stage"], "record-cadence-review")
            self.assertIn(str(Path(context["manifest"]).resolve()), plan["steps"][0]["outputs"])

    def test_audio_evidence_bundle_defers_missing_alignment_and_delta(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["audio"]["capture"] = "P1"
            context["manifest_data"]["video"]["delta_s"] = None
            Path(context["file_paths"]["align_npy"]).unlink()
            plan = build_audio_evidence_plan(context)
            self.assertEqual([step["stage"] for step in plan["steps"]], ["audio-diagnostic"])
            deferred = next(row["value"] for row in plan["decisions"]
                            if row["id"] == "audio-evidence-deferred")
            self.assertTrue(any("video.delta_s" in value for value in deferred))

    def test_audio_evidence_resolves_portable_probe_root_alignment_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["audio"].update({"capture": "P1"})
            alignment = Path(context["probe"]) / "approved-align.npy"
            alignment.write_bytes(b"approved alignment")
            context["file_paths"]["align_npy"] = str(alignment)
            context["manifest_data"]["video"]["dvd_alignment_review"] = {
                "status": "APPROVED",
                "path": "probe/approved-align.npy",
                "sha256": hashlib.sha256(alignment.read_bytes()).hexdigest(),
            }
            plan = build_audio_evidence_plan(context)
            self.assertIn("measure-audio-rate", [step["stage"] for step in plan["steps"]])

    def test_audio_evidence_window_summarizes_json_metrics_instead_of_showing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fade = root / "fade.json"
            fade.write_text(json.dumps({
                "schema": "fade-midpoint-fit-v1",
                "fit_A_master_vs_wav": {
                    "n": 7, "asetrate_from_b": 44055.0,
                    "asetrate_from_theil_sen": 44056.0,
                    "asetrate_stderr_hz": 1.2, "residual_sd_s": 0.08,
                },
                "fit_B_dvd_vs_wav": {"n": 2, "error": "too few pairs"},
            }), encoding="utf-8")
            duration = root / "duration.json"
            duration.write_text(json.dumps({
                "schema": "dvd-free-audio-timing-v1",
                "duration_candidate_effective_rate_hz": 44055.5,
                "duration_candidate_effective_rate_rounded_hz": 44055.5,
                "duration_delta_frames_stack_minus_audio": 0.2,
                "map_pad_count_full": 9,
            }), encoding="utf-8")
            context = {"identity": "DS9:S01E12", "manifest_data": {
                "audio": {"capture": "P1", "pad_frames": 7}}}
            window = AudioTimingEvidenceWindow(None, context, [fade, duration],
                                               ["DVD alignment not available"])
            self.assertEqual(len(window.rows), 3)
            self.assertIn("44055.00 Hz", window.rows[0][2])
            self.assertIn("too few pairs", window.rows[1][5])
            self.assertIn("candidate only", window.rows[2][5])
            self.assertEqual(window.deferred, ["DVD alignment not available"])

    def test_no_dvd_audio_promotion_plan_requires_explicit_choice_and_measured_endpoint_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["audio_root"] = Path(temporary) / "audio"
            context["audio_root"].mkdir()
            data = context["manifest_data"]
            data.update({"dvd_reference_available": False, "no_dvd_proceed_approved": True,
                         "deliverable_tracks": ["ld"]})
            data["audio"]["capture"] = "P1"
            data["video"].update({"delta_s": 0.4, "frames": 1000})
            plan = build_no_dvd_audio_promotion_plan(context)
            step = plan["steps"][0]
            self.assertEqual(step["stage"], "audio-promote-no-dvd")
            self.assertEqual(len(step["outputs"]), 4)
            self.assertIn("--confirm-manifest-sha256", step["args"])
            self.assertTrue(step["report_out"].endswith(".json"))
            plan_path = Path(temporary) / "no-dvd-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            loaded, _raw = load_plan(plan_path)
            self.assertEqual(loaded["steps"][0]["stage"], "audio-promote-no-dvd")
            data["no_dvd_proceed_approved"] = False
            with self.assertRaisesRegex(WorkflowAppError, "recorded no-DVD choice"):
                build_no_dvd_audio_promotion_plan(context)

    def test_sample_sync_plan_requires_approved_origin_and_uses_new_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["file_paths"]["ld_synced"] = str(Path(temporary) / "new-synced.flac")
            context["manifest_data"]["audio"].update(
                {"capture": "P1", "pad_frames": 7, "skip_s": 0.3, "asetrate": 44056})
            context["manifest_data"]["video"].update({"frames": 1000, "duration_s": 41.708})
            plan = build_ld_synced_plan(context)
            step = plan["steps"][0]
            self.assertEqual(step["stage"], "build-ld-synced")
            self.assertEqual(plan["decisions"][0]["id"], "approved-audio-skip-s")
            self.assertEqual(step["outputs"], [context["file_paths"]["ld_synced"]])
            context["manifest_data"]["audio"]["skip_s"] = None
            with self.assertRaisesRegex(WorkflowAppError, "human-approved measured audio.skip_s"):
                build_ld_synced_plan(context)

    def test_pad_occupancy_plan_is_report_only_and_unique(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            first = build_pad_audio_occupancy_plan(context)
            second = build_pad_audio_occupancy_plan(context)
            self.assertEqual(first["steps"][0]["stage"], "measure-pad-audio")
            self.assertNotEqual(first["steps"][0]["outputs"], second["steps"][0]["outputs"])
            self.assertIn("--report-out", first["steps"][0]["args"])

    def test_efm_decode_plan_is_allowlisted_input_bound_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            captures = []
            for label in ("L1", "P1"):
                tbc_name = f"{label}.tbc"
                json_name = tbc_name + ".json"
                efm_name = f"{label}.efm.7z"
                for name in (tbc_name, json_name, efm_name):
                    (context["downloads"] / name).write_bytes(name.encode())
                captures.append({"label": label, "tbc_file": tbc_name,
                                 "json_file": json_name, "efm_file": efm_name})
            context["captures"] = captures
            context["audio_root"] = Path(temporary) / "audio"
            context["audio_root"].mkdir()
            plan = build_efm_decode_plan(context, captures)
            step = plan["steps"][0]
            self.assertEqual(step["stage"], "decode-efm-captures")
            self.assertEqual(step["args"].count("--capture"), 2)
            self.assertIn(str(context["file_paths"]["stack_project"]), step["inputs"])
            self.assertTrue(any(path.endswith("L1.efm.7z") for path in step["inputs"]))
            self.assertIn(str(context["manifest"]), step["outputs"])
            self.assertTrue(any(path.endswith("P1-efm.wav") for path in step["outputs"]))
            self.assertEqual(sum(path.endswith(".orcprj") for path in step["outputs"]), 2)
            token = step["args"][step["args"].index("--run-token") + 1]
            self.assertTrue(any(path.endswith(f"L1-{token}.efm") for path in step["outputs"]))
            plan_path = Path(temporary) / "efm-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            loaded, _raw = load_plan(plan_path)
            self.assertEqual(loaded["steps"][0]["stage"], "decode-efm-captures")

    def test_efm_decode_plan_refuses_missing_source_and_existing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["audio_root"] = Path(temporary) / "audio"
            context["audio_root"].mkdir()
            capture = {"label": "L1", "tbc_file": "L1.tbc",
                       "json_file": "L1.tbc.json", "efm_file": "L1.efm"}
            for name in (capture["tbc_file"], capture["json_file"]):
                (context["downloads"] / name).write_bytes(b"source")
            with self.assertRaisesRegex(WorkflowAppError, "missing or unsafe"):
                build_efm_decode_plan(context, [capture])
            (context["downloads"] / capture["efm_file"]).write_bytes(b"source")
            wav = context["audio_root"] / "S01E12" / "L1-efm.wav"
            wav.parent.mkdir()
            wav.write_bytes(b"existing")
            with self.assertRaisesRegex(WorkflowAppError, "refusing to plan an EFM overwrite"):
                build_efm_decode_plan(context, [capture])

    def test_cadence_workbench_plans_motion_trace_then_measured_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            motion = build_motion_trace_plan(context)
            self.assertEqual(motion["steps"][0]["stage"], "motion-trace")
            self.assertEqual(motion["steps"][0]["args"][-2:], ["--trim-head", "7"])
            cadence = build_cadence_map_plan(
                context, context["file_paths"]["tfm_log"], context["file_paths"]["motion"])
            self.assertEqual(cadence["steps"][0]["stage"], "build-map")
            self.assertIn("-Log", cadence["steps"][0]["args"])
            self.assertIn("-Motion", cadence["steps"][0]["args"])
            self.assertEqual(cadence["steps"][1]["kind"], "review")
            boundary_policy = next(row["value"] for row in cadence["decisions"]
                                   if row["id"] == "dvd-boundary-policy")
            self.assertIn("visual and native-audio start/end", boundary_policy)
            self.assertIn("Keep the LD internal commercial-break timeline", boundary_policy)
            self.assertIn("not equal black durations", cadence["steps"][1]["prompt"])

    def test_cadence_plan_refuses_missing_measured_trim_or_motion_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["video"]["tail_trim"] = None
            with self.assertRaisesRegex(WorkflowAppError, "measured head_trim"):
                build_cadence_map_plan(context, context["file_paths"]["tfm_log"],
                                       context["file_paths"]["motion"])
            context["manifest_data"]["video"]["tail_trim"] = 0
            with self.assertRaisesRegex(WorkflowAppError, "measured motion trace"):
                build_cadence_map_plan(context, context["file_paths"]["tfm_log"], None)

    def test_cadence_review_only_applies_dvd_boundary_policy_when_dvd_is_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            context["manifest_data"]["dvd_reference_available"] = False
            plan = build_cadence_map_plan(
                context, context["file_paths"]["tfm_log"], context["file_paths"]["motion"])
            policy = next(row["value"] for row in plan["decisions"]
                          if row["id"] == "dvd-boundary-policy")
            self.assertIn("No DVD reference", policy)
            self.assertNotIn("independently map visual", policy)

    def test_delivery_builder_gates_encode_remux_and_qc_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            plan = build_delivery_plan(context, planned_encode_gib=25.0)
            stages = [step.get("stage") for step in plan["steps"] if step.get("stage")]
            self.assertIn("encode-validate", stages)
            self.assertIn("encode-final", stages)
            self.assertIn("validate-audio", stages)
            self.assertIn("remux", stages)
            self.assertIn("qc", stages)
            remux = next(step for step in plan["steps"] if step.get("stage") == "remux")
            self.assertIn(context["file_paths"]["padfixed_video"], remux["inputs"])
            reviews = [step for step in plan["steps"] if step.get("kind") == "review"]
            self.assertEqual([step["for_stage"] for step in reviews],
                             ["encode-validate", "validate-audio", "qc"])
            self.assertTrue(any("remux" in step["prompt"].lower() for step in reviews))
            encode = next(step for step in plan["steps"] if step.get("stage") == "encode-final")
            self.assertNotIn("--planned-gib", encode.get("args", []))

    def test_existing_deliverable_builds_qc_only_plan_without_encode_or_remux(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            Path(context["file_paths"]["pre_upscale"]).write_bytes(b"existing output")
            plan = build_delivery_plan(context, planned_encode_gib=None)
            stages = [step.get("stage") for step in plan["steps"] if step.get("stage")]
            self.assertNotIn("encode-final", stages)
            self.assertNotIn("remux", stages)
            self.assertEqual(stages, ["validate-deliverable", "qc"])
            self.assertEqual(plan["decisions"][0]["value"], "qc-only")

    def test_existing_encoded_video_builds_remux_qc_plan_with_pre_remux_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            Path(context["file_paths"]["padfixed_video"]).write_bytes(b"encoded video")
            plan = build_delivery_plan(context, planned_encode_gib=None)
            stages = [step.get("stage") for step in plan["steps"] if step.get("stage")]
            self.assertEqual(stages, ["validate-audio", "remux", "qc"])
            reviews = [step for step in plan["steps"] if step.get("kind") == "review"]
            self.assertEqual(reviews[0]["for_stage"], "validate-audio")
            self.assertIn("stream-copy remux only", reviews[0]["prompt"])

    def test_delivery_plan_passes_the_real_runner_plan_validator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self._context(root)
            plan = build_delivery_plan(context, planned_encode_gib=25.0)
            plan_path = root / "delivery-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            validated, _raw = load_plan(plan_path)
            self.assertEqual(validated["steps"][0]["stage"], "encode-validate")

            Path(context["file_paths"]["padfixed_video"]).write_bytes(b"encoded")
            remux_plan = build_delivery_plan(context, planned_encode_gib=None)
            remux_path = root / "remux-plan.json"
            remux_path.write_text(json.dumps(remux_plan, indent=2), encoding="utf-8")
            validated_remux, _raw = load_plan(remux_path)
            self.assertEqual(validated_remux["steps"][0]["stage"], "validate-audio")

            Path(context["file_paths"]["pre_upscale"]).write_bytes(b"delivered")
            qc_plan = build_delivery_plan(context, planned_encode_gib=None)
            qc_path = root / "qc-plan.json"
            qc_path.write_text(json.dumps(qc_plan, indent=2), encoding="utf-8")
            validated_qc, _raw = load_plan(qc_path)
            self.assertEqual(validated_qc["steps"][0]["stage"], "validate-deliverable")


if __name__ == "__main__":
    unittest.main()
