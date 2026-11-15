from __future__ import annotations

import json
import getpass
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_workflow  # noqa: E402
from run_workflow import (PLAN_SCHEMA, WorkflowError, file_fingerprints,
                          generate_job_id, load_plan, run_plan, workflow_lock,
                          _state_for_disk)


FAKE_RUN_STAGE = r'''from __future__ import annotations
import pathlib, sys
args = sys.argv[1:]
emit = next((item.removeprefix("--script-arg=--emit=") for item in args if item.startswith("--script-arg=--emit=")), None)
counter = next((item.removeprefix("--script-arg=--counter=") for item in args if item.startswith("--script-arg=--counter=")), None)
if counter:
    p = pathlib.Path(counter)
    count = int(p.read_text() if p.exists() else "0") + 1
    p.write_text(str(count))
    if count == 1 and "--stage" in args and args[args.index("--stage") + 1] == "download":
        print("simulated interrupted download", flush=True)
        raise SystemExit(1)
if emit:
    out = pathlib.Path(emit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("verified output\n")
print("stage report: fake-report.json", flush=True)
'''


class WorkflowPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "logs").mkdir()
        self.profile = self.root / "machine.json"
        self.profile_data = {
            "machine": "TEST", "os": "windows",
            "shared": {"enabled": False},
            "roots": {"logs": str(self.root / "logs")},
        }
        self.profile.write_text(json.dumps(self.profile_data), encoding="utf-8")
        profile_patch = patch.object(run_workflow, "load_profile",
                                     return_value=(self.profile, self.profile_data))
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        self.input = self.root / "input.dat"
        self.input.write_text("source evidence\n", encoding="utf-8")
        self.output = self.root / "output.dat"
        self.counter = self.root / "attempts.txt"
        self.fake = self.root / "fake_run_stage.py"
        self.fake.write_text(FAKE_RUN_STAGE, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_plan(self, *, job_id="test-job", stage="validate-inputs",
                   resume_policy="inspect", outputs=None, inputs=None):
        plan_path = self.root / f"{job_id}.json"
        plan = {
            "schema": PLAN_SCHEMA,
            "episode": "S01E99",
            "episode_title": "Q-Less",
            "profile": str(self.profile),
            "decisions": [{"choice": "test fixture"}],
            "steps": [{
                "id": "step-1", "stage": stage,
                "args": [f"--emit={self.output}", f"--counter={self.counter}"],
                "inputs": inputs if inputs is not None else [str(self.input)],
                "outputs": outputs if outputs is not None else [str(self.output)],
                "resume_policy": resume_policy,
            }],
        }
        if job_id is not None:
            plan["job_id"] = job_id
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        return plan_path

    def test_generated_id_uses_episode_title_and_timestamp(self):
        from datetime import datetime, timezone
        generated = generate_job_id("S01E99", "Q-Less: The Return!",
                                    datetime(2026, 11, 15, 12, 30, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(generated, "S01E99-q-less-the-return-20261115T123000123456Z")

    def test_plan_rejects_non_allowlisted_stages_and_bad_resume_policy(self):
        path = self.write_plan(stage="arbitrary-shell")
        with self.assertRaisesRegex(WorkflowError, "not an allowlisted"):
            load_plan(path)
        path = self.write_plan(job_id="bad-retry", stage="validate-inputs", resume_policy="download")
        with self.assertRaisesRegex(WorkflowError, "only the allowlisted download"):
            load_plan(path)

    def test_intake_example_is_a_valid_ordered_allowlisted_plan(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.json"
        data = json.loads(template_path.read_text(encoding="utf-8"))
        replacements = {
            "episode": "S01E07",
            "profile": str(self.profile),
            "archive_url": "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
            "downloads_dir": str(self.root / "downloads"),
            "confirmed_title": "Q-Less",
            "dvd_mode": "no-dvd",
            "cut_pad_images": "skip",
            "manifest_path": str(self.root / "episode.json"),
        }
        for key, value in replacements.items():
            data["_operator_inputs_EDIT"][key]["value"] = value
        temp_plan = self.root / "ready-intake.json"
        temp_plan.write_text(json.dumps(data), encoding="utf-8")
        plan, _ = load_plan(temp_plan)
        self.assertEqual([step["stage"] for step in plan["steps"]],
                         ["download", "init-episode", "validate-inputs"])
        self.assertEqual(plan["steps"][0]["resume_policy"], "download")
        self.assertEqual(plan["steps"][0]["args"][1], data["_operator_inputs_EDIT"]["archive_url"]["value"])
        self.assertEqual(plan["steps"][1]["args"][-2:], ["--cut-pad-images", "skip"])
        self.assertNotIn("job_id", plan)
        self.assertEqual(plan["episode_title"], "Q-Less")
        self.assertTrue(all("_comment" in field for field in data["_operator_inputs_EDIT"].values()))

    def test_root_intake_template_is_sanitized_and_keeps_per_setting_comments(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.json"
        raw = template_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIn("_instructions_DO_NOT_EDIT", data)
        self.assertIn("_operator_inputs_EDIT", data)
        for setting in data["_operator_inputs_EDIT"].values():
            self.assertEqual(set(setting), {"value", "_comment"})
            self.assertTrue(setting["_comment"].strip())
            self.assertIn("REPLACE_", setting["value"])
        self.assertNotRegex(raw, r"(?i)(?:[A-Z]:\\|\\\\)[^\s\"]+")
        self.assertIn("${input:profile}", data["profile"])

    def test_template_rejects_placeholder_embedded_inside_a_path(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.json"
        data = json.loads(template_path.read_text(encoding="utf-8"))
        replacements = {
            "episode": "S01E07",
            "profile": str(self.profile),
            "archive_url": "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
            "downloads_dir": "D:\\REPLACE_ME\\downloads",
            "confirmed_title": "Q-Less",
            "dvd_mode": "no-dvd",
            "cut_pad_images": "skip",
            "manifest_path": str(self.root / "episode.json"),
        }
        for key, value in replacements.items():
            data["_operator_inputs_EDIT"][key]["value"] = value
        temp_plan = self.root / "unfinished-intake.json"
        temp_plan.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "_operator_inputs_EDIT.downloads_dir"):
            load_plan(temp_plan)

    def test_intake_template_rejects_dvd_choice_that_conflicts_with_its_command(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.json"
        data = json.loads(template_path.read_text(encoding="utf-8"))
        replacements = {
            "episode": "S01E07",
            "profile": str(self.profile),
            "archive_url": "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
            "downloads_dir": str(self.root / "downloads"),
            "confirmed_title": "Q-Less",
            "dvd_mode": "dvd",
            "cut_pad_images": "skip",
            "manifest_path": str(self.root / "episode.json"),
        }
        for key, value in replacements.items():
            data["_operator_inputs_EDIT"][key]["value"] = value
        temp_plan = self.root / "mismatched-dvd-intake.json"
        temp_plan.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "supports only 'no-dvd'"):
            load_plan(temp_plan)

    def test_generated_job_can_be_resumed_without_asking_for_its_id(self):
        path = self.write_plan(job_id=None)
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
            state_paths = list((self.root / "logs").glob("workflow-*.json"))
            self.assertEqual(len(state_paths), 1)
            saved = json.loads(state_paths[0].read_text(encoding="utf-8"))
            self.assertRegex(saved["job_id"], r"^S01E99-q-less-\d{8}T\d{12}Z$")
            self.assertEqual(run_plan(path, execute=True, resume=True), 0)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")

    def test_fingerprint_detects_content_change(self):
        before = file_fingerprints([str(self.input)])
        self.input.write_text("changed evidence\n", encoding="utf-8")
        after = file_fingerprints([str(self.input)])
        self.assertNotEqual(before, after)

    def test_directory_fingerprint_detects_added_or_changed_source_file(self):
        folder = self.root / "captures"
        folder.mkdir()
        tbc = folder / "capture.tbc"
        tbc.write_bytes(b"source")
        before = file_fingerprints([str(folder)])
        (folder / "capture.tbc.json").write_text("sidecar", encoding="utf-8")
        self.assertNotEqual(before, file_fingerprints([str(folder)]))

    def test_persisted_state_keeps_fingerprints_without_absolute_paths(self):
        fingerprint = file_fingerprints([str(self.input)])
        state = {"steps": [{"input_fingerprints": fingerprint, "output_fingerprints": []}]}
        saved = _state_for_disk(state, [])
        self.assertNotIn(str(self.input), saved)
        self.assertIn("path_id", saved)

    def test_plan_rejects_overlapping_input_and_output_paths(self):
        path = self.write_plan(job_id="overlap", inputs=[str(self.root)], outputs=[str(self.input)])
        with self.assertRaisesRegex(WorkflowError, "must not overlap"):
            load_plan(path)

    def test_resume_skips_only_when_inputs_and_outputs_still_match(self):
        path = self.write_plan()
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
            saved = json.loads((self.root / "logs" / "workflow-test-job.json").read_text(encoding="utf-8"))
            decision = saved["decisions"][0]
            self.assertEqual(decision["approved_by"], getpass.getuser())
            self.assertIsNotNone(datetime.fromisoformat(decision["approved_at_utc"].replace("Z", "+00:00")))
            attempts = self.counter.read_text(encoding="utf-8")
            self.assertEqual(run_plan(path, execute=True, resume=True), 0)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), attempts)

        self.output.write_text("changed output\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            with self.assertRaisesRegex(WorkflowError, "output changed"):
                run_plan(path, execute=True, resume=True)

    def test_dry_run_creates_no_state_or_log(self):
        path = self.write_plan(job_id="dry-run")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=False, resume=False), 0)
        self.assertFalse((self.root / "logs" / "workflow-dry-run.json").exists())
        self.assertFalse((self.root / "logs" / "workflow-dry-run.log").exists())

    def test_job_lock_rejects_a_second_writer(self):
        lock_path = self.root / "logs" / "workflow-test.lock"
        with workflow_lock(lock_path):
            with self.assertRaisesRegex(WorkflowError, "another process holds workflow lock"):
                with workflow_lock(lock_path):
                    self.fail("second writer acquired an active job lock")

    def test_resume_rejects_changed_inputs(self):
        path = self.write_plan(job_id="changed-input")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
            self.input.write_text("different source evidence\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "input changed"):
                run_plan(path, execute=True, resume=True)

    def test_only_resumable_download_retries_a_failed_step(self):
        path = self.write_plan(job_id="resume-download", stage="download",
                               resume_policy="download", inputs=[])
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 1)
            self.assertEqual(run_plan(path, execute=True, resume=True), 0)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "2")
        self.assertTrue(self.output.is_file())


if __name__ == "__main__":
    unittest.main()
