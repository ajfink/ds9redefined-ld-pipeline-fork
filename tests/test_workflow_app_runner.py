from __future__ import annotations

import io
import json
import getpass
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_workflow  # noqa: E402
import run_stage  # noqa: E402
from run_workflow import (PLAN_SCHEMA, WorkflowError, file_fingerprints,
                          generate_job_id, load_plan, run_plan, standard_output_line,
                          _run_child, _state_for_disk, WorkflowProgress)


FAKE_RUN_STAGE = r'''from __future__ import annotations
import pathlib, sys
args = sys.argv[1:]
emit = next((item.removeprefix("--script-arg=--emit=") for item in args if item.startswith("--script-arg=--emit=")), None)
counter = next((item.removeprefix("--script-arg=--counter=") for item in args if item.startswith("--script-arg=--counter=")), None)
mutate = next((item.removeprefix("--script-arg=--mutate=") for item in args if item.startswith("--script-arg=--mutate=")), None)
fail_once = next((item.removeprefix("--script-arg=--fail-once=") for item in args if item.startswith("--script-arg=--fail-once=")), None)
if counter:
    p = pathlib.Path(counter)
    count = int(p.read_text() if p.exists() else "0") + 1
    p.write_text(str(count))
    if count == 1 and "--stage" in args and args[args.index("--stage") + 1] == "download":
        print("simulated interrupted download", flush=True)
        raise SystemExit(1)
if fail_once:
    marker = pathlib.Path(fail_once)
    if not marker.exists():
        marker.write_text("first attempt failed\n")
        print("simulated failed stage with no declared output changes", flush=True)
        raise SystemExit(1)
if emit:
    out = pathlib.Path(emit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("verified output\n")
if mutate:
    pathlib.Path(mutate).write_text("updated by a later completed step\n")
print("stage report: fake-report.json", flush=True)
'''


class WorkflowPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release_patch = patch.object(run_stage, "ROOT", self.root)
        self.release_patch.start()
        (self.root / "logs").mkdir()
        self.profile = self.root / "machine.json"
        self.profile.write_text(json.dumps({
            "machine": "TEST", "os": "windows",
            "shared": {"enabled": False},
            "roots": {"logs": str(self.root / "logs")},
        }), encoding="utf-8")
        self.input = self.root / "input.dat"
        self.input.write_text("source evidence\n", encoding="utf-8")
        self.output = self.root / "output.dat"
        self.counter = self.root / "attempts.txt"
        self.fake = self.root / "fake_run_stage.py"
        self.fake.write_text(FAKE_RUN_STAGE, encoding="utf-8")

    def tearDown(self):
        self.release_patch.stop()
        self.temp.cleanup()

    def write_plan(self, *, job_id="test-job", stage="validate-inputs",
                   resume_policy="inspect", outputs=None, inputs=None, series=None):
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
        if series is not None:
            plan["series"] = series
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        return plan_path

    def test_generated_id_uses_episode_title_and_timestamp(self):
        from datetime import datetime, timezone
        generated = generate_job_id("S01E99", "Q-Less: The Return!",
                                    datetime(2026, 11, 15, 12, 30, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(generated, "S01E99-q-less-the-return-20261115T123000123456Z")
        voyager_id = generate_job_id("S01E99", "Caretaker", series="VOY")
        self.assertTrue(voyager_id.startswith("VOY-S01E99-caretaker-"))

    def test_series_qualified_plan_routes_voy_identity_and_stage_argument(self):
        path = self.write_plan(job_id="voy-plan", series="VOY")
        plan, _ = load_plan(path)
        self.assertEqual((plan["series"], plan["episode"], plan["identity"]),
                         ("VOY", "S01E99", "VOY:S01E99"))
        command = run_workflow._command(plan, plan["steps"][0], self.profile, None)
        self.assertIn("--series", command)
        self.assertEqual(command[command.index("--series") + 1], "VOY")

    def test_plan_rejects_series_identity_conflict(self):
        path = self.write_plan(job_id="conflicting-identity", series="VOY")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["identity"] = "DS9:S01E99"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "plan.identity conflicts"):
            load_plan(path)

    def test_voy_jobs_use_separate_logs_identity_and_resume_namespace(self):
        path = self.write_plan(job_id=None, series="VOY")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
            logs = self.root / "logs" / "VOY"
            state_paths = list(logs.glob("workflow-*.json"))
            self.assertEqual(len(state_paths), 1)
            state = json.loads(state_paths[0].read_text(encoding="utf-8"))
            self.assertEqual((state["series"], state["identity"]), ("VOY", "VOY:S01E99"))
            self.assertTrue(state["job_id"].startswith("VOY-S01E99-"))
            self.assertEqual(run_plan(path, execute=True, resume=True), 0)

    def test_standard_output_filter_keeps_diagnostics_and_gates(self):
        self.assertFalse(standard_output_line("loaded 4 source captures"))
        self.assertTrue(standard_output_line("WARN: optional report was not found"))
        self.assertTrue(standard_output_line("REVIEW REQUIRED [stack]: inspect metrics"))
        self.assertTrue(standard_output_line("stage report: report.json"))

    def test_workflow_progress_bar_reports_fraction_elapsed_and_idle_time(self):
        now = [10.0]
        output = io.StringIO()
        progress = WorkflowProgress(2, output, clock=lambda: now[0], interval=2.0)
        progress.start_step(1, "encode-final")
        now[0] = 14.0
        progress.heartbeat()
        progress.note_output()
        now[0] = 17.0
        progress.heartbeat()
        progress.complete_step(1, "encode-final")
        progress.complete_step(2, "qc")
        progress.finish("PASS")
        text = output.getvalue()
        self.assertIn("[#########---------] 1/2 50%", text)
        self.assertIn("1/2 encode-final - running", text)
        self.assertIn("stage 00:04 | idle 00:04", text)
        self.assertIn("100%", text)
        self.assertIn("PASS", text)

    def test_full_input_fingerprint_emits_progress_heartbeats(self):
        calls = []
        result = file_fingerprints([str(self.input)], policy="full",
                                   heartbeat=lambda: calls.append("tick"))
        self.assertEqual(result[0]["kind"], "file")
        self.assertTrue(calls)

    def test_execution_emits_progress_for_each_running_stage(self):
        path = self.write_plan(job_id="progress-plan")
        progress_output = io.StringIO()
        with patch.object(run_workflow, "RUN_STAGE", self.fake), redirect_stderr(progress_output):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
        text = progress_output.getvalue()
        self.assertIn("PROGRESS [", text)
        self.assertIn("1/1 validate-inputs - running", text)
        self.assertIn("100%", text)
        self.assertIn("PASS", text)

    def test_progress_includes_review_steps_and_marks_approval_wait(self):
        path = self.write_plan(job_id="review-progress")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"][0]["stage"] = "render-stack"
        data["steps"].append({
            "id": "review-stack", "kind": "review", "for_stage": "render-stack",
            "prompt": "Review the rendered stack.", "inputs": [str(self.output)], "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        progress_output = io.StringIO()
        with patch.object(run_workflow, "RUN_STAGE", self.fake), redirect_stderr(progress_output):
            self.assertEqual(run_plan(path, execute=True, resume=False), 3)
        text = progress_output.getvalue()
        self.assertIn("1/2 50%", text)
        self.assertIn("2/2 review review-stack - WAITING FOR HUMAN REVIEW", text)

    def test_standard_mode_hides_routine_output_but_saves_full_log(self):
        script = self.root / "routine_stage.py"
        script.write_text("print('loaded 4 source captures')\n", encoding="utf-8")
        log_path = self.root / "standard.log"
        state = {"active_index": 0, "steps": [{}]}
        output = io.StringIO()
        with log_path.open("w", encoding="utf-8") as log, redirect_stdout(output):
            code = _run_child([sys.executable, str(script)], os.environ.copy(), log, [], state,
                              self.root / "unused-state.json", verbose=False)
        self.assertEqual(code, 0)
        self.assertNotIn("loaded 4 source captures", output.getvalue())
        self.assertIn("loaded 4 source captures", log_path.read_text(encoding="utf-8"))

    def test_standard_mode_shows_recent_output_when_a_stage_fails(self):
        script = self.root / "failing_stage.py"
        script.write_text("print('opening source media')\nprint('ERROR: decoder failed')\nraise SystemExit(7)\n",
                          encoding="utf-8")
        log_path = self.root / "failure.log"
        state = {"active_index": 0, "steps": [{}]}
        output = io.StringIO()
        with log_path.open("w", encoding="utf-8") as log, redirect_stdout(output):
            code = _run_child([sys.executable, str(script)], os.environ.copy(), log, [], state,
                              self.root / "unused-state.json", verbose=False)
        self.assertEqual(code, 7)
        self.assertIn("ERROR: decoder failed", output.getvalue())
        self.assertIn("Recent stage output", output.getvalue())
        self.assertIn("opening source media", output.getvalue())
        self.assertIn("opening source media", log_path.read_text(encoding="utf-8"))

    def test_verbose_mode_streams_routine_child_output(self):
        script = self.root / "verbose_stage.py"
        script.write_text("print('loaded 4 source captures')\n", encoding="utf-8")
        log_path = self.root / "verbose.log"
        state = {"active_index": 0, "steps": [{}]}
        output = io.StringIO()
        with log_path.open("w", encoding="utf-8") as log, redirect_stdout(output):
            code = _run_child([sys.executable, str(script)], os.environ.copy(), log, [], state,
                              self.root / "unused-state.json", verbose=True)
        self.assertEqual(code, 0)
        self.assertIn("loaded 4 source captures", output.getvalue())

    def test_plan_rejects_non_allowlisted_stages_and_bad_resume_policy(self):
        path = self.write_plan(stage="arbitrary-shell")
        with self.assertRaisesRegex(WorkflowError, "not an allowlisted"):
            load_plan(path)
        path = self.write_plan(job_id="bad-retry", stage="validate-inputs", resume_policy="download")
        with self.assertRaisesRegex(WorkflowError, "only the allowlisted download"):
            load_plan(path)

    def test_plan_accepts_powerShell_utf8_bom(self):
        path = self.write_plan(job_id="bom-plan")
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        plan, _ = load_plan(path)
        self.assertEqual(plan["job_id"], "bom-plan")

    def test_intake_example_is_a_valid_ordered_allowlisted_plan(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.example.json"
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

    def test_legacy_v10_dvd_backed_plan_remains_loadable_as_ds9(self):
        dvd = self.root / "legacy-matching-dvd.mkv"
        dvd.write_bytes(b"DVD evidence fixture")
        plan_path = self.root / "legacy-v10-dvd-plan.json"
        plan = {
            "schema": PLAN_SCHEMA,
            "_instructions_DO_NOT_EDIT": [
                "DVD mode choices: use DVD-backed with an exact path, or explicitly choose no-DVD."],
            "_operator_inputs_EDIT": {
                "episode": {"value": "S01E07", "_comment": "episode"},
                "profile": {"value": str(self.profile), "_comment": "profile"},
                "archive_url": {"value": "https://archive.org/details/DS9-S01E07-Copies", "_comment": "URL"},
                "downloads_dir": {"value": str(self.root / "downloads"), "_comment": "downloads"},
                "confirmed_title": {"value": "Q-Less", "_comment": "title"},
                "dvd_mode": {"value": "dvd-backed", "_comment": "DVD mode"},
                "dvd_path": {"value": str(dvd), "_comment": "DVD"},
                "cut_pad_images": {"value": "skip", "_comment": "cut pads"},
                "manifest_path": {"value": str(self.root / "S01E07.json"), "_comment": "manifest"},
            },
            "episode": "${input:episode}",
            "episode_title": "${input:confirmed_title}",
            "profile": "${input:profile}",
            "fingerprint_policy": "balanced",
            "decisions": [],
            "steps": [{"id": "validate", "stage": "validate-inputs",
                       "inputs": [str(self.input)], "outputs": []}],
        }
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        loaded, _raw = load_plan(plan_path)
        self.assertEqual((loaded["series"], loaded["episode"], loaded["identity"]),
                         ("DS9", "S01E07", "DS9:S01E07"))
        self.assertEqual(loaded["_operator_inputs_EDIT"]["dvd_mode"]["value"], "dvd-backed")

    def test_template_rejects_placeholder_embedded_inside_a_path(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.example.json"
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

    def test_plan_cannot_chain_past_a_review_gated_stage_without_review_step(self):
        path = self.write_plan(job_id="missing-stack-review", stage="render-stack")
        with self.assertRaisesRegex(WorkflowError, "followed by an explicit human review"):
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

    def test_review_gate_waits_for_explicit_human_approval_and_resumes(self):
        path = self.write_plan(job_id="review-gate")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"][0]["stage"] = "render-stack"
        data["steps"].append({
            "id": "review-stack-evidence",
            "kind": "review",
            "for_stage": "render-stack",
            "prompt": "Review the stack quality metrics and confirm the alignment evidence.",
            "inputs": [str(self.output)],
            "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 3)
            state_path = self.root / "logs" / "workflow-review-gate.json"
            waiting = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(waiting["status"], "WAITING_APPROVAL")
            self.assertEqual(run_plan(path, execute=True, resume=True), 3)
            self.assertEqual(run_plan(path, execute=True, resume=True,
                                      approve_steps={"review-stack-evidence"}), 0)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        review = saved["steps"][1]
        self.assertEqual(saved["status"], "PASS")
        self.assertEqual(review["status"], "PASS")
        self.assertTrue(review["approval"]["approved_by"])

    def test_report_only_reference_review_is_audited_as_auto_skipped(self):
        path = self.write_plan(job_id="auto-skip-reference", stage="stack-reference")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"].append({
            "id": "review-reference-selection", "kind": "review",
            "for_stage": "stack-reference", "prompt": "Review the detailed report.",
            "inputs": [str(self.output)], "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
        saved = json.loads((self.root / "logs" / "workflow-auto-skip-reference.json").read_text(
            encoding="utf-8"))
        review = saved["steps"][1]
        self.assertEqual(review["status"], "PASS")
        self.assertNotIn("approval", review)
        self.assertIn("alignment_map and stack.frames", review["auto_review_skip"]["reason"])
        self.assertFalse(review["auto_review_skip"]["reviewed_by_human"])

    def test_explicit_efm_and_retime_record_plans_skip_redundant_post_write_review(self):
        for job_id, stage, review_id in (
                ("auto-skip-efm-record", "record-efm-capture", "review-recorded-efm-capture"),
                ("auto-skip-retime-record", "record-retime-breaks", "approve-retime-break-recording")):
            with self.subTest(stage=stage):
                path = self.write_plan(job_id=job_id, stage=stage,
                                       inputs=[str(self.input)], outputs=[str(self.output)])
                data = json.loads(path.read_text(encoding="utf-8"))
                data["steps"].append({"id": review_id, "kind": "review", "for_stage": stage,
                                      "prompt": "The operator's selected values were already recorded.",
                                      "inputs": [str(self.output)], "outputs": []})
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                with patch.object(run_workflow, "RUN_STAGE", self.fake):
                    self.assertEqual(run_plan(path, execute=True, resume=False), 0)
                saved = json.loads((self.root / "logs" / f"workflow-{job_id}.json").read_text(
                    encoding="utf-8"))
                self.assertEqual(saved["steps"][1]["status"], "PASS")
                self.assertFalse(saved["steps"][1]["auto_review_skip"]["reviewed_by_human"])
                self.assertTrue(saved["steps"][1]["auto_review_skip"]["reason"])

    def test_retime_candidate_report_gate_is_auto_skipped_before_dedicated_review_ui(self):
        path = self.write_plan(job_id="auto-skip-retime", stage="prepare-retime-breaks")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"].append({
            "id": "review-retime-break-candidates", "kind": "review",
            "for_stage": "prepare-retime-breaks", "prompt": "Review the detailed report.",
            "inputs": [str(self.output)], "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
        saved = json.loads((self.root / "logs" / "workflow-auto-skip-retime.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved["steps"][1]["status"], "PASS")
        self.assertFalse(saved["steps"][1]["auto_review_skip"]["reviewed_by_human"])

    def test_exact_approved_review_is_reused_for_a_new_job_but_not_changed_prompt(self):
        def make_review_plan(job_id: str, prompt: str) -> Path:
            path = self.root / f"{job_id}.json"
            plan = {
                "schema": PLAN_SCHEMA, "job_id": job_id, "episode": "S01E99",
                "episode_title": "Q-Less", "profile": str(self.profile), "decisions": [],
                "steps": [
                    {"id": "validate-evidence", "stage": "validate-inputs",
                     "inputs": [str(self.input)], "outputs": []},
                    {"id": "review-identical-evidence", "kind": "review",
                     "for_stage": "validate-inputs", "prompt": prompt,
                     "inputs": [str(self.input)], "outputs": []},
                ],
            }
            path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            return path

        first = make_review_plan("approval-source", "Review this exact verified evidence.")
        second = make_review_plan("approval-reuse", "Review this exact verified evidence.")
        changed = make_review_plan("approval-changed-prompt", "Review this evidence under a changed policy.")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(first, execute=True, resume=False), 3)
            self.assertEqual(run_plan(first, execute=True, resume=True,
                                      approve_steps={"review-identical-evidence"}), 0)
            legacy_state_path = self.root / "logs" / "workflow-approval-source.json"
            legacy_state = json.loads(legacy_state_path.read_text(encoding="utf-8"))
            legacy_state["steps"][1].pop("review_prompt_sha256", None)
            legacy_state_path.write_text(json.dumps(legacy_state, indent=2) + "\n",
                                         encoding="utf-8")
            self.assertEqual(run_plan(second, execute=True, resume=False), 0)
            self.assertEqual(run_plan(changed, execute=True, resume=False), 3)

        reused_state = json.loads((self.root / "logs" / "workflow-approval-reuse.json").read_text(
            encoding="utf-8"))
        approval = reused_state["steps"][1]["approval"]
        self.assertEqual(approval["reused_from_job_id"], "approval-source")
        self.assertEqual(approval["approved_by"], getpass.getuser())

    def test_resume_allows_a_later_passed_step_to_update_an_earlier_input(self):
        manifest = self.root / "manifest.json"
        manifest.write_text("original manifest\n", encoding="utf-8")
        project = self.root / "stack.orcprj"
        path = self.root / "later-manifest-writer.json"
        plan = {
            "schema": PLAN_SCHEMA, "job_id": "later-manifest-writer",
            "episode": "S01E99", "episode_title": "Q-Less", "profile": str(self.profile),
            "decisions": [],
            "steps": [
                {"id": "validate-inputs", "stage": "validate-inputs",
                 "inputs": [str(manifest)], "outputs": []},
                {"id": "select-reference", "stage": "stack-reference",
                 "args": [f"--mutate={manifest}", f"--emit={project}"],
                 "inputs": [str(self.input)], "outputs": [str(manifest), str(project)]},
                {"id": "review-reference", "kind": "review", "for_stage": "stack-reference",
                 "prompt": "Review the selection evidence.",
                 "inputs": [str(manifest), str(project)], "outputs": []},
            ],
        }
        path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 3)
            state_path = self.root / "logs" / "workflow-later-manifest-writer.json"
            waiting = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(waiting["status"], "WAITING_APPROVAL")
            self.assertEqual(run_plan(path, execute=True, resume=True,
                                      approve_steps={"review-reference"}), 0)
        self.assertEqual(manifest.read_text(encoding="utf-8"),
                         "updated by a later completed step\n")

    def test_resume_continues_and_records_handoff_when_workflow_version_changes(self):
        path = self.write_plan(job_id="version-change")
        version_file = self.root / "WORKFLOW_VERSION"
        version_file.write_text("32\n", encoding="ascii")
        with patch.object(run_workflow, "ROOT", self.root), \
                patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
            state_path = self.root / "logs" / "workflow-version-change.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["workflow_version"] = "31"
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

            self.assertEqual(run_plan(path, execute=True, resume=True), 0)

        resumed = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["workflow_version"], "32")
        self.assertEqual(resumed["workflow_version_history"][-1]["from"], "31")
        self.assertEqual(resumed["workflow_version_history"][-1]["to"], "32")
        self.assertIn("workflow_version_handoff=31->32",
                      (self.root / "logs" / "workflow-version-change.log").read_text(encoding="utf-8"))

    def test_resume_retries_failed_step_from_last_pass_when_outputs_are_unchanged(self):
        steps = []
        counters = []
        for index in range(1, 5):
            output = self.root / f"step-{index}.dat"
            counter = self.root / f"step-{index}.attempts"
            args = [f"--emit={output}", f"--counter={counter}"]
            if index == 4:
                args.append(f"--fail-once={self.root / 'step-4-failure.marker'}")
            steps.append({"id": f"stage-{index}", "stage": "validate-inputs",
                          "args": args, "inputs": [str(self.input)], "outputs": [str(output)]})
            counters.append(counter)
        plan_path = self.root / "safe-retry.json"
        plan_path.write_text(json.dumps({
            "schema": PLAN_SCHEMA, "job_id": "safe-retry", "episode": "S01E99",
            "episode_title": "Q-Less", "profile": str(self.profile), "decisions": [],
            "steps": steps,
        }, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(plan_path, execute=True, resume=False), 1)
            state_path = self.root / "logs" / "workflow-safe-retry.json"
            failed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual([step["status"] for step in failed["steps"]],
                             ["PASS", "PASS", "PASS", "FAILED"])
            self.assertEqual(failed["steps"][3]["output_fingerprints_before"], [])

            self.assertEqual(run_plan(plan_path, execute=True, resume=True), 0)

        self.assertEqual([int(path.read_text(encoding="utf-8")) for path in counters],
                         [1, 1, 1, 2])

    def test_reference_selection_report_review_is_not_a_human_gate(self):
        path = self.write_plan(job_id="optional-reference-review", stage="stack-reference")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"].append({
            "id": "review-reference-selection", "kind": "review",
            "for_stage": "stack-reference",
            "prompt": "Review the source-reference selection report and project.",
            "inputs": [str(self.output)], "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 0)
        saved = json.loads((self.root / "logs" / "workflow-optional-reference-review.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved["steps"][1]["status"], "PASS")
        self.assertIn("User-requested", saved["steps"][1]["auto_review_skip"]["reason"])

    def test_denied_review_is_durable_and_terminal(self):
        path = self.write_plan(job_id="denied-review")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"][0]["stage"] = "render-stack"
        data["steps"].append({
            "id": "review-stack-evidence", "kind": "review",
            "for_stage": "render-stack", "prompt": "Review the rendered stack.",
            "inputs": [str(self.output)], "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 3)
            self.assertEqual(run_plan(path, execute=True, resume=True,
                                      deny_steps={"review-stack-evidence"}), 4)
            with self.assertRaisesRegex(WorkflowError, "job was rejected.*terminal"):
                run_plan(path, execute=True, resume=True)
        state_path = self.root / "logs" / "workflow-denied-review.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "REJECTED")
        review = saved["steps"][1]
        self.assertEqual(review["status"], "REJECTED")
        self.assertTrue(review["denial"]["denied_by"])
        self.assertTrue(review["denial"]["denied_at_utc"])

    def test_review_cannot_be_approved_before_runner_records_waiting_state(self):
        path = self.write_plan(job_id="premature-approval")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"][0]["stage"] = "render-stack"
        data["steps"].append({
            "id": "review-stack-evidence", "kind": "review",
            "for_stage": "render-stack", "prompt": "Review the rendered stack.",
            "inputs": [str(self.output)], "outputs": [],
        })
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with patch.object(run_workflow, "RUN_STAGE", self.fake):
            self.assertEqual(run_plan(path, execute=True, resume=False), 3)
            state_path = self.root / "logs" / "workflow-premature-approval.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"][1]["status"] = "PENDING"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "not currently waiting"):
                run_plan(path, execute=True, resume=True,
                         approve_steps={"review-stack-evidence"})


if __name__ == "__main__":
    unittest.main()
