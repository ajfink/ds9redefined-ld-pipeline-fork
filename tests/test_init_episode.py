from __future__ import annotations

import sys
import unittest
import csv
import hashlib
import json
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from ldcommon import PipelineError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import init_episode as init_module  # noqa: E402
from scripts.init_episode import (archive_episode_identity, episode_files,
                                  title_from_capture)  # noqa: E402


class InitEpisodeFileKeyTests(unittest.TestCase):
    def test_archive_identity_infers_series_and_preserves_ds9_legacy_default(self):
        self.assertEqual(archive_episode_identity("VOY-S01E01-LaserDisc-Copies"),
                         ("VOY", "S01E01"))
        self.assertEqual(archive_episode_identity("S02E18-LaserDisc-Copies"),
                         ("DS9", "S02E18"))
        with self.assertRaisesRegex(PipelineError, "conflicting with --series"):
            archive_episode_identity("VOY-S01E01-LaserDisc-Copies", "DS9")

    def test_only_ds9_emissary_uses_the_two_part_initializer_block(self):
        self.assertEqual(archive_episode_identity("VOY-S01E01-LaserDisc-Copies"),
                         ("VOY", "S01E01"))
        with self.assertRaisesRegex(PipelineError, "Emissary is two-part"):
            archive_episode_identity("DS9-S01E01-LaserDisc-Copies")
        with self.assertRaisesRegex(PipelineError, "TNG Archive items are not enabled"):
            archive_episode_identity("TNG-S01E24-LaserDisc-Copies")

    def test_capture_independent_audio_and_avs_paths_are_initialized(self):
        files = episode_files("S01E07", "Q-Less")
        for key in ("avs_log", "avs_trim", "dvd51_source"):
            self.assertIn(key, files)
        self.assertEqual(files["dvd51_source"]["name"], "S01E07-Q-Less-dvd-51-original.ac3")

    def test_mac_profile_uses_vapoursynth_script_extensions(self):
        files = episode_files("S01E10", "Move Along Home", ".vpy")
        self.assertEqual(files["avs_encode"]["name"], "S01E10-encode.vpy")
        self.assertEqual(files["avs_padfix"]["name"], "S01E10-padfix.vpy")

    def test_no_dvd_title_can_be_derived_from_verified_capture_name(self):
        captures = [{"label": "L1", "tbc_file": "DS9-S01E07-Q-Less-L1.tbc"}]
        self.assertEqual(title_from_capture(captures, "S01E07"), "Q-Less")

    def test_capture_and_rate_dependent_paths_remain_unset_at_init(self):
        files = episode_files("S01E07", "Q-Less")
        self.assertNotIn("ld_native", files)
        self.assertNotIn("ld_corrected", files)

    def test_voy_initializer_writes_series_identity_and_voy_manifest_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            (bundle / "episodes" / "VOY").mkdir(parents=True)
            (bundle / "episodes" / "_template.json").write_bytes(
                (ROOT / "episodes" / "_template.json").read_bytes())
            downloads_root = root / "downloads"
            downloads = downloads_root / "VOY S01E01 LaserDisc"
            downloads.mkdir(parents=True)
            file_data = {
                "VOY-S01E01-Caretaker, Part I-L1.tbc": b"synthetic tbc",
                "VOY-S01E01-Caretaker, Part I-L1.tbc.json": b"{}",
                "VOY-S01E01-Caretaker, Part I-L1.efm": b"synthetic efm",
            }
            rows = []
            remote_files = []
            for name, payload in file_data.items():
                (downloads / name).write_bytes(payload)
                digest = hashlib.md5(payload).hexdigest()
                rows.append({"Name": name, "Bytes": str(len(payload)), "MD5": digest,
                             "Status": "verified", "ArchiveId": "VOY-S01E01-LaserDisc-Copies"})
                remote_files.append({"name": name, "size": str(len(payload)), "md5": digest})
            with (downloads / "download-manifest.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            output_roots = {}
            for key in ("work", "probe", "avs", "audio", "subs", "out", "prores", "stack", "logs"):
                output_roots[key] = str(root / key)
            profile = {"os": "windows", "machine": "TEST", "roots": {
                "downloads_root": str(downloads_root),
                "downloads_template": "DS9 {episode} LaserDisc", **output_roots}}
            fake_ld = SimpleNamespace(profile=profile, profile_path=root / "machine.json")
            target = bundle / "episodes" / "VOY" / "S01E01.json"
            remote = {"files": remote_files,
                      "metadata": {"title": "VOY-S01E01-LaserDisc-Copies"}}
            args = ["init_episode.py", "--url",
                    "https://archive.org/details/VOY-S01E01-LaserDisc-Copies",
                    "--downloads", str(downloads), "--no-dvd", "--series", "VOY",
                    "--cut-pad-images", "skip"]
            with patch.object(init_module, "BUNDLE", bundle), \
                    patch.object(init_module, "Ld", return_value=fake_ld), \
                    patch.object(init_module, "episode_manifest_path",
                                 side_effect=lambda code, ld, series=None: target), \
                    patch.object(init_module, "assert_writable_path", side_effect=lambda path, ld: Path(path)), \
                    patch.object(init_module, "urlopen") as urlopen, \
                    patch.object(sys, "argv", args):
                urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(remote).encode()
                self.assertEqual(init_module.main(), 0)
            manifest = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(manifest["episode"], "S01E01")
            self.assertEqual(manifest["series"], "VOY")
            self.assertEqual(manifest["identity"], "VOY:S01E01")
            self.assertEqual(manifest["archive_item"], "VOY-S01E01-LaserDisc-Copies")
            self.assertTrue((root / "probe" / "VOY").is_dir())

    def test_shared_initializer_remembers_user_selected_download_folder_and_external_dvd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            (bundle / "episodes").mkdir(parents=True)
            (bundle / "episodes" / "_template.json").write_bytes(
                (ROOT / "episodes" / "_template.json").read_bytes())
            configured_downloads_root = root / "configured-downloads"
            downloads_root = root / "user-selected-downloads"
            downloads = downloads_root / "DS9 S01E07 LaserDisc"
            downloads.mkdir(parents=True)
            files = {
                "DS9-S01E07-Q-Less-L1.tbc": b"synthetic tbc",
                "DS9-S01E07-Q-Less-L1.tbc.json": b"{}",
            }
            rows, remote_files = [], []
            for name, payload in files.items():
                (downloads / name).write_bytes(payload)
                digest = hashlib.md5(payload).hexdigest()
                rows.append({"Name": name, "Bytes": str(len(payload)), "MD5": digest,
                             "Status": "verified", "ArchiveId": "DS9-S01E07-LaserDisc-Copies"})
                remote_files.append({"name": name, "size": str(len(payload)), "md5": digest})
            with (downloads / "download-manifest.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            configured_dvd_root = root / "configured-dvd"
            configured_dvd_root.mkdir()
            dvd = root / "Video" / "Vortex.mkv"
            dvd.parent.mkdir()
            dvd.write_bytes(b"DVD fixture")
            profile = {"os": "windows", "machine": "TEST", "shared": {
                "enabled": True, "release_root": str(bundle), "device_name": "TEST",
                "device_instance_id": "1234ABCD"},
                "roots": {"downloads_root": str(configured_downloads_root),
                          "downloads_template": "DS9 {episode} LaserDisc",
                          "dvd_root": str(configured_dvd_root)}}
            fake_ld = SimpleNamespace(profile=profile, profile_path=root / "machine.json",
                                      ffprobe=lambda: sys.executable)
            instance = bundle / "devices" / "TEST-1234ABCD" / "episodes" / "S01E07"
            target = instance / "manifest" / "S01E07.json"
            remote = {"files": remote_files,
                      "metadata": {"title": "DS9-S01E07-LaserDisc-Copies"}}
            args = ["init_episode.py", "--url",
                    "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
                    "--downloads", str(downloads), "--series", "DS9", "--dvd", str(dvd)]
            with patch.object(init_module, "BUNDLE", bundle), \
                    patch.object(init_module, "Ld", return_value=fake_ld), \
                    patch.object(init_module, "shared_instance_root", return_value=instance), \
                    patch.object(init_module, "episode_manifest_path",
                                 side_effect=lambda code, ld, series=None: target), \
                    patch.object(init_module, "assert_writable_path", side_effect=lambda path, ld: Path(path)), \
                    patch.object(init_module, "urlopen") as urlopen, \
                    patch.object(init_module.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps({"streams": [{"codec_type": "video"}]}), stderr="")), \
                    patch.object(init_module, "title_from_dvd", return_value="Q-Less"), \
                    patch.object(sys, "argv", args):
                urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(remote).encode()
                self.assertEqual(init_module.main(), 0)

            manifest = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"]["dvd"]["path"], str(dvd.resolve()))
            self.assertEqual(manifest["roots"]["downloads"], str(downloads.resolve()))
            self.assertEqual(manifest["episode"], "S01E07")
            self.assertEqual(manifest["files"]["stack_master"]["root"], "prores")


if __name__ == "__main__":
    unittest.main()
