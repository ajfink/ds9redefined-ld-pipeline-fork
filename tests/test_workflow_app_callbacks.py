from __future__ import annotations

import sys
import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / 'tests'))
from support import temporary_directory  # noqa: E402

import workflow_app  # noqa: E402
import workflow_app_process  # noqa: E402
from workflow_app_model import WorkflowAppError, read_intake_template  # noqa: E402
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
    def test_dependency_update_button_opens_profile_bound_popup_and_tracks_busy_state(self):
        with temporary_directory() as temporary:
            profile = Path(temporary) / "machine.json"
            profile.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.dependency_update_window = None
            app.dependency_updates_busy = False
            app.profile_path = profile
            app.plan = None
            app.values = {}
            app.process = None
            app.root = object()
            state = []
            app._set_running = lambda running: state.append(running)

            class FakeWindow:
                def __init__(self, parent, selected_profile, busy_changed):
                    self.parent = parent
                    self.profile = selected_profile
                    self.busy_changed = busy_changed
                    self.window = SimpleNamespace(winfo_exists=lambda: True, lift=lambda: None)

            with patch.object(workflow_app, "DependencyUpdateWindow", FakeWindow):
                app._open_dependency_updates()
            self.assertEqual(app.dependency_update_window.profile, profile.resolve())
            app.dependency_update_window.busy_changed(True)
            self.assertTrue(app.dependency_updates_busy)
            self.assertEqual(state, [False])
            app.dependency_update_window.busy_changed(False)
            self.assertFalse(app.dependency_updates_busy)

    def test_intake_template_placeholders_are_not_displayed_as_selections(self):
        self.assertEqual(workflow_app._form_value_from_template("REPLACE_WITH_no-dvd"), "")
        self.assertEqual(workflow_app._form_value_from_template("REPLACE_WITH_use_or_skip"), "")
        self.assertEqual(workflow_app._form_value_from_template("YOUR_ACCOUNT"), "")
        self.assertEqual(workflow_app._form_value_from_template("no-dvd"), "no-dvd")
        self.assertEqual(workflow_app._form_value_from_template("skip"), "skip")

    def test_intake_plan_save_suggests_next_unused_immutable_filename(self):
        with temporary_directory() as temporary:
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
        with temporary_directory() as temporary:
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
        with temporary_directory() as temporary:
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

    def test_existing_manifest_preview_saves_and_previews_fresh_annotated_plan(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        plans_dir = Path(temporary.name) / "plans"
        plans_dir.mkdir()
        existing_plan = plans_dir / "DS9-S01E18-stack-reference-plan.json"
        existing_plan.write_text("{}\n", encoding="utf-8")
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        source = Path("Z:/DS9 S01E18 LaserDisc")
        profile = Path("Z:/device/local device config/machine.json")
        manifest = Path("Z:/device/episodes/S01E18/manifest/S01E18.json")
        instance = manifest.parents[1]
        app.root = None
        app.process = None
        app.template = {"schema": "test-intake-template"}
        app.form_editable = True
        app.plan_from_intake = True
        app.plan = {
            "episode": "S01E17", "profile": "old-profile.json",
            "_fromstuntokill_intake": {
                "episode": {"value": "S01E17"},
                "manifest_path": {"value": "old-manifest.json"},
            },
        }
        app.template_path = Path("intake-template.json")
        app.profile_path = profile
        app.values = {key: FakeVariable(value) for key, value in {
            "source_mode": "local", "local_source_path": str(source), "archive_url": "",
            "episode": "S01E18", "series": "DS9", "profile": str(profile),
            "manifest_path": str(manifest), "instance_root": str(instance),
            "fingerprint_policy": "balanced",
        }.items()}
        app.resolved_archive_url = f"local:{source}"
        app.status = FakeVariable()
        app._collect_values = lambda: {}
        captured = {}
        app._save_app_stage_plan = lambda plan, filename, scope: captured.update(
            saved_plan=plan, filename=filename, scope=scope) or True
        previews = []
        app._launch = lambda action: previews.append(action)
        context = {"phase": "reference", "identity": "DS9:S01E18"}

        with patch.object(workflow_app, "build_intake_plan",
                          side_effect=WorkflowAppError(
                              f"refusing to target an existing manifest: {manifest}")), \
                patch.object(workflow_app, "inspect_stack_console", return_value=context) as inspect, \
                patch.object(workflow_app, "build_stack_console_plan",
                             return_value={"steps": [{"stage": "stack-reference"}],
                                           "decisions": []}) as build, \
                patch.object(workflow_app, "_episode_plan_directory", return_value=plans_dir), \
                patch.object(workflow_app, "_existing_reference_plan",
                             return_value=existing_plan) as find_existing:
            self.assertFalse(app._save_intake_plan())

        inspect.assert_called_once_with(profile, manifest, "S01E18", instance, series="DS9")
        build.assert_called_once_with(context, fingerprint_policy="balanced",
                                      restart_completed=False)
        find_existing.assert_called_once_with(plans_dir, "S01E18", "DS9")
        self.assertEqual(captured["filename"], "DS9-S01E18-stack-reference-plan.json")
        self.assertEqual(captured["saved_plan"]["decisions"][-1]["value"],
                         f"Note - existing reference plan for this episode already exists at {existing_plan}.")
        self.assertEqual(previews, ["preview"])
        self.assertIn("Existing manifest preserved", app.status.get())

    def test_existing_reference_plan_finder_uses_series_and_stage_identity(self):
        with temporary_directory() as temporary:
            plans = Path(temporary)
            other = plans / "other.json"
            other.write_text(json.dumps({"episode": "S01E18", "series": "VOY",
                                         "steps": [{"stage": "stack-reference"}]}), encoding="utf-8")
            matching = plans / "reference.json"
            matching.write_text(json.dumps({"episode": "S01E18", "series": "DS9",
                                            "steps": [{"stage": "stack-reference"}]}), encoding="utf-8")
            self.assertEqual(workflow_app._existing_reference_plan(plans, "S01E18", "DS9"),
                             matching.resolve())

    def test_stable_efm_wav_report_log_auto_starts_provenance_verification(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            audio = root / "audio"
            episode_audio = audio / "S01E18"
            episode_audio.mkdir(parents=True)
            instance = root / "episode"
            logs = instance / "logs"
            logs.mkdir(parents=True)
            wav = episode_audio / "L1-efm.wav"
            wav.write_bytes(b"wave fixture")
            Path(str(wav) + ".txt").write_text("EFMSink report", encoding="utf-8")
            (logs / "orc-L1-decode.log").write_text(
                f"EFMSink: Success: EFM decoded to {wav}\n", encoding="utf-8")
            context = {
                "identity": "DS9:S01E18", "episode": "S01E18", "audio_root": audio,
                "instance": instance, "captures": [{"label": "L1"}],
                "manifest_data": {"audio": {"efm_provenance": {}}},
            }
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = SimpleNamespace(after=lambda *_args: None, winfo_exists=lambda: True)
            app.process = None
            app.efm_verification_dialog = None
            app.efm_auto_scan_state = {}
            app.efm_auto_attempted = {}
            app._efm_episode_context = lambda: context
            launches = []
            app._verify_efm_capture = lambda **kwargs: launches.append(kwargs)

            app._poll_efm_decode_outputs()
            self.assertEqual(launches, [])
            app._poll_efm_decode_outputs()

            self.assertEqual(launches, [{"capture_hint": "L1", "wav_hint": str(wav),
                                         "orc_log_hint": str(logs / "orc-L1-decode.log"),
                                         "auto_start": True}])

    def test_efm_auto_selection_requires_a_unique_pareto_dominant_quality_report(self):
        def row(label, values):
            metrics = dict(zip(("post_c2_data_loss_pct", "c2_uncorrectable_pct",
                                "c1_uncorrectable_pct", "missing_sections",
                                "concealed_samples", "muted_samples"), values))
            return (label, Path(f"{label}.json"), "a" * 64, metrics,
                    Path(f"{label}.txt"), Path(f"{label}.wav"))

        automatic = workflow_app.WorkflowApp._unique_pareto_efm_capture([
            row("L1", (0.1, 0.01, 0.1, 0, 0, 0)),
            row("M1", (0.2, 0.02, 0.2, 1, 10, 2)),
        ])
        self.assertEqual(automatic[0], "L1")
        self.assertIn("Pareto-dominant", automatic[1])
        only_capture = workflow_app.WorkflowApp._unique_pareto_efm_capture([
            row("P1", (0.1, 0.01, 0.1, 0, 0, 0)),
        ])
        self.assertEqual(only_capture[0], "P1")
        self.assertIn("only available", only_capture[1])
        self.assertIsNone(workflow_app.WorkflowApp._unique_pareto_efm_capture([
            row("L1", (0.1, 0.03, 0.1, 0, 0, 0)),
            row("M1", (0.2, 0.01, 0.2, 1, 10, 2)),
        ]))

    def test_loading_a_recovery_candidate_leaves_resume_enabled(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            plan_path = root / "incomplete-plan.json"
            plan_path.write_text(json.dumps({
                "schema": "ld-pipeline-workflow-plan-v1", "episode": "S01E18",
                "series": "DS9", "profile": str(root / "machine.json"),
                "manifest_path": str(root / "S01E18.json"), "instance_root": str(root),
                "fingerprint_policy": "balanced",
                "steps": [{"id": "validate", "stage": "validate-inputs",
                           "inputs": [str(root / "input")], "outputs": []}],
            }), encoding="utf-8")
            profile = root / "machine.json"
            profile.write_text("{}\n", encoding="utf-8")
            manifest = root / "S01E18.json"
            manifest.write_text(json.dumps({
                "episode": "S01E18", "series": "DS9", "title": "Dramatis Personae",
                "source_provenance": {"mode": "local-inventory", "fingerprint_policy": "balanced"},
                "roots": {"downloads": "Z:/DS9 S01E18 LaserDisc"},
                "files": {"dvd": {"path": "Z:/Video/Dramatis Personae.mkv"}},
                "dvd_reference_available": True, "cut_pad_images_choice": "skip",
            }), encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = None
            app.plan_path = None
            app.plan = None
            app.values = {key: FakeVariable() for key in (
                "episode", "series", "profile", "manifest_path", "instance_root",
                "archive_url", "fingerprint_policy", "source_mode", "local_source_path",
                "downloads_dir", "confirmed_title", "dvd_mode", "dvd_path", "cut_pad_images")}
            app.values["series"].set("DS9")
            app.profile_note = FakeLabel()
            app.episode_display = FakeVariable()
            app.manifest_display = FakeVariable()
            app.source_label = FakeLabel()
            app.status = FakeVariable()
            app.resume_button = FakeLabel()
            app._reset_post_run_cleanup = lambda: None
            app._set_form_editable = lambda _editable: None
            app._update_review_choices = lambda: None
            app._update_mode_controls = lambda: None
            app._set_running = lambda running: app.resume_button.configure(
                state="disabled" if running else "normal")
            app._intake_plan_has_passed = lambda: False
            app.opencode_help_results = {}
            app._suppress_archive_trace = False
            app._efm_episode_context = lambda: {
                "downloads": Path("Z:/DS9 S01E18 LaserDisc"),
                "file_paths": {"dvd": "Z:/Video/Dramatis Personae.mkv"}}
            recovery = {"status": "BLOCKED", "last_completed_step": "stack-reference",
                        "last_completed_utc": "2026-11-17T22:43:40+00:00",
                        "profile_matches": True}

            self.assertTrue(app._load_saved_plan(plan_path, recovery=recovery))
            self.assertEqual(app.resume_button.options["state"], "normal")
            self.assertIn("Click Resume", app.status.get())
            self.assertEqual(app.values["source_mode"].get(), "local")
            self.assertEqual(app.values["local_source_path"].get(), str(Path("Z:/DS9 S01E18 LaserDisc")))
            self.assertEqual(app.values["downloads_dir"].get(), str(Path("Z:/DS9 S01E18 LaserDisc")))
            self.assertEqual(app.values["confirmed_title"].get(), "Dramatis Personae")
            self.assertEqual(app.values["dvd_path"].get(), "Z:/Video/Dramatis Personae.mkv")

    def test_startup_recovery_check_uses_current_device_profile(self):
        with temporary_directory() as temporary:
            profile = Path(temporary) / "machine.json"
            profile.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.profile_path = profile
            app.incomplete_jobs_checked = False
            app.status = FakeVariable()
            candidates = [{"job_id": "unfinished"}]
            shown = []
            app._show_incomplete_workflows = lambda rows: shown.extend(rows)
            with patch.object(workflow_app, "discover_incomplete_workflows",
                              return_value=candidates) as discover:
                app._check_incomplete_workflows()
            discover.assert_called_once_with(profile, workflow_app.ROOT)
            self.assertEqual(shown, candidates)
            self.assertTrue(app.incomplete_jobs_checked)

    def test_console_initial_geometry_is_adjacent_and_matches_main_height(self):
        self.assertEqual(
            workflow_app.WorkflowApp._console_geometry(40, 55, 1000, 780),
            "620x780+1048+55")

    def test_root_jpeg_is_installed_as_the_default_taskbar_icon(self):
        icon_photo = object()

        class IconRoot:
            def __init__(self):
                self.icon = None
            def iconphoto(self, default, image):
                self.icon = (default, image)

        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.root = IconRoot()
        app.window_icon_photo = None
        app.window_icon_error = ""
        with patch("PIL.ImageTk.PhotoImage", return_value=icon_photo) as photo_factory:
            app._set_window_icon()

        photo_factory.assert_called_once()
        self.assertEqual(app.root.icon, (True, icon_photo))
        self.assertIs(app.window_icon_photo, icon_photo)
        self.assertEqual(app.window_icon_error, "")

    def test_run_after_form_edits_saves_and_previews_before_execution(self):
        with temporary_directory() as temporary:
            old_plan = Path(temporary) / "old-plan.json"
            new_plan = Path(temporary) / "new-plan.json"
            old_plan.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.plan_path = old_plan
            app.plan = {"steps": []}
            app.template = {"schema": "intake-template"}
            app.process = None
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
            self.assertTrue(app.auto_run_after_preview)
            self.assertIn(str(new_plan), command)
            self.assertNotIn("--execute", command)

    def test_run_on_an_unpreviewed_saved_plan_automatically_previews_first(self):
        with temporary_directory() as temporary:
            plan_path = Path(temporary) / "saved-plan.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.plan_path = plan_path
            app.plan = {"steps": [{"stage": "validate-inputs"}]}
            app.template = None
            app.plan_from_intake = False
            app.previewed_plan_hash = None
            app.status = FakeVariable()
            app.root = object()
            app.process = None
            launched = []
            app._start_process = lambda command, action: launched.append((command, action))
            app._logging_args = lambda: []
            app._require_preview = lambda: False

            app._launch("run")

            self.assertEqual(launched[0][1], "preview")
            self.assertTrue(app.auto_run_after_preview)

    def test_resume_on_a_loaded_saved_run_starts_resume_without_an_extra_plan_preview(self):
        with temporary_directory() as temporary:
            plan_path = Path(temporary) / "recovered-plan.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.plan_path = plan_path
            app.plan = {"steps": [{"stage": "stack-reference"}]}
            app.template = None
            app.plan_from_intake = False
            app.process = None
            app.status = FakeVariable()
            app._resume_paused_process = lambda: False
            app._start_process = lambda command, action: launched.append((command, action))
            app._logging_args = lambda: ["--verbose"]
            app._require_preview = lambda: self.fail("Resume must not require a separate preview click")
            launched = []

            app._launch("resume")

            self.assertEqual(len(launched), 1)
            command, action = launched[0]
            self.assertEqual(action, "resume")
            self.assertIn("--execute", command)
            self.assertIn("--resume", command)
            self.assertNotIn("preview", command)

    def test_resume_click_continues_a_paused_process_without_restarting_cli(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.process_paused = True
        app.process_pause_token = object()
        app.process = SimpleNamespace(poll=lambda: None)
        app.pending_action = "run"
        app.process_status_var = FakeVariable()
        app.status = FakeVariable()
        app._set_running = lambda _running: None
        resumed = []
        with patch.object(workflow_app_process, "resume_process_tree",
                          side_effect=lambda token: resumed.append(token)):
            app._launch("resume")
        self.assertEqual(len(resumed), 1)
        self.assertFalse(app.process_paused)
        self.assertIsNone(app.process_pause_token)
        self.assertIn("continuing in place", app.status.get())

    def test_pause_is_enabled_only_for_an_active_stage_run(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        class Button:
            def __init__(self):
                self.state = None
            def configure(self, **kwargs):
                self.state = kwargs.get("state", self.state)
        button_names = ("run_button", "approve_button", "pad_review_button", "pad_classify_button",
                        "episode_status_button", "dependency_update_button", "stage_console_button",
                        "review_retime_button", "verify_efm_button", "workbench_button",
                        "open_profile_button", "open_template_button", "open_plan_button",
                        "pause_button", "resume_button", "select_efm_button", "prepare_retime_button",
                        "verbose_logging_check", "process_snapshot_button")
        for name in button_names:
            setattr(app, name, Button())
        app.dependency_updates_busy = False
        app.process_paused = False
        app.process = SimpleNamespace(poll=lambda: None)
        app.pending_action = "preview"
        app.active_process_pid = 4321
        app.post_run_cleanup_ready = False
        app.post_run_cleanup_button = Button()
        app._set_running(True)
        self.assertEqual(app.pause_button.state, "disabled")
        app.pending_action = "run"
        app._set_running(True)
        self.assertEqual(app.pause_button.state, "normal")
        self.assertEqual(app.resume_button.state, "disabled")
        app.process_paused = True
        app._set_running(True)
        self.assertEqual(app.pause_button.state, "disabled")
        self.assertEqual(app.resume_button.state, "normal")

    def test_successful_efm_verification_auto_opens_human_selection_when_ready(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = {"steps": [{"stage": "verify-efm-capture"}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._auto_select_efm_capture_if_ready = lambda: opened.append(True)
        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    def test_recorded_efm_selection_starts_audio_evidence_suite(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = {"steps": [{"stage": "record-efm-capture"}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._auto_start_audio_evidence = lambda: opened.append(True)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    def test_audio_evidence_suite_completion_opens_structured_review_ui(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        plan = {"decisions": [{"id": "audio-evidence-suite"}], "steps": []}
        app.plan = plan
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._review_audio_timing_evidence = lambda value: opened.append(value)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [plan])

    def test_alignment_sample_completion_opens_visual_audio_review(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        sample_dir = Path("C:/episode/probe/alignment-samples")
        index = sample_dir / "index.json"
        app.plan = {"steps": [{"stage": "alignment-samples", "outputs": [str(sample_dir)]}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._review_alignment_samples = lambda value: opened.append(value)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [index])

    def test_alignment_assets_completion_opens_hash_bound_visual_review(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        review_dir = Path("C:/episode/probe/review")
        app.plan = {"steps": [{"stage": "alignment-review-assets", "outputs": [str(review_dir)]}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._review_dvd_alignment = lambda value: opened.append(value)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [review_dir / "index.json"])

    def test_alignment_review_record_completion_routes_approval(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = {
            "decisions": [{"id": "alignment-visual-decision", "value": "APPROVED"}],
            "steps": [{"stage": "record-dvd-alignment-review"}],
        }
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._after_dvd_alignment_review = lambda decision: opened.append(decision)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, ["APPROVED"])

    def test_trim_review_record_completion_starts_gated_cadence_plan(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        self._prepare_completed_review_app(app, {
            "steps": [{"stage": "record-video-trim-review"}],
        })
        opened = []
        app._auto_start_cadence_evidence = lambda: opened.append(True)
        app._drain_output()
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    def test_cadence_assets_completion_opens_visual_review_ui(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        review_dir = Path("C:/episode/probe/cadence-review")
        self._prepare_completed_review_app(app, {
            "steps": [{"stage": "cadence-review-assets", "outputs": [str(review_dir)]}],
        })
        opened = []
        app._review_cadence_evidence = lambda path: opened.append(path)
        app._drain_output()
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [review_dir / "index.json"])

    def test_approved_cadence_metrics_allow_progress_without_starting_final_encode(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        self._prepare_completed_review_app(app, {
            "decisions": [{"id": "cadence-visual-decision", "value": "APPROVED"}],
            "steps": [{"stage": "record-cadence-review"}],
        })
        opened = []
        app._cadence_review_approved = lambda: opened.append(True)
        app._drain_output()
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    @staticmethod
    def _prepare_completed_review_app(app, plan):
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = plan
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None

    def test_audio_evidence_runs_report_only_before_auto_alignment_then_reruns_after_approval(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.process = None
        app._efm_episode_context = lambda: {
            "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            "manifest_data": {
                "dvd_reference_available": True,
                "audio": {"capture": "P1", "pad_frames": 0},
            },
        }
        app._dvd_alignment_is_approved = lambda _context: False
        app.values = {"fingerprint_policy": FakeVariable("balanced")}
        app.status = FakeVariable()
        app._save_app_stage_plan = lambda *_args: True
        launched = []
        app._launch = lambda value: launched.append(value)
        alignment_started = []
        app._auto_start_dvd_alignment = lambda: alignment_started.append(True)
        with patch.object(workflow_app, "build_audio_evidence_plan",
                          return_value={"steps": [], "decisions": []}):
            app._auto_start_audio_evidence()
        self.assertTrue(app.auto_start_dvd_alignment_after_samples)
        self.assertEqual(launched, ["preview"])
        self.assertEqual(alignment_started, [])

        app._dvd_alignment_is_approved = lambda _context: True
        app.auto_open_retime_after_alignment_samples = True
        with patch.object(workflow_app, "build_audio_evidence_plan",
                          return_value={"steps": [], "decisions": []}):
            app._auto_start_audio_evidence()
        self.assertFalse(app.auto_start_dvd_alignment_after_samples)
        self.assertTrue(app.auto_open_retime_after_alignment_samples)
        self.assertEqual(launched, ["preview", "preview"])

    def test_no_dvd_audio_promotion_completion_continues_to_delivery_workbench(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = {"steps": [{"stage": "audio-promote-no-dvd"}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._continue_to_audio_delivery = lambda: opened.append(True)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    def test_encode_validation_dispatches_to_audio_delivery_continuation(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = {"steps": [{"stage": "encode-validate"}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._continue_to_audio_delivery = lambda: opened.append(True)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    def test_stack_visual_approval_automatically_starts_efm_decode_plan(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "approve"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        app.plan = {
            "steps": [
                {"stage": "render-stack"},
                {"kind": "review", "id": "review-stack-evidence"},
            ],
        }
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._auto_start_efm_decode = lambda: opened.append(True)

        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [True])

    def test_successful_retime_report_plan_auto_opens_its_own_report(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "approve"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.post_run_cleanup_ready = False
        report = Path("retime-report.json")
        app.plan = {"steps": [{"stage": "prepare-retime-breaks", "outputs": [str(report)]}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        opened = []
        app._review_retime_break_candidates = lambda path: opened.append(path)
        app._drain_output()
        self.assertEqual(len(app.root.idle_callbacks), 1)
        app.root.idle_callbacks[0]()
        self.assertEqual(opened, [report])

    def test_successful_cadence_check_and_rife_stage_auto_open_visual_gates(self):
        for stage, expected in (("fullres-check", "classification"),
                                ("rife-replacements", "interpolated")):
            with self.subTest(stage=stage):
                app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
                app.output_queue = queue.Queue()
                app.output_queue.put("__WORKFLOW_DONE__0")
                app.pending_action = "run"
                app.pending_plan_hash = None
                app.pending_review_step = None
                app.waiting_review_step = None
                app.auto_skipped_review_steps = set()
                app.auto_advance_after_run = False
                app.auto_run_after_preview = False
                app.auto_resume_after_preview = False
                app.auto_start_rife_after_pad_record = False
                app.process = object()
                app.process_paused = False
                app.process_pause_token = None
                app.root = FakeRoot()
                app.status = FakeVariable()
                app.process_status_var = FakeVariable()
                app.process_snapshot_button = FakeLabel()
                app.post_run_cleanup_ready = False
                app.plan = {"steps": [{"stage": stage}]}
                app._set_running = lambda _running: None
                app._cleanup_pad_previews = lambda: None
                opened = []
                app._classify_pad_candidates = lambda: opened.append("classification")
                app._review_pad_frames = lambda **kwargs: opened.append(
                    "interpolated" if kwargs.get("auto_continue") else "manual")

                app._drain_output()
                self.assertEqual(len(app.root.idle_callbacks), 1)
                app.root.idle_callbacks[0]()
                self.assertEqual(opened, [expected])

    def test_completed_reference_stage_auto_starts_next_stack_stage_without_report_review(self):
        app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
        app.output_queue = queue.Queue()
        app.output_queue.put("__WORKFLOW_DONE__0")
        app.pending_action = "run"
        app.pending_plan_hash = None
        app.pending_review_step = None
        app.waiting_review_step = None
        app.auto_skipped_review_steps = set()
        app.auto_advance_after_run = False
        app.auto_run_after_preview = False
        app.auto_resume_after_preview = False
        app.auto_start_rife_after_pad_record = False
        app.process = object()
        app.process_paused = False
        app.process_pause_token = None
        app.root = FakeRoot()
        app.status = FakeVariable()
        app.process_status_var = FakeVariable()
        app.process_snapshot_button = FakeLabel()
        app.process_output_tail = []
        app.post_run_cleanup_ready = False
        app.plan = {"steps": [{"stage": "stack-reference"}]}
        app._set_running = lambda _running: None
        app._cleanup_pad_previews = lambda: None
        app._append_output = lambda _line: None
        continued = []
        app._auto_start_stack_reference = lambda: continued.append(True)

        app._drain_output()

        self.assertIn("starting the next stack step", app.status.get())
        self.assertTrue(app.root.idle_callbacks)
        app.root.idle_callbacks[0]()
        self.assertEqual(continued, [True])

    def test_successful_intake_run_is_marked_for_automatic_stack_continuation(self):
        with temporary_directory() as temporary:
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
        with temporary_directory() as temporary:
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
        with temporary_directory() as temporary, temporary_directory() as identity_dir:
            bundle = Path(temporary) / "app"
            bundle.mkdir()
            (bundle / "machine.example.json").write_bytes(
                (workflow_app.ROOT / "machine.example.json").read_bytes())
            (bundle / "WORKFLOW_VERSION").write_text("15\n", encoding="ascii")
            with patch.dict("os.environ", {"LDP_DEVICE_ID_DIR": str(identity_dir)}, clear=False), \
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
            app.template = {"schema": "test"}
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
                    patch.dict("os.environ", {"LDP_DEVICE_ID_DIR": str(identity_dir)}, clear=False), \
                    patch.object(workflow_app.filedialog, "asksaveasfilename") as chooser:
                self.assertTrue(app._save_app_stage_plan(plan, "stack-reference-plan.json", "stack reference"))

            self.assertEqual(app.plan_path.parent, instance / "plans")
            self.assertTrue(app.plan_path.is_file())
            self.assertFalse(app.plan_from_intake)
            self.assertFalse(app.form_editable)
            chooser.assert_not_called()

    def test_stage_console_auto_continues_preserved_orc_reference_without_a_choice(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            profile, manifest = root / "machine.json", root / "S01E18.json"
            project, report = root / "stack.orcprj", root / "selection-report.md"
            profile.write_text("{}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            report.write_text("verified reference report\n", encoding="utf-8")
            context = {
                "phase": "reference-complete", "identity": "DS9:S01E18",
                "completed_stage": {"stage": "stack-reference", "job_id": "known-good-job",
                                    "outputs": [str(project), str(report)]},
                "measurement_requirements": [
                    "record the measured alignment_map",
                    "record the measured positive stack.frames"],
                "project": project, "master": root / "stack.mov",
            }
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = object()
            app.process = None
            app.plan = {"episode": "S01E18", "series": "DS9", "profile": str(profile),
                        "manifest_path": str(manifest), "instance_root": str(root)}
            app.form_editable = False
            app.template = None
            app.values = {key: FakeVariable(value) for key, value in {
                "episode": "S01E18", "series": "DS9", "profile": str(profile),
                "manifest_path": str(manifest), "instance_root": str(root),
                "fingerprint_policy": "balanced",
            }.items()}
            app.status = FakeVariable()
            app._resume_completed_stack_job_if_needed = lambda _completed: False
            app._completed_stack_stage_has_unpassed_review = lambda _completed: False
            app._save_app_stage_plan = lambda plan, name, scope: (
                saved.append((plan, name, scope)) or True)
            app._launch = lambda action: launched.append(action)
            app.auto_run_after_preview = False
            saved, launched = [], []

            with patch.object(workflow_app, "inspect_stack_console", return_value=context), \
                    patch.object(workflow_app.messagebox, "askyesnocancel") as choose_continue_or_restart, \
                    patch.object(workflow_app.messagebox, "showinfo") as show_information, \
                    patch.object(workflow_app, "build_orc_reference_measurement_plan",
                                 return_value={"steps": [{"stage": "record-orc-reference-measurements"}],
                                               "decisions": []}) as build_plan:
                app._stage_console()

            choose_continue_or_restart.assert_not_called()
            show_information.assert_not_called()
            build_plan.assert_called_once_with(context, "balanced")
            self.assertEqual(launched, ["preview"])
            self.assertTrue(app.auto_run_after_preview)
            self.assertIn("Recording measured Orc alignment/frame coverage", app.status.get())
            self.assertEqual(saved[0][1], "DS9-S01E18-orc-measurements-plan.json")

    def test_run_uses_selected_episode_to_continue_from_verified_stack_reference(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            profile, manifest = root / "machine.json", root / "S01E18.json"
            profile.write_text("{}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.form_editable = True
            app.template = {"schema": "test"}
            app.values = {key: FakeVariable(value) for key, value in {
                "episode": "S01E18", "series": "DS9", "profile": str(profile),
                "manifest_path": str(manifest), "instance_root": str(root),
            }.items()}
            app.plan = None
            app.plan_path = None
            app.status = FakeVariable()
            context = {"phase": "reference-complete", "identity": "DS9:S01E18"}
            continued = []

            with patch.object(workflow_app, "discover_incomplete_workflows", return_value=[]), \
                    patch.object(workflow_app, "inspect_stack_console", return_value=context), \
                    patch.object(app, "_auto_continue_stack_stage",
                                 side_effect=lambda value: continued.append(value)):
                self.assertTrue(app._auto_resume_or_continue_selected_episode())

            self.assertEqual(continued, [context])

    def test_run_resumes_latest_matching_workspace_job_without_recovery_picker(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            profile, manifest = root / "machine.json", root / "S01E18.json"
            instance = root / "episode-instance"
            profile.write_text("{}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.form_editable = True
            app.template = {"schema": "test"}
            app.values = {key: FakeVariable(value) for key, value in {
                "episode": "S01E18", "series": "DS9", "profile": str(profile),
                "manifest_path": str(manifest), "instance_root": str(instance),
            }.items()}
            app.status = FakeVariable()
            loaded, actions = [], []
            candidate = {
                "resumable": True, "episode": "S01E18", "series": "DS9",
                "plan": {"instance_root": str(instance)}, "plan_path": root / "saved-plan.json",
                "last_completed_step": "stack-reference",
            }
            with patch.object(workflow_app, "discover_incomplete_workflows",
                              return_value=[candidate]), \
                    patch.object(app, "_load_saved_plan",
                                 side_effect=lambda path, recovery=None: loaded.append(
                                     (path, recovery)) or True), \
                    patch.object(app, "_launch", side_effect=lambda action: actions.append(action)):
                self.assertTrue(app._auto_resume_or_continue_selected_episode())
            self.assertEqual(loaded, [(candidate["plan_path"], candidate)])
            self.assertEqual(actions, ["resume"])
            self.assertIn("stack-reference", app.status.get())

    def test_stage_console_explicit_restart_builds_overwrite_plan(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            profile, manifest = root / "machine.json", root / "S01E18.json"
            project, master = root / "stack.orcprj", root / "stack.mov"
            profile.write_text("{}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            context = {
                "phase": "reference-complete", "identity": "DS9:S01E18",
                "completed_stage": {"stage": "stack-reference", "job_id": "known-good-job",
                                    "outputs": [str(manifest), str(project)]},
                "measurement_requirements": ["record measured alignment_map"],
                "project": project, "master": master,
            }
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.root = object()
            app.process = None
            app.plan = {"episode": "S01E18", "series": "DS9", "profile": str(profile),
                        "manifest_path": str(manifest), "instance_root": str(root)}
            app.form_editable = False
            app.template = None
            app.values = {key: FakeVariable(value) for key, value in {
                "episode": "S01E18", "series": "DS9", "profile": str(profile),
                "manifest_path": str(manifest), "instance_root": str(root),
                "fingerprint_policy": "balanced",
            }.items()}
            app.status = FakeVariable()
            saved, launched, built = [], [], []
            app._save_app_stage_plan = lambda plan, name, scope: (
                saved.append((plan, name, scope)) or True)
            app._launch = lambda action: launched.append(action)
            app._logging_args = lambda: []

            with patch.object(workflow_app, "inspect_stack_console", return_value=context), \
                    patch.object(workflow_app, "build_stack_console_plan",
                                 side_effect=lambda *args, **kwargs: (
                                     built.append(kwargs) or {"steps": [{"stage": "stack-reference"}],
                                                      "decisions": []})), \
                    patch.object(workflow_app, "_episode_plan_directory", return_value=root / "plans"), \
                    patch.object(workflow_app, "_existing_reference_plan", return_value=None):
                app._stage_console(force_restart_completed=True)

            self.assertEqual(built, [{"fingerprint_policy": "balanced",
                                      "restart_completed": True}])
            self.assertEqual(saved[0][1], "DS9-S01E18-stack-reference-plan.json")
            self.assertEqual(launched, ["preview"])
            self.assertTrue(app.auto_run_after_preview)
            self.assertIn("back up", app.status.get())

    def test_archive_download_override_survives_resolution_but_resets_for_next_episode(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            profile = root / "machine.json"
            configured = root / "downloads"
            profile.write_text(json.dumps({
                "roots": {"downloads_root": str(configured),
                          "downloads_template": "DS9 {episode} LaserDisc"},
            }), encoding="utf-8")
            selected = root / "portable-drive" / "my-episode-downloads"
            app = workflow_app.WorkflowApp.__new__(workflow_app.WorkflowApp)
            app.source_mode_var = FakeVariable("archive-url")
            app.profile_path = profile
            app.values = {
                "downloads_dir": FakeVariable(""),
                "episode": FakeVariable("S01E17"),
                "series": FakeVariable("DS9"),
            }
            app._suppress_download_dir_trace = False
            app._download_destination_override = None
            app._download_destination_episode = None
            app._set_download_destination(str(selected), user_selected=True)

            app._set_profile_download_dir("S01E17", "DS9")
            self.assertEqual(app.values["downloads_dir"].get(), str(selected))

            app._set_profile_download_dir("S01E18", "DS9")
            self.assertEqual(app.values["downloads_dir"].get(),
                             str((configured / "DS9 S01E18 LaserDisc").resolve()))
            self.assertIsNone(app._download_destination_override)

    def test_reviewed_pad_choices_create_record_plan_with_a_human_gate(self):
        with temporary_directory() as temporary:
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
        with temporary_directory() as temporary:
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
