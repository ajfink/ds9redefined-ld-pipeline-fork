from __future__ import annotations

import sys
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workflow_app  # noqa: E402
from workflow_app_model import read_intake_template  # noqa: E402
import shared_workspace  # noqa: E402


class FakeVariable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeLabel:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class FakeRoot:
    def __init__(self):
        self.idle_callbacks = []

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)

    def after(self, _milliseconds, _callback):
        return None


class WorkflowAppCallbackTests(unittest.TestCase):
    def test_intake_template_placeholders_are_not_displayed_as_selections(self):
        self.assertEqual(workflow_app._form_value_from_template("REPLACE_WITH_no-dvd"), "")
        self.assertEqual(workflow_app._form_value_from_template("REPLACE_WITH_use_or_skip"), "")
        self.assertEqual(workflow_app._form_value_from_template("YOUR_ACCOUNT"), "")
        self.assertEqual(workflow_app._form_value_from_template("no-dvd"), "no-dvd")
        self.assertEqual(workflow_app._form_value_from_template("skip"), "skip")

    def test_intake_plan_save_suggests_next_unused_immutable_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(workflow_app._next_intake_plan_filename("S01E12", directory),
                             "S01E12-intake-plan.json")
            (directory / "S01E12-intake-plan.json").write_text("{}", encoding="utf-8")
            self.assertEqual(workflow_app._next_intake_plan_filename("S01E12", directory),
                             "S01E12-intake-plan-2.json")
            (directory / "S01E12-intake-plan-2.json").write_text("{}", encoding="utf-8")
            self.assertEqual(workflow_app._next_intake_plan_filename("S01E12", directory),
                             "S01E12-intake-plan-3.json")

    def test_new_intake_plans_live_under_the_profile_device_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "app"
            profile = (bundle / "devices" / "DEVICE-1234ABCD" /
                       "local device config" / "machine.json")
            profile.parent.mkdir(parents=True)
            profile.write_text("{}\n", encoding="utf-8")
            plans = workflow_app._device_plan_directory(profile, bundle)
            self.assertEqual(plans, profile.parents[1] / "plans")
            self.assertTrue(plans.is_dir())
            with self.assertRaisesRegex(workflow_app.WorkflowAppError, "under this device's devices folder"):
                workflow_app._device_plan_directory(bundle / "machine.json", bundle)

    def test_auto_save_keeps_intake_editable_and_writes_under_device_plans(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "app"
            device_root = bundle / "devices" / "DEVICE-1234ABCD"
            config_dir = device_root / "local device config"
            config_dir.mkdir(parents=True)
            profile = config_dir / "machine.json"
            profile.write_text(json.dumps({
                "machine": "DEVICE", "shared": {"enabled": True, "release_root": str(bundle)},
                "roots": {"downloads_root": str(device_root / "downloads")},
            }), encoding="utf-8")
            source = Path(temporary) / "DS9 S01E12 LaserDisc"
            source.mkdir()
            for capture in ("L1", "M1"):
                tbc = source / f"DS9-S01E12-Vortex-{capture}.tbc"
                tbc.write_bytes(b"synthetic capture")
                Path(str(tbc) + ".json").write_text("{}\n", encoding="utf-8")
            dvd = Path(temporary) / "Vortex.mkv"
            dvd.write_bytes(b"synthetic DVD reference")

            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = None
            app.template = read_intake_template(workflow_app.EXAMPLE_TEMPLATE)
            app.template_path = workflow_app.EXAMPLE_TEMPLATE
            app.profile_path = profile
            app.values = {key: FakeVariable(value) for key, value in {
                "series": "DS9", "episode": "S01E12", "source_mode": "local",
                "local_source_path": str(source), "archive_url": "",
                "confirmed_title": "", "dvd_mode": "dvd-backed", "dvd_path": str(dvd),
                "cut_pad_images": "", "profile": str(profile),
                "downloads_dir": "", "manifest_path": str(
                    device_root / "episodes" / "S01E12" / "manifest" / "S01E12.json"),
                "instance_root": str(device_root / "episodes" / "S01E12"),
                "fingerprint_policy": "balanced",
            }.items()}
            app.resolved_archive_url = f"local:{source}"
            app.source_label = FakeLabel()
            app.status = FakeVariable()
            app.form_editable = True
            app._set_form_editable = lambda editable: setattr(app, "form_editable", editable)
            app._append_output = lambda _text: None
            app._update_review_choices = lambda: None

            with patch.object(workflow_app, "ROOT", bundle):
                self.assertTrue(app._save_intake_plan())

            self.assertTrue(app.form_editable)
            self.assertEqual(app.plan_path.parent, device_root / "plans")
            self.assertEqual(app.plan["episode_title"], "Title not supplied (DS9:S01E12)")
            self.assertTrue(app.plan_from_intake)
            self.assertEqual(app.intake_plan_snapshot, app._current_intake_snapshot())

    def test_run_after_form_edits_creates_and_previews_new_plan_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            old_plan = Path(temporary) / "old-plan.json"
            new_plan = Path(temporary) / "new-plan.json"
            old_plan.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.plan_path = old_plan
            app.plan = {"steps": []}
            app.template = {"schema": "intake-template"}
            app.plan_from_intake = True
            app.intake_plan_snapshot = "before-edit"
            app.status = FakeVariable()
            app._current_intake_snapshot = lambda: "after-edit"
            saves = []
            app._save_intake_plan = lambda: (saves.append(True) or
                                             setattr(app, "plan_path", new_plan) or True)
            commands = []
            app._start_process = lambda command, action: commands.append((command, action))
            app._logging_args = lambda: ["--standard-output"]
            app._require_preview = lambda: self.fail("changed form must preview before run")

            app._launch("run")

            self.assertEqual(len(saves), 1)
            self.assertEqual(len(commands), 1)
            command, action = commands[0]
            self.assertEqual(action, "preview")
            self.assertIn(str(new_plan), command)
            self.assertNotIn("--execute", command)

    def test_successful_intake_run_is_marked_for_automatic_stack_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "completed-intake-plan.json"
            path.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.plan_path = path
            app.plan = {"steps": [
                {"stage": "init-local-episode"}, {"stage": "validate-inputs"}]}
            app.template = None
            app.plan_from_intake = False
            app.previewed_plan_hash = "current"
            app.auto_advance_after_run = False
            app.auto_run_after_preview = False
            app._require_preview = lambda: True
            app._set_form_editable = lambda _editable: None
            app._logging_args = lambda: []
            commands = []
            app._start_process = lambda command, action: commands.append((command, action))

            app._launch("run")

            self.assertTrue(app.auto_advance_after_run)
            self.assertEqual(commands[0][1], "run")
            self.assertIn("--execute", commands[0][0])

    def test_nonfinal_run_needs_no_extra_confirmation_and_arms_auto_stack(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "intake-plan.json"
            path.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.plan_path = path
            app.plan = {"steps": [{"stage": "init-local-episode"},
                                  {"stage": "validate-inputs"}]}
            app.template = None
            app.plan_from_intake = False
            app._require_preview = lambda: True
            app._set_form_editable = lambda editable: setattr(app, "form_editable", editable)
            app._logging_args = lambda: []
            app.form_editable = True
            app.auto_advance_after_run = False
            commands = []
            app._start_process = lambda command, action: commands.append((command, action))
            with patch.object(workflow_app.messagebox, "askyesno",
                              side_effect=AssertionError("nonfinal run should not add another confirmation")):
                app._launch("run")
            self.assertTrue(app.auto_advance_after_run)
            self.assertFalse(app.form_editable)
            self.assertEqual(commands[0][1], "run")

    def test_successful_intake_completion_dispatches_automatic_stack_transition(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_advance_after_run = True
        app.auto_run_after_preview = False
        app.process = object()
        app.root = FakeRoot()
        app.status = FakeVariable()
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        transitions = []
        app._auto_start_stack_reference = lambda: transitions.append("stack-reference")

        app._drain_output()

        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(transitions, ["stack-reference"])

    def test_only_intake_plans_are_eligible_for_automatic_stack_continuation(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.plan = {"steps": [{"stage": "download"}, {"stage": "init-episode"},
                              {"stage": "validate-inputs"}]}
        self.assertTrue(app._is_intake_completion_plan())
        app.plan = {"steps": [{"stage": "validate-inputs"}, {"stage": "stack-reference"},
                              {"kind": "review", "for_stage": "stack-reference"}]}
        self.assertFalse(app._is_intake_completion_plan())

    def test_successful_intake_run_automatically_schedules_stack_continuation(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_advance_after_run = True
        app.auto_run_after_preview = False
        app.process = object()
        app.root = FakeRoot()
        app.status = FakeVariable()
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        continued = []
        app._auto_start_stack_reference = lambda: continued.append(True)

        app._drain_output()

        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(continued, [True])

    def test_failed_pad_record_clears_pending_automatic_rife_continuation(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__1")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = True
        app.process = object()
        app.root = FakeRoot()
        app.status = FakeVariable()
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None

        app._drain_output()

        self.assertFalse(app.auto_start_rife_after_pad_record)
        self.assertEqual(app.root.idle_callbacks, [])

    def test_successful_pad_record_run_dispatches_fresh_rife_plan(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = True
        app.process = object()
        app.root = FakeRoot()
        app.status = FakeVariable()
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        continued = []
        app._auto_start_rife_replacements = lambda: continued.append("rife")

        app._drain_output()

        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(continued, ["rife"])
        self.assertFalse(app.auto_start_rife_after_pad_record)

    def test_pad_preview_auto_run_keeps_rife_continuation_armed(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "preview"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_advance_after_run = False
        app.auto_run_after_preview = True
        app.auto_start_rife_after_pad_record = True
        app.process = object()
        app.root = FakeRoot()
        app.status = FakeVariable()
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        launches = []
        app._launch = lambda action, **_kwargs: launches.append(action)

        app._drain_output()

        self.assertTrue(app.auto_start_rife_after_pad_record)
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(launches, ["run"])

    def test_stage_plan_auto_saves_under_episode_workspace_without_file_dialog(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as identity_dir:
            bundle = Path(temporary) / "app"
            bundle.mkdir()
            (bundle / "machine.example.json").write_bytes(
                (workflow_app.ROOT / "machine.example.json").read_bytes())
            (bundle / "WORKFLOW_VERSION").write_text("15\n", encoding="ascii")
            with patch.dict("os.environ", {"LDP_DEVICE_ID_DIR": identity_dir}, clear=False), \
                    patch.object(shared_workspace, "ROOT", bundle):
                profile_path, _ = shared_workspace.bootstrap_device_profile(
                    bundle, system="Windows", machine="DEVICE-STAGEPLAN")
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                workspace = shared_workspace.create_workspace(
                    profile_path, profile, "S01E12", series="DS9")
            instance = Path(workspace["instance_root"])
            plan = {
                "schema": "ld-pipeline-workflow-plan-v1",
                "job_id": "stack-reference-test",
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "episode_title": "Vortex", "profile": str(profile_path),
                "instance_root": str(instance), "fingerprint_policy": "balanced",
                "decisions": [], "steps": [{"id": "validate", "stage": "validate-inputs",
                                               "inputs": [str(instance / "manifest" / "S01E12.json")],
                                               "outputs": []}],
            }
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = None
            app.plan_path = None
            app.plan = None
            app.plan_from_intake = True
            app.intake_plan_snapshot = "old"
            app.template = {}
            app.previewed_plan_hash = None
            app.waiting_review_step = None
            app.opencode_help_results = {}
            app.source_label = FakeLabel()
            app.status = FakeVariable()
            app.form_editable = True
            app.console_expanded = True
            app._set_form_editable = lambda editable: setattr(app, "form_editable", editable)
            app._update_review_choices = lambda: None
            app._append_output = lambda _text: None

            with patch.object(workflow_app, "ROOT", bundle), \
                    patch.object(shared_workspace, "ROOT", bundle), \
                    patch.dict("os.environ", {"LDP_DEVICE_ID_DIR": identity_dir}, clear=False), \
                    patch.object(workflow_app.filedialog, "asksaveasfilename") as chooser:
                self.assertTrue(app._save_app_stage_plan(plan, "stack-reference-plan.json", "stack reference"))

            self.assertEqual(app.plan_path.parent, instance / "plans")
            self.assertTrue(app.plan_path.is_file())
            self.assertFalse(app.plan_from_intake)
            self.assertFalse(app.form_editable)
            chooser.assert_not_called()

    def test_reviewed_pad_choices_create_record_plan_with_a_human_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "machine.json"
            profile.write_text("{}\n", encoding="utf-8")
            instance = root / "devices" / "MAC-ABCD1234" / "episodes" / "S01E12"
            instance.mkdir(parents=True)
            manifest = instance / "manifest.json"
            manifest.write_text(json.dumps({"title": "Vortex"}), encoding="utf-8")
            evidence = root / "probe"
            evidence.mkdir()
            report_path = instance / "plans" / "decision.json"
            report_path.parent.mkdir()
            report_path.write_text("{}\n", encoding="utf-8")
            for name in ("audit.tsv", "repair.txt", "fullres.tsv"):
                (evidence / name).write_text("fixture\n", encoding="utf-8")
            report = {
                "identity": "DS9:S01E12", "review_note": "visual review confirmed dim scene",
                "manifest_path": str(manifest.resolve()),
                "candidates": [{"frame": 54922, "classification": "rife"}],
                "evidence": {
                    "pad_audit": str(evidence / "audit.tsv"),
                    "repair_frames": str(evidence / "repair.txt"),
                    "fullres_report": str(evidence / "fullres.tsv"),
                },
            }
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = None
            app.values = {"fingerprint_policy": FakeVariable("full")}
            app._current_episode_context = lambda: (
                "S01E12", "DS9", str(profile), str(manifest), str(instance))
            saved = []
            app._save_app_stage_plan = lambda plan, filename, scope: saved.append(
                (plan, filename, scope)) or True
            launched = []
            app._launch = lambda action: launched.append(action)
            app.status = FakeVariable()

            self.assertTrue(app._save_pad_classification_plan(report_path, "a" * 64, report))

            plan, filename, scope = saved[0]
            self.assertEqual(filename, "DS9-S01E12-pad-classification-plan.json")
            self.assertEqual(plan["fingerprint_policy"], "full")
            self.assertEqual(plan["steps"][0]["stage"], "record-pad-classifications")
            self.assertIn("--apply", plan["steps"][0]["args"])
            self.assertEqual(len(plan["steps"]), 1)
            self.assertTrue(app.auto_start_rife_after_pad_record)
            self.assertTrue(app.auto_run_after_preview)
            self.assertEqual(launched, ["preview"])

    def test_default_machine_profile_is_created_under_unique_device_folder(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.values = {"profile": FakeVariable()}
        app.profile_note = FakeLabel()
        app.profile_path = None
        expected = (workflow_app.ROOT / "devices" / "LENOVO-LEGION-5-1234ABCD" /
                    "local device config" / "machine.json")

        with patch.object(workflow_app, "create_app_device_profile",
                          return_value=(expected, False)) as create_profile:
            selected = app._prepare_default_profile()

        self.assertEqual(selected, expected)
        self.assertEqual(app.profile_path, expected)
        self.assertEqual(app.values["profile"].get(), str(expected))
        create_profile.assert_called_once_with(workflow_app.ROOT)
        self.assertIn("machine.json", app.profile_note.options["text"])

    def test_local_folder_selection_resolves_and_populates_episode_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary) / "Voyager captures"
            source_dir.mkdir()
            selected_path = source_dir / "VOY-S01E01-Caretaker-L1.tbc"
            fields = {
                "source_mode": FakeVariable("archive-url"),
                "confirmed_title": FakeVariable("stale title"),
                "series": FakeVariable("DS9"),
                "local_source_path": FakeVariable(),
                "downloads_dir": FakeVariable(),
                "archive_url": FakeVariable("https://archive.org/details/old-item"),
            }
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = None
            app.values = fields
            app.source_mode_var = FakeVariable("archive-url")
            app.previous_source_mode = "archive-url"
            app._suppress_archive_trace = False
            app._update_source_controls = lambda: None
            resolved = []
            app._finish_episode_resolution = lambda *args, **kwargs: resolved.append((args, kwargs))
            inventory = {
                "raw_rf_files": [], "series_inferred": True, "series": "VOY",
                "episode": "S01E01", "selected_path": str(selected_path),
                "source_dir": str(source_dir),
            }

            with patch.object(workflow_app, "resolve_local_source", return_value=inventory) as resolver:
                app._local_source_selected(source_dir)

            resolver.assert_called_once_with(source_dir)
            self.assertEqual(app.source_mode_var.get(), "local")
            self.assertEqual(fields["series"].get(), "VOY")
            self.assertEqual(fields["local_source_path"].get(), str(selected_path))
            self.assertEqual(fields["downloads_dir"].get(), str(source_dir))
            self.assertEqual(fields["archive_url"].get(), "")
            self.assertEqual(fields["confirmed_title"].get(), "")
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0][0][1], "S01E01")
            self.assertEqual(resolved[0][1]["series"], "VOY")


if __name__ == "__main__":
    unittest.main()
