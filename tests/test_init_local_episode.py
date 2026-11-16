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
            with patch.dict(os.environ, {"LDP_PROFILE": str(profile)}, clear=False):
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


if __name__ == "__main__":
    unittest.main()
