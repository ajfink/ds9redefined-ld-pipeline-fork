from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_workflow import load_plan  # noqa: E402
from workflow_app_model import (WorkflowAppError, build_stack_console_plan,
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

    def test_reference_phase_plan_is_bounded_validated_and_has_human_review(self):
        before = self.manifest.read_bytes()
        context = inspect_stack_console(self.profile, self.manifest, "S01E07")
        self.assertEqual(context["phase"], "reference")
        plan = build_stack_console_plan(context)
        path = self.root / "reference-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        loaded, _ = load_plan(path)
        self.assertEqual([step.get("stage", step.get("id")) for step in loaded["steps"]],
                         ["validate-inputs", "stack-reference", "review-reference-selection"])
        reference = loaded["steps"][1]
        self.assertIn("--report-out", reference["args"])
        self.assertEqual(reference["outputs"], [str(self.manifest), str(self.project),
                                                  reference["args"][1]])
        self.assertEqual(loaded["steps"][2]["kind"], "review")
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

    def test_render_phase_requires_measured_frames_and_explicit_space_estimate(self):
        self.write_manifest(reference="L1", alignment_map="1+2", stack_frames=83230)
        self.project.write_text("measured Orc project", encoding="utf-8")
        context = inspect_stack_console(self.profile, self.manifest, "S01E07")
        self.assertEqual(context["phase"], "render")
        with self.assertRaisesRegex(WorkflowAppError, "positive finite GiB estimate"):
            build_stack_console_plan(context)
        plan = build_stack_console_plan(context, planned_gib=26.5)
        path = self.root / "render-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        loaded, _ = load_plan(path)
        render = next(step for step in loaded["steps"] if step.get("stage") == "render-stack")
        self.assertEqual(render["args"][render["args"].index("--planned-gib") + 1], "26.500000")
        self.assertEqual(loaded["steps"][-1]["kind"], "review")

    def test_render_phase_waits_for_stack_frame_measurement(self):
        self.write_manifest(reference="L1", alignment_map="1+2", stack_frames=None)
        self.project.write_text("measured Orc project", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowAppError, "will not infer it"):
            inspect_stack_console(self.profile, self.manifest, "S01E07")

    def test_render_phase_waits_for_measured_alignment_map(self):
        self.write_manifest(reference="L1", alignment_map="", stack_frames=83230)
        self.project.write_text("measured Orc project", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowAppError, "record the measured alignment_map"):
            inspect_stack_console(self.profile, self.manifest, "S01E07")

    def test_existing_stack_master_is_never_overwritten(self):
        self.master.write_bytes(b"preserve")
        with self.assertRaisesRegex(WorkflowAppError, "never overwrites"):
            inspect_stack_console(self.profile, self.manifest, "S01E07")
        self.assertEqual(self.master.read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
