from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workflow_app_model import WorkflowAppError  # noqa: E402
from run_workflow import load_plan  # noqa: E402
from workflow_workbenches import (build_audio_rate_plan, build_cadence_map_plan,
                                  build_delivery_plan, build_motion_trace_plan,
                                  build_pad_audio_occupancy_plan)  # noqa: E402


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
        for key in ("ld_native", "dvd", "align_npy", "stack_project", "stack_master",
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
            "captures": [], "file_paths": files,
            "manifest_data": {
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "audio": {"pad_frames": 7},
                "video": {"head_trim": 2, "tail_trim": 0},
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

    def test_raw_provisional_requires_an_explicit_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(WorkflowAppError, "explicit non-negative"):
                build_audio_rate_plan(self._context(Path(temporary)), raw_provisional=True)

    def test_pad_occupancy_plan_is_report_only_and_unique(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = self._context(Path(temporary))
            first = build_pad_audio_occupancy_plan(context)
            second = build_pad_audio_occupancy_plan(context)
            self.assertEqual(first["steps"][0]["stage"], "measure-pad-audio")
            self.assertNotEqual(first["steps"][0]["outputs"], second["steps"][0]["outputs"])
            self.assertIn("--report-out", first["steps"][0]["args"])

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
            self.assertIn("DVD interval is measured shorter", boundary_policy)
            self.assertIn("do not execute", cadence["steps"][1]["prompt"])

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
