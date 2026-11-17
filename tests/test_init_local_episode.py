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

import init_local_episode  # noqa: E402
import shared_workspace  # noqa: E402
from validate_episode import Checks, validate_local_inventory  # noqa: E402


def make_source(root: Path) -> Path:
    source = root / "rips" / "DS9 S01E07 Q-Less"
    source.mkdir(parents=True)
    for capture in ("L1", "M1"):
        stem = f"DS9-S01E07-Q-Less-{capture}"
        (source / f"{stem}.tbc").write_bytes(f"fixture {capture}".encode())
        (source / f"{stem}.tbc.json").write_text("{}\n", encoding="utf-8")
    return source


class LocalEpisodeInitializationTests(unittest.TestCase):
    def test_local_intake_needs_no_download_manifest_and_records_local_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root)
            probe = root / "probe"
            logs = root / "logs"
            probe.mkdir()
            logs.mkdir()
            profile = root / "machine.json"
            profile.write_text(json.dumps({
                "machine": "TEST", "os": "windows", "shared": {"enabled": False},
                "roots": {"probe": str(probe), "logs": str(logs)},
                "tools": {},
            }), encoding="utf-8")
            manifest_out = root / "manifest" / "S01E07.json"
            args = ["init_local_episode.py", "--episode", "S01E07",
                    "--local-source", str(source), "--confirmed-title", "Q-Less",
                    "--fingerprint-policy", "balanced", "--no-dvd",
                    "--cut-pad-images", "skip", "--manifest-out", str(manifest_out)]
            with patch("ldcommon.BUNDLE", root / "bundle"), \
                    patch.dict(os.environ, {"LDP_PROFILE": str(profile)}, clear=False):
                with patch.object(sys, "argv", args):
                    self.assertEqual(init_local_episode.main(), 0)

            self.assertFalse((source / "download-manifest.csv").exists())
            manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
            provenance = manifest["source_provenance"]
            self.assertEqual(provenance["mode"], "local-inventory")
            self.assertFalse(provenance["archive_verified"])
            self.assertEqual(len(manifest["captures"]), 2)
            self.assertEqual(manifest["captures"][0]["tbc_md5"], "")
            self.assertNotIn("source_dir", provenance["inventory"])

            checks = Checks()
            validate_local_inventory(checks, manifest, manifest["captures"],
                                     source, provenance["inventory"])
            self.assertFalse(checks.failed, checks.rows)

    def test_shared_local_intake_accepts_external_capture_and_dvd_in_device_manifest(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as identity_dir:
            root = Path(temporary)
            bundle = root / "app"
            (bundle / "episodes").mkdir(parents=True)
            (bundle / "episodes" / "_template.json").write_bytes(
                (ROOT / "episodes" / "_template.json").read_bytes())
            (bundle / "WORKFLOW_VERSION").write_text("15\n", encoding="ascii")
            source = make_source(root)
            dvd = root / "DVD" / "Vortex.mkv"
            dvd.parent.mkdir()
            dvd.write_bytes(b"selected DVD fixture")
            profile = root / "local-machine.json"
            profile.write_text(json.dumps({
                "machine": "TEST-LOCAL-SOURCE", "os": "windows",
                "shared": {"enabled": True, "release_root": str(bundle),
                           "device_name": "TEST-LOCAL-SOURCE"},
                "roots": {"downloads_root": str(root / "configured-downloads"),
                          "downloads_template": "DS9 {episode} LaserDisc",
                          "dvd_root": str(root / "configured-dvd")},
                "tools": {},
            }), encoding="utf-8")
            identity = {"LDP_DEVICE_ID_DIR": identity_dir}
            with patch.object(shared_workspace, "ROOT", bundle), \
                    patch("ldcommon.BUNDLE", bundle), \
                    patch.object(init_local_episode, "BUNDLE", bundle), \
                    patch.dict(os.environ, identity, clear=False):
                workspace = shared_workspace.create_workspace(
                    profile, json.loads(profile.read_text(encoding="utf-8")), "S01E07")
                env = {"LDP_PROFILE": str(profile), **workspace["shell_exports"]}
                args = ["init_local_episode.py", "--episode", "S01E07",
                        "--local-source", str(source), "--confirmed-title", "Q-Less",
                        "--fingerprint-policy", "balanced", "--dvd", str(dvd)]
                with patch.dict(os.environ, env, clear=False), patch.object(sys, "argv", args):
                    self.assertEqual(init_local_episode.main(), 0)

            manifest = Path(workspace["instance_root"]) / "manifest" / "S01E07.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["roots"]["downloads"], str(source.resolve()))
            self.assertEqual(data["files"]["dvd"]["path"], str(dvd.resolve()))
            checks = Checks()
            validate_local_inventory(checks, data, data["captures"], source,
                                     data["source_provenance"]["inventory"])
            self.assertFalse(checks.failed, checks.rows)


if __name__ == "__main__":
    unittest.main()
