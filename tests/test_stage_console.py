from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_workflow import (_fingerprint_payload, _fresh_state, _plan_digest,
                          _profile_digest, file_fingerprints, load_plan)  # noqa: E402
from workflow_app_model import (WorkflowAppError, build_stack_console_plan,
                                build_orc_reference_measurement_plan,
                                inspect_stack_console)  # noqa: E402


class StageConsolePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle_patch = patch("ldcommon.BUNDLE", self.root)
        self.bundle_patch.start()
        self.downloads = self.root / "captures"
        self.probe = self.root / "probe"
        self.work = self.root / "work"
        self.logs = self.root / "logs"
        for directory in (self.downloads, self.probe, self.work, self.logs):
            directory.mkdir()
        self.project = self.work / "S01E07-stack.orcprj"
        self.master = self.work / "S01E07-stack.mov"
        self.profile = self.root / "machine.json"
        self.profile.write_text(json.dumps({
            "machine": "TEST", "os": "windows", "shared": {"enabled": False},
            "roots": {"downloads_root": str(self.root),
                      "downloads_template": "unused-{episode}",
                      "probe": str(self.probe), "work": str(self.work),
                      "logs": str(self.logs)},
            "tools": {},
        }), encoding="utf-8")
        self.manifest = self.root / "S01E07.json"
        self.write_manifest()

    def tearDown(self):
        self.bundle_patch.stop()
        self.temp.cleanup()

    def write_manifest(self, *, reference="", alignment_map="", stack_frames=None,
                       series="DS9"):
        data = {
            "episode": "S01E07", "series": series, "identity": f"{series}:S01E07",
            "title": "Q-Less", "record_status": "draft",
            "format": "one_part", "captures": [{"label": "L1"}, {"label": "M1"}],
            "reference": reference, "alignment_map": alignment_map,
            "stack": {"frames": stack_frames},
            "dvd_reference_available": False, "no_dvd_proceed_approved": True,
            "cut_pad_images_choice": "skip",
            "roots": {"downloads": str(self.downloads)},
            "files": {"stack_project": {"path": str(self.project)},
                      "stack_master": {"path": str(self.master)}},
        }
        self.manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def make_completed_reference_instance(self, *, alignment_map="", stack_frames=None):
        instance = self.root / "episode-instance"
        plans, logs = instance / "plans", instance / "logs"
        plans.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        self.write_manifest(reference="L1", alignment_map=alignment_map,
                            stack_frames=stack_frames)
        context = inspect_stack_console(
            self.profile, self.manifest, "S01E07", instance_root_value=instance)
        plan = build_stack_console_plan(context)
        plan_path = plans / "reference-plan.json"
        raw_plan = (json.dumps(plan, indent=2) + "\n").encode("utf-8")
        plan_path.write_bytes(raw_plan)
        validate, select = plan["steps"]
        self.project.write_text("verified reference project", encoding="utf-8")
        report = Path(select["outputs"][2])
        report.write_text("verified reference report", encoding="utf-8")
        state = _fresh_state(plan, _plan_digest(raw_plan), _profile_digest(self.profile))
        state["status"] = "PASS"
        state["steps"][0].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(validate["inputs"])),
            "output_fingerprints": [], "finished_utc": "2026-11-17T20:00:00+00:00",
        })
        state["steps"][1].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(select["inputs"])),
            "output_fingerprints": _fingerprint_payload(file_fingerprints(select["outputs"])),
            "finished_utc": "2026-11-17T20:01:00+00:00",
        })
        (logs / f"workflow-{plan['job_id']}.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return instance, plan

    def test_reference_phase_plan_is_bounded_validated_without_report_review(self):
        before = self.manifest.read_bytes()
        context = inspect_stack_console(self.profile, self.manifest, "S01E07")
        self.assertEqual(context["phase"], "reference")
        plan = build_stack_console_plan(context)
        path = self.root / "reference-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        loaded, _ = load_plan(path)
        self.assertEqual([step.get("stage", step.get("id")) for step in loaded["steps"]],
                         ["validate-inputs", "stack-reference"])
        reference = loaded["steps"][1]
        self.assertEqual(reference["args"][0], "--report-out")
        self.assertEqual(reference["outputs"], [str(self.manifest), str(self.project),
                                                  reference["args"][1]])
        self.assertEqual(Path(reference["args"][1]).parent, self.probe)
        self.assertIn("reference-selection", Path(reference["args"][1]).name)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_voy_stage_console_uses_series_scoped_identity_and_plan(self):
        voy_probe = self.probe / "VOY"
        voy_probe.mkdir()
        self.write_manifest(series="VOY")
        context = inspect_stack_console(self.profile, self.manifest, "S01E07", series="VOY")
        self.assertEqual((context["series"], context["identity"]), ("VOY", "VOY:S01E07"))
        plan = build_stack_console_plan(context)
        self.assertEqual((plan["series"], plan["identity"]), ("VOY", "VOY:S01E07"))
        self.assertTrue(plan["job_id"].startswith("stack-reference-voy-s01e07-"))
        path = self.root / "voy-reference-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        loaded, _ = load_plan(path)
        self.assertEqual((loaded["series"], loaded["identity"]), ("VOY", "VOY:S01E07"))

    def test_render_phase_uses_measured_frames_for_automatic_space_estimate(self):
        instance, _plan = self.make_completed_reference_instance(
            alignment_map="1+2", stack_frames=83230)
        context = inspect_stack_console(self.profile, self.manifest, "S01E07", instance)
        self.assertEqual(context["phase"], "render")
        automatic_plan = build_stack_console_plan(context)
        automatic_render = next(step for step in automatic_plan["steps"]
                                if step.get("stage") == "render-stack")
        automatic_budget = float(automatic_render["args"][
            automatic_render["args"].index("--planned-gib") + 1])
        self.assertGreater(automatic_budget, 0)
        plan = build_stack_console_plan(context, planned_gib=26.5)
        path = self.root / "render-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        loaded, _ = load_plan(path)
        render = next(step for step in loaded["steps"] if step.get("stage") == "render-stack")
        self.assertEqual(render["args"][render["args"].index("--planned-gib") + 1], "26.500000")
        self.assertEqual(loaded["steps"][-1]["kind"], "review")

    def test_render_phase_waits_for_stack_frame_measurement(self):
        instance, _plan = self.make_completed_reference_instance(
            alignment_map="1+2", stack_frames=None)
        context = inspect_stack_console(self.profile, self.manifest, "S01E07", instance)
        self.assertEqual(context["phase"], "reference-complete")
        self.assertTrue(any("stack.frames" in row for row in context["measurement_requirements"]))

    def test_render_phase_waits_for_measured_alignment_map(self):
        instance, _plan = self.make_completed_reference_instance(
            alignment_map="", stack_frames=83230)
        context = inspect_stack_console(self.profile, self.manifest, "S01E07", instance)
        self.assertEqual(context["phase"], "reference-complete")
        self.assertTrue(any("alignment_map" in row for row in context["measurement_requirements"]))

    def test_recorded_orc_measurement_step_preserves_verified_reference_recovery(self):
        instance, reference_plan = self.make_completed_reference_instance(
            alignment_map="", stack_frames=None)
        selection_step = next(step for step in reference_plan["steps"]
                              if step.get("stage") == "stack-reference")
        report = Path(selection_step["outputs"][2])
        report.write_text(
            "- Status: **SELECTED**\n"
            "| Pass | Sources | Input order | Suggested reference | Runner-up | Stack sigma | Coverage 1/2/3+ | Fit trusted | Saved/completed |\n"
            "|---:|---:|---|---|---|---:|---|---|---|\n"
            "| 1 | 2 | `L1, M1` | `L1` (input 1) | `M1` | 1.2 | 5/10/100 | True | True/True |\n"
            "Measured alignment map: `1+0, 2+5`\n", encoding="utf-8")
        reference_state_path = instance / "logs" / f"workflow-{reference_plan['job_id']}.json"
        reference_state = json.loads(reference_state_path.read_text(encoding="utf-8"))
        reference_state["steps"][1]["output_fingerprints"] = _fingerprint_payload(
            file_fingerprints(reference_plan["steps"][1]["outputs"]))
        reference_state_path.write_text(json.dumps(reference_state, indent=2) + "\n",
                                        encoding="utf-8")
        context = inspect_stack_console(self.profile, self.manifest, "S01E07", instance)
        measurement_plan = build_orc_reference_measurement_plan(context)
        plan_path = instance / "plans" / "record-measurements-plan.json"
        raw_plan = (json.dumps(measurement_plan, indent=2) + "\n").encode("utf-8")
        plan_path.write_bytes(raw_plan)

        manifest_data = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest_data["alignment_map"] = "1+0, 2+5"
        manifest_data["stack"]["frames"] = 115
        self.manifest.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
        step = measurement_plan["steps"][0]
        state = _fresh_state(measurement_plan, _plan_digest(raw_plan), _profile_digest(self.profile))
        state["status"] = "PASS"
        state["steps"][0].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(step["inputs"])),
            "output_fingerprints": _fingerprint_payload(file_fingerprints(step["outputs"])),
            "finished_utc": "2026-11-17T20:03:00+00:00",
        })
        logs = instance / "logs"
        (logs / f"workflow-{measurement_plan['job_id']}.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8")

        resumed = inspect_stack_console(self.profile, self.manifest, "S01E07", instance)

        self.assertEqual(resumed["phase"], "render")
        self.assertEqual(resumed["completed_stage"]["stage"], "stack-reference")
        self.assertEqual(resumed["completed_stage"]["step_id"], "record-orc-reference-measurements")
        self.assertEqual(resumed["reference_report"], report)
        self.assertEqual(resumed["stack_frames"], 115)

    def test_verified_existing_reference_output_offers_restart_plan_with_explicit_overwrite(self):
        instance = self.root / "episode-instance"
        plans, logs = instance / "plans", instance / "logs"
        plans.mkdir(parents=True)
        logs.mkdir()
        self.write_manifest()
        context = inspect_stack_console(
            self.profile, self.manifest, "S01E07", instance_root_value=instance)
        plan = build_stack_console_plan(context)
        plan_path = plans / "reference-plan.json"
        raw_plan = (json.dumps(plan, indent=2) + "\n").encode("utf-8")
        plan_path.write_bytes(raw_plan)

        validate, select = plan["steps"]
        state = _fresh_state(plan, _plan_digest(raw_plan), _profile_digest(self.profile))
        state["status"] = "PASS"
        state["steps"][0].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(validate["inputs"])),
            "output_fingerprints": [], "finished_utc": "2026-11-17T20:00:00+00:00",
        })
        self.project.write_text("verified reference project", encoding="utf-8")
        reference_report = Path(select["outputs"][2])
        reference_report.write_text("verified reference report", encoding="utf-8")
        self.write_manifest(reference="L1")
        state["steps"][1].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(select["inputs"])),
            "output_fingerprints": _fingerprint_payload(file_fingerprints(select["outputs"])),
            "finished_utc": "2026-11-17T20:01:00+00:00",
        })
        (logs / f"workflow-{plan['job_id']}.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8")

        resumed_context = inspect_stack_console(
            self.profile, self.manifest, "S01E07", instance_root_value=instance)

        self.assertEqual(resumed_context["phase"], "reference-complete")
        self.assertEqual(resumed_context["completed_stage"]["stage"], "stack-reference")
        self.assertEqual(resumed_context["completed_stage"]["job_id"], plan["job_id"])
        self.assertIn("alignment_map", " ".join(resumed_context["measurement_requirements"]))
        restart_plan = build_stack_console_plan(resumed_context, restart_completed=True)
        restart_args = restart_plan["steps"][1]["args"]
        self.assertIn("--reselect", restart_args)
        self.assertIn("--overwrite-project", restart_args)
        self.assertIn("backup", " ".join(row["value"] for row in restart_plan["decisions"]))

    def test_restart_of_verified_render_plan_allows_master_replacement(self):
        self.write_manifest(reference="L1", alignment_map="1+2", stack_frames=83230)
        self.project.write_text("verified Orc project", encoding="utf-8")
        self.master.write_bytes(b"prior verified master")
        context = {
            "phase": "render-complete", "completed_stage": {"stage": "render-stack"},
            "episode": "S01E07", "series": "DS9", "identity": "DS9:S01E07",
            "title": "Q-Less", "profile": self.profile, "manifest": self.manifest,
            "downloads": self.downloads, "project": self.project, "master": self.master,
            "probe": self.probe, "instance_root": None,
        }

        plan = build_stack_console_plan(context, planned_gib=25.0, restart_completed=True)

        render = next(step for step in plan["steps"] if step.get("stage") == "render-stack")
        self.assertIn("--overwrite-stack", render["args"])
        self.assertIn("backup", " ".join(row["value"] for row in plan["decisions"]))

    def test_inspect_picks_up_a_fingerprint_verified_completed_render_output(self):
        instance, _reference_plan = self.make_completed_reference_instance(
            alignment_map="1+2", stack_frames=83230)
        reference_context = inspect_stack_console(
            self.profile, self.manifest, "S01E07", instance)
        render_plan = build_stack_console_plan(reference_context, planned_gib=25.0)
        plans, logs = instance / "plans", instance / "logs"
        raw_plan = (json.dumps(render_plan, indent=2) + "\n").encode("utf-8")
        (plans / "render-plan.json").write_bytes(raw_plan)
        validate, render, review = render_plan["steps"]
        self.master.write_bytes(b"verified stack master")
        report = Path(render["outputs"][1])
        report.write_text("verified render report", encoding="utf-8")
        state = _fresh_state(render_plan, _plan_digest(raw_plan), _profile_digest(self.profile))
        state["status"] = "PASS"
        state["steps"][0].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(validate["inputs"])),
            "output_fingerprints": [], "finished_utc": "2026-11-17T20:03:00+00:00",
        })
        state["steps"][1].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(render["inputs"])),
            "output_fingerprints": _fingerprint_payload(file_fingerprints(render["outputs"])),
            "finished_utc": "2026-11-17T20:04:00+00:00",
        })
        state["steps"][2].update({
            "status": "PASS", "input_fingerprints": _fingerprint_payload(
                file_fingerprints(review["inputs"])),
            "output_fingerprints": [], "prompt": review["prompt"],
            "review_prompt_sha256": hashlib.sha256(review["prompt"].encode("utf-8")).hexdigest(),
            "approval": {"approved_by": "operator", "approved_at_utc": "2026-11-17T20:05:00+00:00"},
            "finished_utc": "2026-11-17T20:05:00+00:00",
        })
        (logs / f"workflow-{render_plan['job_id']}.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8")

        found = inspect_stack_console(self.profile, self.manifest, "S01E07", instance)

        self.assertEqual(found["phase"], "render-complete")
        self.assertEqual(found["completed_stage"]["stage"], "render-stack")
        self.assertEqual(found["completed_stage"]["job_id"], render_plan["job_id"])

    def test_existing_stack_master_is_never_overwritten(self):
        self.master.write_bytes(b"preserve")
        with self.assertRaisesRegex(WorkflowAppError, "no fingerprint-verified completed render step"):
            inspect_stack_console(self.profile, self.manifest, "S01E07")
        self.assertEqual(self.master.read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
