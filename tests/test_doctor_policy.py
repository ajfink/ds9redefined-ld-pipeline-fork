from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from doctor import shared_workspace_ready  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
