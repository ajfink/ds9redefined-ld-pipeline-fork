from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import shared_workspace  # noqa: E402
from ldcommon import (PipelineError, assert_writable_path, episode_manifest_path,
                      shared_instance_root)  # noqa: E402
from shared_workspace import (LockError, configure_shared_profile,
                              initialize_workspace, resolve_instance_workspace)  # noqa: E402


class SharedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.device_identity_root = tempfile.TemporaryDirectory()
        self.identity_env = patch.dict(os.environ, {
            "LDP_DEVICE_ID_DIR": self.device_identity_root.name,
        }, clear=False)
        self.identity_env.start()

    def tearDown(self):
        self.identity_env.stop()
        self.device_identity_root.cleanup()

    def test_ensure_creates_readme_relative_workspace_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            (bundle / "devices").mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("7\n", encoding="ascii")
            profile_path = Path(temp) / "local-profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "DEVICE-A", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "DEVICE-A"}}
            with patch.object(shared_workspace, "ROOT", bundle):
                first, warning1 = resolve_instance_workspace(profile_path, profile, "S01E09")
                second, warning2 = resolve_instance_workspace(profile_path, profile, "S01E09")
            self.assertIsNone(warning1)
            self.assertIsNone(warning2)
            self.assertEqual(first["instance_root"], second["instance_root"])
            self.assertTrue(Path(first["instance_root"]).resolve().is_relative_to(
                (bundle / "devices").resolve()))
            self.assertTrue((Path(first["instance_root"]) / "logs" / "README.md").is_file())

    def test_workspace_creation_failure_falls_back_to_local_roots_without_gating(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            bundle.mkdir()
            (bundle / "WORKFLOW_VERSION").write_text("7\n", encoding="ascii")
            profile_path = Path(temp) / "local-profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            local_logs = Path(temp) / "local" / "logs"
            profile = {"machine": "DEVICE-B", "roots": {"logs": str(local_logs)},
                       "shared": {"enabled": True, "release_root": str(bundle),
                                  "device_name": "DEVICE-B"}}
            original_mkdir = Path.mkdir

            def deny_devices_creation(path, *args, **kwargs):
                if path == bundle / "devices":
                    raise PermissionError("simulated README-folder ACL denial")
                return original_mkdir(path, *args, **kwargs)

            with patch.object(shared_workspace, "ROOT", bundle), \
                    patch.object(Path, "mkdir", deny_devices_creation):
                result, warning = resolve_instance_workspace(profile_path, profile, "S01E09")
                validated = shared_workspace.validate_instance(
                    result["instance_root"], profile_path, profile)
            self.assertEqual(result["storage_mode"], "local-fallback")
            self.assertIsNotNone(warning)
            self.assertEqual(validated, Path(result["instance_root"]))
            for folder in ("manifest", "logs", "references", "cut-pad-images"):
                self.assertTrue((validated / folder).is_dir())
            self.assertTrue((validated / "logs" / "README.md").is_file())
            with patch.object(__import__("ldcommon"), "BUNDLE", bundle), \
                    patch.dict("os.environ", result["shell_exports"], clear=False):
                self.assertEqual(shared_instance_root(profile), validated)
                self.assertEqual(episode_manifest_path("S01E09", SimpleNamespace(
                    profile=profile, profile_path=profile_path)),
                    validated / "manifest" / "S01E09.json")

    def test_legacy_shared_workspace_mode_is_inferred_without_rewriting_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            (bundle / "devices").mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("9\n", encoding="ascii")
            profile_path = Path(temp) / "local-profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "DEVICE-C", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "DEVICE-C"}}
            with patch.object(shared_workspace, "ROOT", bundle):
                created = shared_workspace.create_workspace(profile_path, profile, "S01E09")
                root = Path(created["instance_root"])
                metadata_path = root / "workspace.json"
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
                saved["workflow_version"] = "7"
                saved.pop("storage_mode")
                metadata_path.write_text(json.dumps(saved), encoding="utf-8")

                resolved, warning = resolve_instance_workspace(
                    profile_path, profile, "S01E09", create=True)

            self.assertIsNone(warning)
            self.assertEqual(resolved["storage_mode"], "shared-release")
            unchanged = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertNotIn("storage_mode", unchanged)
            self.assertEqual(unchanged["workflow_version"], "7")

    def test_legacy_local_fallback_mode_is_inferred_without_rewriting_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            bundle.mkdir()
            (bundle / "WORKFLOW_VERSION").write_text("9\n", encoding="ascii")
            profile_path = Path(temp) / "local-profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            local_logs = Path(temp) / "local" / "logs"
            profile = {"machine": "DEVICE-D", "roots": {"logs": str(local_logs)},
                       "shared": {"enabled": True, "release_root": str(bundle),
                                  "device_name": "DEVICE-D"}}
            with patch.object(shared_workspace, "ROOT", bundle):
                created = shared_workspace.create_local_fallback(
                    profile_path, profile, "S01E09")
                root = Path(created["instance_root"])
                metadata_path = root / "workspace.json"
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
                saved.pop("storage_mode")
                metadata_path.write_text(json.dumps(saved), encoding="utf-8")

                resolved, warning = resolve_instance_workspace(
                    profile_path, profile, "S01E09", create=True)

            self.assertEqual(resolved["storage_mode"], "local-fallback")
            self.assertIsNotNone(warning)
            self.assertNotIn("storage_mode", json.loads(metadata_path.read_text(encoding="utf-8")))

    def test_shared_profile_configuration_is_external_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            bundle.mkdir()
            (bundle / "avsplugins").mkdir()
            source = Path(temp) / "base-machine.json"
            output = Path(temp) / "profiles" / "machine-shared.json"
            source.write_text(json.dumps({
                "machine": "BOX-A", "os": "windows",
                "roots": {"work": r"D:\work", "runbook_dir": r"C:\old-bundle"},
                "tools": {"avisynth_plugin_dir": r"C:\old-bundle\avsplugins"},
            }), encoding="utf-8")
            with patch.object(shared_workspace, "ROOT", bundle):
                created = configure_shared_profile(source, output, None)
                configured = json.loads(created.read_text(encoding="utf-8"))
                self.assertEqual(Path(configured["shared"]["release_root"]).resolve(), bundle.resolve())
                self.assertEqual(Path(configured["roots"]["runbook_dir"]).resolve(), bundle.resolve())
                self.assertEqual(Path(configured["tools"]["avisynth_plugin_dir"]).resolve(),
                                 (bundle / "avsplugins").resolve())
                with self.assertRaisesRegex(LockError, "refusing to overwrite"):
                    configure_shared_profile(source, output, None)

    def test_new_episodes_reuse_one_stable_machine_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            devices = bundle / "devices"
            devices.mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("7\n", encoding="ascii")
            profile_path = Path(temp) / "local-profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "MACHINE / DEVICE", "shared": {"release_root": str(bundle)}}
            with patch.object(shared_workspace, "ROOT", bundle):
                first = shared_workspace.create_workspace(profile_path, profile, "S01E07")
                second = shared_workspace.create_workspace(profile_path, profile, "S01E08")
            self.assertEqual(first["instance_id"], second["instance_id"])
            self.assertEqual(Path(first["instance_root"]).parents[1],
                             Path(second["instance_root"]).parents[1])
            self.assertNotEqual(first["instance_root"], second["instance_root"])
            for result in (first, second):
                instance = Path(result["shell_exports"]["LDP_INSTANCE_ROOT"])
                self.assertTrue((instance / "manifest").is_dir())
                self.assertTrue((instance / "logs").is_dir())
                self.assertTrue((instance / "references").is_dir())
                self.assertTrue((instance / "cut-pad-images").is_dir())
                self.assertTrue((instance / "workspace.json").is_file())
                saved = json.loads((instance / "workspace.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["release_root"], ".")
                self.assertFalse(Path(saved["instance_relative_path"]).is_absolute())
                self.assertNotIn("profile_path", saved)
                self.assertEqual(result["instance_root"], str(instance))
            device_folders = list((bundle / "devices").glob("MACHINE-DEVICE-*/episodes"))
            self.assertEqual(len(device_folders), 1)
            self.assertEqual(sorted(p.name for p in device_folders[0].iterdir()), ["S01E07", "S01E08"])

    def test_legacy_multiple_device_folders_require_explicit_operator_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            devices = Path(temp) / "devices"
            devices.mkdir()
            profile = {"machine": "MAC-OPENCODE", "shared": {
                "enabled": True, "device_name": "MAC-OPENCODE"}}
            for suffix in ("4DD0B985", "1EF5351C"):
                workspace = devices / f"MAC-OPENCODE-{suffix}" / "episodes" / "S01E11"
                workspace.mkdir(parents=True)
                (workspace / "workspace.json").write_text(json.dumps({
                    "machine": "MAC-OPENCODE", "instance_id": suffix,
                    "episode": "S01E11",
                }), encoding="utf-8")
            with self.assertRaisesRegex(LockError, "multiple legacy device folders"):
                shared_workspace.stable_device_instance_id(profile, "MAC-OPENCODE", devices)
            profile["shared"]["device_instance_id"] = "4DD0B985"
            chosen = shared_workspace.stable_device_instance_id(profile, "MAC-OPENCODE", devices)
            self.assertEqual(chosen, "4DD0B985")
            no_profile_override = {"machine": "MAC-OPENCODE", "shared": {
                "enabled": True, "device_name": "MAC-OPENCODE"}}
            with patch.dict(os.environ, {"LDP_DEVICE_INSTANCE_ID": "1EF5351C"}, clear=False):
                with self.assertRaisesRegex(LockError, "conflicts with the saved machine identity"):
                    shared_workspace.stable_device_instance_id(
                        no_profile_override, "MAC-OPENCODE", devices)

    def test_ambiguous_legacy_ids_fall_back_without_creating_a_third_shared_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ld-pipeline"
            devices = bundle / "devices"
            devices.mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("12\n", encoding="ascii")
            profile_path = root / "profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            local_logs = root / "local-logs"
            profile = {"machine": "MAC-OPENCODE", "roots": {"logs": str(local_logs)},
                       "shared": {"enabled": True, "release_root": str(bundle),
                                  "device_name": "MAC-OPENCODE"}}
            for suffix in ("4DD0B985", "1EF5351C"):
                folder = devices / f"MAC-OPENCODE-{suffix}" / "episodes" / "S01E11"
                folder.mkdir(parents=True)
                (folder / "workspace.json").write_text(json.dumps({
                    "storage_mode": "shared-release", "release_root": ".",
                    "instance_relative_path": folder.resolve().relative_to(bundle.resolve()).as_posix(),
                    "machine": "MAC-OPENCODE", "instance_id": suffix, "episode": "S01E11",
                }), encoding="utf-8")
            with patch.object(shared_workspace, "ROOT", bundle):
                result, warning = resolve_instance_workspace(profile_path, profile, "S01E13")
            self.assertEqual(result["storage_mode"], "local-fallback")
            self.assertIn("multiple legacy", warning)
            self.assertEqual(len(list(devices.glob("MAC-OPENCODE-*/"))), 2)

    def test_incomplete_legacy_folder_is_not_silently_bypassed(self):
        with tempfile.TemporaryDirectory() as temp:
            devices = Path(temp) / "devices"
            (devices / "DEVICE-A1B2C3D4").mkdir(parents=True)
            profile = {"machine": "DEVICE-A", "shared": {"device_name": "DEVICE"}}
            with self.assertRaisesRegex(LockError, "ownership cannot be verified"):
                shared_workspace.stable_device_instance_id(profile, "DEVICE", devices)

    def test_saved_device_identity_selects_its_legacy_episode_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ld-pipeline"
            devices = bundle / "devices"
            devices.mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("12\n", encoding="ascii")
            profile_path = root / "profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "MAC-OPENCODE", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "MAC-OPENCODE"}}
            for suffix in ("4DD0B985", "1EF5351C"):
                folder = devices / f"MAC-OPENCODE-{suffix}" / "episodes" / "S01E11"
                for name in ("manifest", "logs", "references", "cut-pad-images"):
                    (folder / name).mkdir(parents=True, exist_ok=True)
                (folder / "workspace.json").write_text(json.dumps({
                    "storage_mode": "shared-release", "release_root": ".",
                    "instance_relative_path": folder.resolve().relative_to(bundle.resolve()).as_posix(),
                    "machine": "MAC-OPENCODE", "instance_id": suffix, "episode": "S01E11",
                }), encoding="utf-8")
            with patch.object(shared_workspace, "ROOT", bundle), \
                    patch.dict(os.environ, {"LDP_DEVICE_INSTANCE_ID": "4DD0B985"}, clear=False):
                result, warning = resolve_instance_workspace(profile_path, profile, "S01E11")
            self.assertIsNone(warning)
            self.assertIn("MAC-OPENCODE-4DD0B985", result["instance_root"])

    def test_one_legacy_device_folder_is_adopted_for_future_episodes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ld-pipeline"
            old = bundle / "devices" / "DEVICE-A1B2C3D4" / "episodes" / "S01E01"
            old.mkdir(parents=True)
            (old / "workspace.json").write_text(json.dumps({
                "machine": "DEVICE-A", "instance_id": "A1B2C3D4", "episode": "S01E01",
            }), encoding="utf-8")
            (bundle / "WORKFLOW_VERSION").write_text("12\n", encoding="ascii")
            profile_path = root / "profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "DEVICE-A", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "DEVICE"}}
            with patch.object(shared_workspace, "ROOT", bundle):
                created = shared_workspace.create_workspace(profile_path, profile, "S01E02")
            self.assertEqual(created["instance_id"], "A1B2C3D4")
            self.assertEqual(Path(created["instance_root"]).parents[1], old.parents[1])
            identity = shared_workspace.device_identity_path("DEVICE")
            self.assertTrue(identity.is_file())
            self.assertEqual(json.loads(identity.read_text(encoding="utf-8"))["instance_id"],
                             "A1B2C3D4")

    def test_explicit_legacy_workspace_selection_persists_machine_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ld-pipeline"
            devices = bundle / "devices"
            episode_root = devices / "DEVICE-A1B2C3D4" / "episodes" / "S01E01"
            episode_root.mkdir(parents=True)
            profile = {"machine": "DEVICE-A", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "DEVICE"}}
            metadata = {"instance_id": "A1B2C3D4"}
            with patch.object(shared_workspace, "ROOT", bundle):
                shared_workspace.adopt_workspace_device_identity(episode_root, profile, metadata)
            identity = shared_workspace.device_identity_path("DEVICE")
            self.assertEqual(json.loads(identity.read_text(encoding="utf-8"))["instance_id"],
                             "A1B2C3D4")

    def test_repeated_init_refuses_to_create_a_sibling_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            (bundle / "devices").mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("9\n", encoding="ascii")
            profile_path = Path(temp) / "local-profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "DEVICE-E", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "DEVICE-E"}}
            with patch.object(shared_workspace, "ROOT", bundle):
                first = initialize_workspace(profile_path, profile, "S01E09")
                with self.assertRaisesRegex(LockError, "use 'ensure'"):
                    initialize_workspace(profile_path, profile, "S01E09")
                folders = list((bundle / "devices").glob("DEVICE-E-*/episodes/S01E09"))
            self.assertEqual(len(folders), 1)
            self.assertEqual(Path(first["instance_root"]).resolve(), folders[0].resolve())

    def test_shared_manifest_resolves_inside_current_instance_and_rejects_release_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            devices = bundle / "devices"
            instance = devices / "DEVICE-A1B2C3D4" / "episodes" / "S01E07"
            (instance / "manifest").mkdir(parents=True)
            (instance / "logs").mkdir()
            (instance / "references").mkdir()
            (instance / "cut-pad-images").mkdir()
            devices.mkdir(exist_ok=True, parents=True)
            (instance / "workspace.json").write_text(json.dumps({
                "release_root": ".",
                "instance_relative_path": instance.resolve().relative_to(bundle.resolve()).as_posix(),
                "machine": "DEVICE", "episode": "S01E07", "instance_id": "A1B2C3D4"}),
                encoding="utf-8")
            profile = {"machine": "DEVICE", "shared": {"enabled": True,
                                                           "release_root": str(bundle),
                                                           "device_name": "DEVICE"}}
            fake_ld = SimpleNamespace(profile=profile, profile_path=Path(temp) / "local-profile.json")
            env = {"LDP_INSTANCE_ROOT": str(instance)}
            with patch.object(__import__("ldcommon"), "BUNDLE", bundle), \
                    patch.dict("os.environ", env, clear=False):
                self.assertEqual(episode_manifest_path("S01E07", fake_ld),
                                 instance / "manifest" / "S01E07.json")
                self.assertEqual(assert_writable_path(instance / "logs" / "stage.log", fake_ld),
                                 instance / "logs" / "stage.log")
                with self.assertRaises(PipelineError):
                    assert_writable_path(bundle / "README.md", fake_ld)
                with self.assertRaises(PipelineError):
                    assert_writable_path(devices / "OTHER" / "episodes" / "S01E07" / "manifest" / "x.json", fake_ld)

    def test_reference_and_cut_pad_publication_refuse_overwrites_and_preserve_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "ld-pipeline"
            devices = bundle / "devices"
            devices.mkdir(parents=True)
            (bundle / "WORKFLOW_VERSION").write_text("7\n", encoding="ascii")
            profile_path = Path(temp) / "profile.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            profile = {"machine": "M5", "shared": {"release_root": str(bundle)}}
            with patch.object(shared_workspace, "ROOT", bundle):
                metadata = shared_workspace.create_workspace(profile_path, profile, "S01E07")
                instance = Path(metadata["shell_exports"]["LDP_INSTANCE_ROOT"])
                ref = Path(temp) / "alignment.tsv"
                ref.write_text("measurement\n", encoding="utf-8")
                copied = shared_workspace.publish_reference(ref, instance, None,
                                                            profile_path, profile)
                self.assertEqual(copied.read_text(encoding="utf-8"), "measurement\n")
                with self.assertRaises(LockError):
                    shared_workspace.publish_reference(ref, instance, None,
                                                       profile_path, profile)

                png = Path(temp) / "DVD-00-12-42.762-f00018288.png"
                payload = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (768).to_bytes(4, "big") + (484).to_bytes(4, "big")
                png.write_bytes(payload)
                image, sidecar = shared_workspace.publish_cut_pad(
                    png, instance, 18222, "DS9 Q-Less DVD MKV", "a" * 64, True,
                    "stable-background RGB match to preceding LD frame")
                provenance = json.loads(sidecar.read_text(encoding="utf-8"))
                self.assertEqual(provenance["schema"], "ld-pipeline-cut-pad-provenance-v2")
                self.assertEqual(provenance["png_sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(provenance["output_pad_frame"], 18222)
                self.assertEqual(provenance["color_match_note"],
                                 "stable-background RGB match to preceding LD frame")
                with self.assertRaisesRegex(LockError, "color-match method"):
                    shared_workspace.publish_cut_pad(
                        png, instance, 18223, "DS9 Q-Less DVD MKV", "a" * 64, True, "  ")
                with self.assertRaises(LockError):
                    shared_workspace.publish_cut_pad(
                        png, instance, 18222, "DS9 Q-Less DVD MKV", "a" * 64, True,
                        "stable-background RGB match to preceding LD frame")


if __name__ == "__main__":
    unittest.main()
