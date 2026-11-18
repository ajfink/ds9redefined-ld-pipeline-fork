from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dependency_updates  # noqa: E402
from dependency_lock import LockError  # noqa: E402
from dependency_update_window import DependencyUpdateWindow  # noqa: E402


class FakeStatusLabel:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class FakeText:
    def __init__(self):
        self.content = ""

    def configure(self, **_options):
        pass

    def insert(self, _index, text):
        self.content += text

    def index(self, _index):
        return f"{self.content.count(chr(10)) + 1}.0"

    def delete(self, _start, _end):
        self.content = ""

    def see(self, _index):
        pass


class FakeVariable:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeDialog:
    def grab_release(self):
        pass

    def destroy(self):
        pass


class FakeWindow:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class DependencyUpdateTests(unittest.TestCase):
    def test_python_package_row_marks_only_older_local_version_as_available(self):
        actual = {"packages": {"numpy": "1.0", "opencv-python": "4.0"},
                  "import_errors": {}, "python": "3.10.11"}
        with patch.object(dependency_updates, "_pypi_latest",
                          side_effect=lambda name, _python: {"numpy": "2.0", "opencv-python": "4.0"}[name]):
            rows = dependency_updates._pypi_rows([
                ("numpy", "1.0"), ("opencv-python", "4.0")], actual)
        self.assertTrue(rows[0]["available"])
        self.assertTrue(rows[0]["selectable"])
        self.assertFalse(rows[1]["available"])
        self.assertEqual(rows[0]["latest"], "2.0")

    def test_native_build_suffix_does_not_hide_a_release_update(self):
        self.assertEqual(dependency_updates._native_version_relation(
            "8.0.1-full_build-www.example", "8.1.2"), "older")
        self.assertEqual(dependency_updates._native_version_relation(
            "8.2.0-full_build", "8.1.2"), "newer")

    def test_selected_update_is_rechecked_before_install(self):
        report = {"ok": True, "interpreter": "python", "rows": [{
            "id": "pypi:numpy", "name": "numpy", "manager": "pip",
            "available": True, "selectable": True,
        }]}
        events = []

        def stream(_command, timeout, on_output, cancel_event=None):
            on_output("Downloading numpy")
            return 0, "Downloading numpy\n", False

        with patch.object(dependency_updates, "check_updates", return_value=report), \
                patch.object(dependency_updates, "_run_stream", side_effect=stream) as run:
            result = dependency_updates.install_selected(
                "machine.json", ["pypi:numpy"],
                progress=lambda item, event, text: events.append((item, event, text)))
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0][-3:], ["--upgrade", "--no-input", "numpy"])
        self.assertIn(("pypi:numpy", "status", "Updating..."), events)
        self.assertIn(("pypi:numpy", "output", "[numpy] Downloading numpy"), events)
        self.assertIn(("pypi:numpy", "status", "Updated!"), events)

    def test_popup_streams_installer_output_and_updates_package_status(self):
        popup = DependencyUpdateWindow.__new__(DependencyUpdateWindow)
        label = FakeStatusLabel()
        popup.status_labels = {"pypi:numpy": label}
        popup.notes = FakeText()
        popup._show_progress({"id": "pypi:numpy", "event": "status", "text": "Updating..."})
        popup._show_progress({"id": "pypi:numpy", "event": "output", "text": "[numpy] Downloading 2.0"})
        popup._show_progress({"id": "pypi:numpy", "event": "status", "text": "Updated!"})
        self.assertEqual(label.options["text"], "Updated!")
        self.assertIn("[numpy] Downloading 2.0", popup.notes.content)

    def test_stop_request_stops_current_installer_and_skips_queued_updates(self):
        report = {"ok": True, "interpreter": "python", "rows": [
            {"id": "pypi:numpy", "name": "numpy", "manager": "pip",
             "available": True, "selectable": True},
            {"id": "pypi:pillow", "name": "Pillow", "manager": "pip",
             "available": True, "selectable": True},
        ]}
        cancellation = threading.Event()
        events = []

        def stream(_command, timeout, on_output, cancel_event):
            on_output("Downloading")
            cancellation.set()
            return 130, "Installer stopped by user.", True

        with patch.object(dependency_updates, "check_updates", return_value=report), \
                patch.object(dependency_updates, "_run_stream", side_effect=stream) as run:
            result = dependency_updates.install_selected(
                "machine.json", ["pypi:numpy", "pypi:pillow"],
                progress=lambda item, event, text: events.append((item, event, text)),
                cancel_event=cancellation)
        self.assertTrue(result["cancelled"])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(all(item.get("cancelled") for item in result["results"]))
        self.assertIn(("pypi:pillow", "status", "Stopped"), events)

    def test_cancelled_update_recheck_does_not_start_an_installer(self):
        cancellation = threading.Event()
        cancellation.set()
        with patch.object(dependency_updates.subprocess, "Popen") as popen:
            code, output, stopped = dependency_updates._run_stream(
                ["installer"], cancel_event=cancellation)
        self.assertEqual(code, 130)
        self.assertTrue(stopped)
        self.assertIn("before launch", output)
        popen.assert_not_called()

    def test_close_choices_stop_continue_detach_or_stay(self):
        popup = DependencyUpdateWindow.__new__(DependencyUpdateWindow)
        popup.close_dialog = FakeDialog()
        popup.busy = True
        popup.operation = "update"
        popup.cancel_event = threading.Event()
        popup.close_when_done = False
        popup.detached = False
        popup.window = FakeWindow()
        popup.status = FakeVariable()
        popup.select_all_button = FakeStatusLabel()
        popup.clear_button = FakeStatusLabel()
        popup.update_button = FakeStatusLabel()
        popup._choose_close("stop")
        self.assertTrue(popup.cancel_event.is_set())
        self.assertTrue(popup.close_when_done)
        self.assertIn("Stopping", popup.status.value)
        self.assertFalse(popup.window.destroyed)

        popup.close_dialog = FakeDialog()
        popup.cancel_event = threading.Event()
        popup.close_when_done = False
        popup.detached = False
        popup.window = FakeWindow()
        popup._choose_close("continue-close")
        self.assertTrue(popup.detached)
        self.assertTrue(popup.window.destroyed)

        popup.close_dialog = FakeDialog()
        popup.detached = False
        popup.window = FakeWindow()
        popup._choose_close("stay")
        self.assertFalse(popup.detached)
        self.assertFalse(popup.window.destroyed)

    def test_close_choices_are_only_needed_after_an_installer_has_started(self):
        popup = DependencyUpdateWindow.__new__(DependencyUpdateWindow)
        popup.busy = True
        popup.operation = "check"
        popup.installation_sequence_started = threading.Event()
        popup.update_worker_finished = threading.Event()
        popup.update_worker_finished.set()
        self.assertFalse(popup._requires_close_choices())

        popup.operation = "update"
        popup.update_worker_finished.clear()
        self.assertFalse(popup._requires_close_choices())
        popup.installation_sequence_started.set()
        self.assertTrue(popup._requires_close_choices())

        popup.busy = False
        self.assertFalse(popup._requires_close_choices())

    def test_close_is_immediate_after_updates_are_done(self):
        popup = DependencyUpdateWindow.__new__(DependencyUpdateWindow)
        popup.busy = True
        popup.operation = "update"
        popup.installation_sequence_started = threading.Event()
        popup.installation_sequence_started.set()
        popup.update_worker_finished = threading.Event()
        popup.update_worker_finished.set()
        popup.cancel_event = threading.Event()
        popup.detached = False
        popup.window = FakeWindow()
        popup.close_when_done = False
        popup.busy_notified = True
        popup.busy_changed = lambda _busy: None
        popup.close_dialog = None
        popup._request_close()
        self.assertTrue(popup.window.destroyed)
        self.assertFalse(popup.cancel_event.is_set())

    def test_nonavailable_or_unknown_update_cannot_be_installed(self):
        report = {"ok": True, "interpreter": "python", "rows": [{
            "id": "pypi:numpy", "name": "numpy", "manager": "pip",
            "available": False, "selectable": False,
        }]}
        with patch.object(dependency_updates, "check_updates", return_value=report):
            result = dependency_updates.install_selected("machine.json", ["pypi:numpy"])
            self.assertFalse(result["ok"])
            with self.assertRaisesRegex(LockError, "unknown update selection"):
                dependency_updates.install_selected("machine.json", ["not-allowlisted"])


if __name__ == "__main__":
    unittest.main()
