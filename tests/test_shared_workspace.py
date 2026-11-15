from __future__ import annotations

import hashlib
import json
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
                              resolve_instance_workspace)  # noqa: E402


class SharedWorkspaceTests(unittest.TestCase):
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
            self.assertTrue(Path(first["instance_root"]).is_relative_to(bundle / "devices"))
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
                self.assertEqual(configured["shared"]["release_root"], str(bundle.resolve()))
                self.assertEqual(configured["roots"]["runbook_dir"], str(bundle.resolve()))
                self.assertEqual(configured["tools"]["avisynth_plugin_dir"],
                                 str(bundle.resolve() / "avsplugins"))
                with self.assertRaisesRegex(LockError, "refusing to overwrite"):
                    configure_shared_profile(source, output, None)

    def test_each_init_gets_a_distinct_episode_folder_with_scoped_subdirectories(self):
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
                second = shared_workspace.create_workspace(profile_path, profile, "S01E07")
            self.assertNotEqual(first["instance_id"], second["instance_id"])
            for result in (first, second):
                instance = Path(result["shell_exports"]["LDP_INSTANCE_ROOT"])
                self.assertTrue((instance / "manifest").is_dir())
                self.assertTrue((instance / "logs").is_dir())
                self.assertTrue((instance / "references").is_dir())
                self.assertTrue((instance / "cut-pad-images").is_dir())
                self.assertTrue((instance / "workspace.json").is_file())
                saved = (instance / "workspace.json").read_text(encoding="utf-8")
                self.assertNotIn(str(profile_path), saved)
                self.assertNotIn(str(bundle.resolve()), saved)
                self.assertEqual(result["instance_root"], str(instance))

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
                    png, instance, 18222, "DS9 Q-Less DVD MKV", "a" * 64, True)
                provenance = json.loads(sidecar.read_text(encoding="utf-8"))
                self.assertEqual(provenance["png_sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(provenance["output_pad_frame"], 18222)
                with self.assertRaises(LockError):
                    shared_workspace.publish_cut_pad(
                        png, instance, 18222, "DS9 Q-Less DVD MKV", "a" * 64, True)


if __name__ == "__main__":
    unittest.main()
