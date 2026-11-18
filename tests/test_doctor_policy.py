from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ldcommon  # noqa: E402
import doctor  # noqa: E402
from dependency_lock import LockError, target_for  # noqa: E402
from doctor import publish_instance_root, shared_workspace_ready  # noqa: E402


class SharedWorkspaceDoctorPolicyTests(unittest.TestCase):
    def test_inherited_release_root_acl_is_advisory_when_device_workspace_is_ready(self):
        report = {
            "enabled": True,
            "instance_ready": True,
            "release_root_writable_for_process": True,
        }
        self.assertTrue(shared_workspace_ready(report))

    def test_missing_device_workspace_still_blocks_shared_readiness(self):
        self.assertFalse(shared_workspace_ready({
            "enabled": True,
            "instance_ready": False,
            "release_root_writable_for_process": False,
        }))

    def test_non_shared_mode_does_not_add_an_acl_gate(self):
        self.assertTrue(shared_workspace_ready({"enabled": False}))


class InstanceRootFlagTests(unittest.TestCase):
    """MAC-OPENCODE-4DD0B985 S01E16 device log section 30 (V16 defect)."""

    def setUp(self):
        self.previous = os.environ.pop("LDP_INSTANCE_ROOT", None)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("LDP_INSTANCE_ROOT", None)
        else:
            os.environ["LDP_INSTANCE_ROOT"] = self.previous

    def test_flag_is_published_to_the_doctors_own_environment_not_only_the_child(self):
        env = {"PATH": os.environ.get("PATH", "")}
        resolved = publish_instance_root(str(Path(__file__).resolve().parent), env)
        self.assertTrue(Path(resolved).is_absolute())
        self.assertEqual(env["LDP_INSTANCE_ROOT"], resolved)
        # This is the defect: the resolver reads os.environ of this process.
        self.assertEqual(os.environ["LDP_INSTANCE_ROOT"], resolved)

    def test_conflicting_exported_instance_root_fails_closed(self):
        os.environ["LDP_INSTANCE_ROOT"] = str(Path(__file__).resolve())
        other = Path(__file__).resolve().parent / "not-the-exported-instance"
        with self.assertRaisesRegex(LockError, "conflicts with the exported"):
            publish_instance_root(str(other), {})


def build_shared_fixture(temp: Path) -> dict:
    """Synthetic shared release bundle with one valid device instance."""
    root = temp / "bundle"
    (root / "scripts").mkdir(parents=True)
    (root / "WORKFLOW_VERSION").write_text("16\n", encoding="ascii")
    instance = root / "devices" / "TEST-DEVICE-ABCD1234" / "episodes" / "S01E19"
    for name in ("manifest", "logs", "references", "cut-pad-images"):
        (instance / name).mkdir(parents=True, exist_ok=True)
    (instance / "workspace.json").write_text(json.dumps({
        "schema": "ld-pipeline-shared-instance-v1",
        "storage_mode": "shared-release",
        "workflow_version": "16",
        "episode": "S01E19",
        "series": "DS9",
        "identity": "DS9:S01E19",
        "machine": "TEST-DEVICE",
        "instance_id": "ABCD1234",
        "profile_name": "machine.json",
        "release_root": ".",
        "instance_relative_path": "devices/TEST-DEVICE-ABCD1234/episodes/S01E19",
        "write_scope": "this machine's episode workspace only",
    }, indent=2), encoding="utf-8")
    profile = {
        "machine": "TEST-DEVICE",
        "os": target_for().split("-", 1)[0],
        "roots": {"logs": str(temp / "local-logs")},
        "python": {"interpreter": sys.executable},
        "shared": {"enabled": True, "release_root": str(root),
                   "device_name": "TEST-DEVICE"},
    }
    profile_path = temp / "machine.json"
    profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return {"root": root, "instance": instance, "profile": profile_path}


def run_doctor(fixture: dict, extra: list[str], clear_instance_env: bool = True) -> tuple[int, str]:
    """Run doctor.main() against the fixture with no exported workspace variables."""
    argv = ["doctor.py", "--profile", str(fixture["profile"]), "--episode", "S01E19"] + extra
    keys = ["LDP_PROFILE", "LDP_EPISODE", "LDP_INSTANCE_MODE", "LDP_MANIFEST_DIR"]
    if clear_instance_env:
        keys.append("LDP_INSTANCE_ROOT")
    saved = {key: os.environ.pop(key, None) for key in keys}
    saved_argv = sys.argv
    buffer = io.StringIO()
    try:
        sys.argv = argv
        with mock.patch.object(doctor, "ROOT", fixture["root"]), \
                mock.patch.object(ldcommon, "BUNDLE", fixture["root"]), \
                mock.patch.object(doctor, "compare_lock",
                                  lambda *a, **k: {"ok": True, "lock": "lock",
                                                   "python": "test", "mismatches": []}), \
                mock.patch.object(doctor, "windows_preflight",
                                  lambda *a, **k: {"ok": True, "exit_code": 0}), \
                mock.patch.object(doctor, "mac_preflight",
                                  lambda *a, **k: {"ok": True, "exit_code": 0}), \
                mock.patch.object(doctor, "linux_preflight",
                                  lambda *a, **k: {"ok": True, "exit_code": 0}), \
                redirect_stdout(buffer):
            code = doctor.main()
    finally:
        sys.argv = saved_argv
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, buffer.getvalue()


class DoctorInstanceRootEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = build_shared_fixture(Path(self.temp.name))
        self.report = self.fixture["instance"] / "logs" / "doctor-report.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_report_is_written_from_the_flag_alone_without_exported_variable(self):
        code, out = run_doctor(self.fixture, [
            "--instance-root", str(self.fixture["instance"]),
            "--json", "--report-out", str(self.report),
        ])
        self.assertNotIn("shared mode has no active workspace", out)
        self.assertIn(code, (0, 1), msg=out)
        self.assertTrue(self.report.is_file(), msg=out)
        data = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(data["workflow_version"], "16")
        self.assertTrue(data["shared_workspace"]["instance_ready"])
        self.assertIn("devices", data["shared_workspace"]["instance_root"])
        self.assertFalse(data["shared_workspace"]["release_root_acl_is_gate"])

    def test_conflicting_flag_and_exported_value_are_reported_as_an_error(self):
        os.environ["LDP_INSTANCE_ROOT"] = str(self.fixture["instance"].parent)
        try:
            code, out = run_doctor(self.fixture, [
                "--instance-root", str(self.fixture["instance"]), "--json",
            ], clear_instance_env=False)
            self.assertEqual(code, 2, msg=out)
            self.assertIn("conflicts with the exported LDP_INSTANCE_ROOT", out)
        finally:
            os.environ.pop("LDP_INSTANCE_ROOT", None)


if __name__ == "__main__":
    unittest.main()
