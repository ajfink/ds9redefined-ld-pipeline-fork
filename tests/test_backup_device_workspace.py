from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backup_device_workspace import (BackupError, create_backup, create_device_backup,
                                     restore_backup, verify_archive)  # noqa: E402


class DeviceWorkspaceBackupTests(unittest.TestCase):
    def _instance(self, release: Path) -> Path:
        instance = release / "devices" / "MAC-ABCD1234" / "episodes" / "S01E12"
        (instance / "manifest").mkdir(parents=True)
        (instance / "logs").mkdir()
        (instance / "references").mkdir()
        (instance / "workspace.json").write_text(json.dumps({
            "schema": "ld-pipeline-shared-instance-v1",
            "storage_mode": "shared-release", "workflow_version": "16",
            "episode": "S01E12", "machine": "MAC", "instance_id": "ABCD1234",
            "instance_relative_path": "devices/MAC-ABCD1234/episodes/S01E12",
        }), encoding="utf-8")
        (instance / "manifest" / "S01E12.json").write_text('{"episode":"S01E12"}\n', encoding="utf-8")
        (instance / "logs" / "run.log").write_text("preserve evidence\n", encoding="utf-8")
        (instance / "logs" / "active.lock").write_text("pid=1\n", encoding="utf-8")
        (instance / "references" / "hash.txt").write_text("reference\n", encoding="utf-8")
        return instance

    def test_backup_verifies_and_restores_only_to_a_new_identity_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            devices = release / "devices"
            devices.mkdir(parents=True)
            instance = self._instance(release)
            destination = root / "backup-store"

            archive = create_backup(instance, destination, release)
            inventory = verify_archive(archive)

            self.assertEqual(inventory["episode"], "S01E12")
            self.assertIn("logs/active.lock", inventory["excluded_volatile_or_unscoped"])
            restore_release = root / "restore-release"
            restore_devices = restore_release / "devices"
            restore_devices.mkdir(parents=True)
            restored = restore_backup(archive, restore_devices, apply=False)
            self.assertFalse(restored.exists())
            restored = restore_backup(archive, restore_devices, apply=True)
            self.assertEqual((restored / "logs" / "run.log").read_text(encoding="utf-8"),
                             "preserve evidence\n")
            self.assertFalse((restored / "logs" / "active.lock").exists())

    def test_backup_destination_must_be_outside_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            (release / "devices").mkdir(parents=True)
            instance = self._instance(release)
            with self.assertRaisesRegex(BackupError, "outside the release tree"):
                create_backup(instance, release / "backups", release)

    def test_existing_restore_destination_is_never_merged_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            devices = release / "devices"
            devices.mkdir(parents=True)
            instance = self._instance(release)
            archive = create_backup(instance, root / "backups", release)
            with self.assertRaisesRegex(BackupError, "already exists"):
                restore_backup(archive, devices, apply=True)

    def test_tampered_backup_or_checksum_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            (release / "devices").mkdir(parents=True)
            instance = self._instance(release)
            archive = create_backup(instance, root / "backups", release)
            sidecar = archive.with_suffix(archive.suffix + ".sha256")
            sidecar.write_text("0" * 64 + "  " + archive.name + "\n", encoding="ascii")
            with self.assertRaisesRegex(BackupError, "SHA-256 does not match"):
                verify_archive(archive)

    def test_device_backup_covers_multiple_episode_workspaces_and_restores_them_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            device_root = release / "devices" / "MAC-ABCD1234"
            (device_root / "episodes").mkdir(parents=True)
            self._instance(release)
            second = device_root / "episodes" / "S01E13"
            (second / "manifest").mkdir(parents=True)
            (second / "logs").mkdir()
            (second / "workspace.json").write_text(json.dumps({
                "schema": "ld-pipeline-shared-instance-v1", "storage_mode": "shared-release",
                "workflow_version": "16", "episode": "S01E13", "machine": "MAC",
                "instance_id": "ABCD1234",
                "instance_relative_path": "devices/MAC-ABCD1234/episodes/S01E13",
            }), encoding="utf-8")
            (second / "manifest" / "S01E13.json").write_text("{}\n", encoding="utf-8")
            (device_root / "WORKFLOW-V16-CHANGE.md").write_text("change\n", encoding="utf-8")
            config = device_root / "local device config"
            config.mkdir()
            (config / "machine.json").write_text("{}\n", encoding="utf-8")
            archive = create_device_backup(device_root, root / "backup-store", release)
            inventory = verify_archive(archive)
            self.assertEqual(inventory["scope"], "device")
            self.assertEqual({row["episode"] for row in inventory["workspaces"]}, {"S01E12", "S01E13"})

            restore_devices = root / "restored-release" / "devices"
            restore_devices.mkdir(parents=True)
            restored = restore_backup(archive, restore_devices, apply=True)
            self.assertTrue((restored / "episodes" / "S01E12" / "manifest" / "S01E12.json").is_file())
            self.assertTrue((restored / "episodes" / "S01E13" / "manifest" / "S01E13.json").is_file())
            self.assertTrue((restored / "WORKFLOW-V16-CHANGE.md").is_file())
            self.assertTrue((restored / "local device config" / "machine.json").is_file())


if __name__ == "__main__":
    unittest.main()
