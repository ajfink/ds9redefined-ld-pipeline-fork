from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ldcommon  # noqa: E402
from ldcommon import Ld, PipelineError, episode_manifest_path, load_episode  # noqa: E402
import shared_workspace  # noqa: E402


class SeriesIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        (self.bundle / "episodes" / "VOY").mkdir(parents=True)
        self.bundle_patch = patch.object(ldcommon, "BUNDLE", self.bundle)
        self.bundle_patch.start()
        self.profile_path = self.root / "machine.json"
        roots = {}
        for name in ("work", "probe", "avs", "audio", "subs", "out", "prores", "stack", "logs", "downloads_root"):
            value = self.root / name
            value.mkdir(exist_ok=True)
            roots[name] = str(value)
        roots["downloads_template"] = "DS9 {episode} LaserDisc"
        self.profile_path.write_text(json.dumps({"machine": "TEST", "roots": roots}), encoding="utf-8")
        self.ld = Ld(self.profile_path)
        self.env = patch.dict(os.environ, {"LDP_SERIES": "DS9"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.bundle_patch.stop()
        self.temp.cleanup()

    def write_manifest(self, series: str):
        data = {"episode": "S01E01", "title": f"{series} pilot", "files": {
            "stack_master": {"root": "prores", "name": "S01E01-stack-master.mov"}}}
        if series == "VOY":
            data["series"] = "VOY"
            data["identity"] = "VOY:S01E01"
        target = self.bundle / "episodes" / ("VOY" if series == "VOY" else "") / "S01E01.json"
        target.write_text(json.dumps(data), encoding="utf-8")
        return target

    def test_same_code_local_manifests_and_output_paths_are_isolated(self):
        ds9_path = self.write_manifest("DS9")
        voy_path = self.write_manifest("VOY")
        with patch.object(ldcommon, "BUNDLE", self.bundle):
            ds9 = load_episode("S01E01", self.ld)
            voy = load_episode("VOY:S01E01", self.ld)
            self.assertEqual(episode_manifest_path("S01E01", self.ld), ds9_path)
            self.assertEqual(episode_manifest_path("S01E01", self.ld, series="VOY"), voy_path)
            self.assertEqual(ds9.identity, "DS9:S01E01")
            self.assertEqual(voy.identity, "VOY:S01E01")
            self.assertEqual(ds9.path("stack_master"), Path(self.ld.profile["roots"]["prores"]) / "S01E01-stack-master.mov")
            self.assertEqual(voy.path("stack_master"), Path(self.ld.profile["roots"]["prores"]) / "VOY" / "S01E01-stack-master.mov")
            self.assertEqual(self.ld.downloads_dir("S01E01", series="VOY"),
                             Path(self.ld.profile["roots"]["downloads_root"]) / "VOY S01E01 LaserDisc")

    def test_bare_identity_defaults_to_ds9_and_manifest_series_is_validated(self):
        ds9_path = self.write_manifest("DS9")
        self.write_manifest("VOY")
        wrong = json.loads(ds9_path.read_text(encoding="utf-8"))
        wrong["series"] = "VOY"
        wrong["identity"] = "VOY:S01E01"
        ds9_path.write_text(json.dumps(wrong), encoding="utf-8")
        os.environ["LDP_SERIES"] = "DS9"
        with patch.object(ldcommon, "BUNDLE", self.bundle):
            with self.assertRaisesRegex(PipelineError, "requested DS9:S01E01, manifest series is VOY"):
                load_episode("S01E01", self.ld)

    def test_same_code_shared_workspaces_have_distinct_roots_and_manifests(self):
        shared_bundle = self.root / "shared"
        (shared_bundle / "devices").mkdir(parents=True)
        (shared_bundle / "WORKFLOW_VERSION").write_text("14\n", encoding="ascii")
        profile_path = self.root / "shared-profile.json"
        profile_path.write_text("{}\n", encoding="utf-8")
        profile = {"machine": "TEST-SHARED", "shared": {
            "enabled": True, "release_root": str(shared_bundle), "device_name": "TEST-SHARED"}}
        with patch.object(shared_workspace, "ROOT", shared_bundle), \
                patch.dict(os.environ, {"LDP_DEVICE_ID_DIR": str(self.root / "identity")}, clear=False):
            ds9, _ = shared_workspace.resolve_instance_workspace(
                profile_path, profile, "S01E01", create=True, series="DS9")
            voy, _ = shared_workspace.resolve_instance_workspace(
                profile_path, profile, "S01E01", create=True, series="VOY")
            self.assertNotEqual(ds9["instance_root"], voy["instance_root"])
            self.assertEqual(Path(ds9["instance_root"]).name, "S01E01")
            self.assertEqual(Path(voy["instance_root"]).name, "VOY-S01E01")
            self.assertEqual(json.loads((Path(ds9["instance_root"]) / "workspace.json").read_text())["identity"], "DS9:S01E01")
            self.assertEqual(json.loads((Path(voy["instance_root"]) / "workspace.json").read_text())["identity"], "VOY:S01E01")
            with patch.object(ldcommon, "BUNDLE", shared_bundle):
                for result, series in ((ds9, "DS9"), (voy, "VOY")):
                    with patch.dict(os.environ, result["shell_exports"], clear=False):
                        manifest = episode_manifest_path(
                            "S01E01", type("Profile", (), {"profile": profile,
                                                          "profile_path": profile_path})(),
                            series=series)
                    self.assertEqual(manifest,
                                     Path(result["instance_root"]) / "manifest" / "S01E01.json")


if __name__ == "__main__":
    unittest.main()
