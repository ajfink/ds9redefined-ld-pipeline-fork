from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_workflow import load_plan  # noqa: E402
from workflow_app_model import (WorkflowAppError, build_intake_plan,
                                read_intake_template, write_new_plan)  # noqa: E402


def intake_template() -> dict:
    fields = {
        "episode": "S01E07",
        "profile": "C:\\machine.json",
        "archive_url": "https://archive.org/details/DS9-S01E07-Copies",
        "downloads_dir": "D:\\downloads",
        "confirmed_title": "Q-Less",
        "dvd_mode": "no-dvd",
        "cut_pad_images": "skip",
        "manifest_path": "D:\\workspace\\manifest\\S01E07.json",
    }
    return {
        "schema": "ld-pipeline-workflow-plan-v1",
        "_instructions_DO_NOT_EDIT": ["Example intake template."],
        "_operator_inputs_EDIT": {
            key: {"value": value, "_comment": f"Input {key}."}
            for key, value in fields.items()
        },
        "episode": "${input:episode}",
        "episode_title": "${input:confirmed_title}",
        "profile": "${input:profile}",
        "fingerprint_policy": "balanced",
        "decisions": [
            {"id": "dvd-mode", "value": "${input:dvd_mode}"},
            {"id": "cut-pad-images", "value": "${input:cut_pad_images}"},
        ],
        "steps": [
            {"id": "download-source", "stage": "download", "resume_policy": "download",
             "args": ["-Url", "${input:archive_url}", "-Destination", "${input:downloads_dir}"],
             "inputs": [], "outputs": ["${input:downloads_dir}"]},
            {"id": "initialize-episode", "stage": "init-episode",
             "args": ["--url", "${input:archive_url}", "--downloads", "${input:downloads_dir}",
                      "--no-dvd", "--cut-pad-images", "${input:cut_pad_images}"],
             "inputs": ["${input:downloads_dir}"], "outputs": ["${input:manifest_path}"]},
            {"id": "validate-inputs", "stage": "validate-inputs",
             "inputs": ["${input:downloads_dir}", "${input:manifest_path}"], "outputs": []},
        ],
    }


def values_for(root: Path) -> dict[str, str]:
    return {
        "episode": "S01E07",
        "confirmed_title": "Q-Less",
        "archive_url": "https://archive.org/details/DS9-S01E07-Copies",
        "profile": str(root / "machine.json"),
        "downloads_dir": str(root / "downloads"),
        "dvd_mode": "no-dvd",
        "dvd_path": "not-applicable",
        "cut_pad_images": "skip",
        "manifest_path": str(root / "workspace" / "manifest" / "S01E07.json"),
        "fingerprint_policy": "balanced",
    }


class WorkflowAppPlanTests(unittest.TestCase):
    def test_real_intake_template_builds_without_modifying_source(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.example.json"
        before = template_path.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = read_intake_template(template_path)
            plan = build_intake_plan(template, values_for(root))
            saved = write_new_plan(root / "generated-plan.json", plan)
            loaded, _ = load_plan(saved)
            self.assertEqual(loaded["episode"], "S01E07")
            self.assertEqual([step["stage"] for step in loaded["steps"]],
                             ["download", "init-episode", "validate-inputs"])
        self.assertEqual(template_path.read_bytes(), before)

    def test_no_dvd_plan_matches_audit_choice_and_never_mutates_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = intake_template()
            original = copy.deepcopy(template)
            plan = build_intake_plan(template, values_for(root))

            self.assertEqual(template, original)
            args = plan["steps"][1]["args"]
            self.assertIn("--no-dvd", args)
            self.assertIn("--cut-pad-images", args)
            self.assertNotIn("--dvd", args)
            self.assertEqual(plan["steps"][1]["outputs"], ["${input:manifest_path}"])
            self.assertEqual(plan["steps"][2]["inputs"], [
                "${input:downloads_dir}", "${input:manifest_path}"])

    def test_dvd_plan_passes_exact_file_and_omits_no_dvd_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dvd = root / "Deep Space Nine - S01E07.mkv"
            dvd.write_bytes(b"fixture")
            values = values_for(root)
            values.update({"dvd_mode": "dvd-backed", "dvd_path": str(dvd),
                           "cut_pad_images": "skip", "fingerprint_policy": "full"})
            plan = build_intake_plan(intake_template(), values)

            args = plan["steps"][1]["args"]
            self.assertEqual(args[args.index("--dvd") + 1], "${input:dvd_path}")
            self.assertNotIn("--no-dvd", args)
            self.assertNotIn("--cut-pad-images", args)
            self.assertIn("${input:dvd_path}", plan["steps"][1]["inputs"])
            self.assertIn("${input:dvd_path}", plan["steps"][2]["inputs"])
            self.assertEqual(plan["fingerprint_policy"], "full")

            saved = write_new_plan(root / "dvd-plan.json", plan)
            loaded, _ = load_plan(saved)
            command_args = loaded["steps"][1]["args"]
            self.assertEqual(command_args[command_args.index("--dvd") + 1], str(dvd.resolve()))

    def test_refuses_missing_dvd_and_mismatched_or_non_archive_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            values.update({"dvd_mode": "dvd-backed", "dvd_path": str(root / "missing.mkv")})
            with self.assertRaisesRegex(WorkflowAppError, "DVD file does not exist"):
                build_intake_plan(intake_template(), values)
            values = values_for(root)
            values["archive_url"] = "https://example.com/details/DS9-S01E07"
            with self.assertRaisesRegex(WorkflowAppError, "archive.org/details"):
                build_intake_plan(intake_template(), values)

    def test_refuses_existing_manifest_and_plan_destinations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            manifest = Path(values["manifest_path"])
            manifest.parent.mkdir(parents=True)
            manifest.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowAppError, "existing manifest"):
                build_intake_plan(intake_template(), values)

            plan_path = root / "existing.json"
            plan_path.write_text('{"keep":true}', encoding="utf-8")
            with self.assertRaisesRegex(WorkflowAppError, "refusing to overwrite"):
                write_new_plan(plan_path, {"schema": "ld-pipeline-workflow-plan-v1"})
            self.assertEqual(plan_path.read_text(encoding="utf-8"), '{"keep":true}')


if __name__ == "__main__":
    unittest.main()
