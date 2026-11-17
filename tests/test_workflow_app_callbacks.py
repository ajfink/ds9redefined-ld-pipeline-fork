from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workflow_app  # noqa: E402
from workflow_app_model import read_intake_template  # noqa: E402


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
