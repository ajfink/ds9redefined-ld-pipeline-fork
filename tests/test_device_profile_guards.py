from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import bootstrap_deps  # noqa: E402
import doctor  # noqa: E402
import shared_workspace  # noqa: E402
from dependency_lock import LockError  # noqa: E402


class DeviceProfileGuardTests(unittest.TestCase):
    def test_verified_device_scoped_writes_allow_shared_root_write_warning(self):
        report = {"enabled": True, "instance_ready": True,
                  "device_roots_scoped": True,
                  "release_root_writable_for_process": True}
        self.assertTrue(doctor.shared_workspace_ready(report))
        report["device_roots_scoped"] = False
        self.assertFalse(doctor.shared_workspace_ready(report))
        report["device_roots_scoped"] = True
        report["instance_ready"] = False
        self.assertFalse(doctor.shared_workspace_ready(report))

    def test_doctor_and_dependency_bootstrap_accept_only_own_device_profile(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as identity_dir:
            bundle = Path(temporary) / "app"
            bundle.mkdir()
            (bundle / "machine.example.json").write_bytes(
                (ROOT / "machine.example.json").read_bytes())
            with patch.dict(os.environ, {"LDP_DEVICE_ID_DIR": identity_dir}, clear=False):
                profile_path, _created = shared_workspace.bootstrap_device_profile(
                    bundle, system="Windows", machine="PROFILE-TEST")
                with patch.object(shared_workspace, "ROOT", bundle), \
                        patch.object(doctor, "ROOT", bundle), \
                        patch.object(bootstrap_deps, "ROOT", bundle):
                    self.assertEqual(doctor.profile_path(str(profile_path)), profile_path.resolve())
                    loaded_path, profile = bootstrap_deps.load_profile(str(profile_path))
                    self.assertEqual(loaded_path, profile_path.resolve())
                    self.assertEqual(profile["shared"]["device_instance_id"],
                                     profile_path.parents[1].name.rsplit("-", 1)[-1])

                    unrelated = bundle / "machine.json"
                    unrelated.write_text(json.dumps({
                        "machine": "PROFILE-TEST", "shared": {"enabled": True,
                        "release_root": str(bundle)},
                    }), encoding="utf-8")
                    with self.assertRaises(LockError):
                        doctor.profile_path(str(unrelated))
                    with self.assertRaises(LockError):
                        bootstrap_deps.load_profile(str(unrelated))


if __name__ == "__main__":
    unittest.main()
