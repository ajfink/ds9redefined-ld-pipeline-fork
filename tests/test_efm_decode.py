from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from efm_decode import (build_decode_project, extract_7z_member,
                        project_formats, sevenzip_efm_member)  # noqa: E402


class EfmDecodeHelperTests(unittest.TestCase):
    def test_project_uses_explicit_efm_sidecar_audio_sink_and_escaped_paths(self):
        project = build_decode_project(
            episode="S01E18", capture="P1",
            tbc=Path("C:/DS9 Capture/capture 'P1'.tbc"),
            efm=Path("C:/DS9 Capture/capture 'P1'.efm"),
            wav=Path("C:/audio/S01E18/P1-efm.wav"),
            video_format="NTSC", source_format="Composite", amplitude_unit="10bit")
        self.assertIn("stage: tbc_source", project)
        self.assertIn("        efm_path:", project)
        self.assertIn("stage: EFMSink", project)
        self.assertIn("          value: audio", project)
        self.assertIn("          value: true", project)
        self.assertIn("capture 'P1'.efm", project)
        self.assertIn("  edges:", project)

    def test_source_formats_are_read_from_existing_orc_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "stack.orcprj"
            project.write_text(
                "project:\n  video_format: NTSC\n  source_format: YC\n  amplitude_unit: 10bit\n",
                encoding="utf-8")
            self.assertEqual(project_formats(project), ("NTSC", "YC", "10bit"))
            project.write_text(
                "project:\n  video_format: SECAM\n  source_format: Weird\n  amplitude_unit: IRE\n",
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "supported video/source format"):
                project_formats(project)

    def test_archive_member_must_be_one_exact_safe_efm(self):
        listing = """Header
----------
Path = sample.7z
Type = 7z

----------
Path = nested/S01E18-P1.efm
Size = 1234
Packed Size = 1000
Folder = -
"""
        self.assertEqual(sevenzip_efm_member(listing, "S01E18-P1.efm"),
                         ("nested/S01E18-P1.efm", 1234))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            sevenzip_efm_member(listing, "other.efm")
        malicious = listing.replace("nested/S01E18-P1.efm", "../S01E18-P1.efm")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            sevenzip_efm_member(malicious, "S01E18-P1.efm")
        link = listing.replace("Folder = -", "Folder = -\nSymbolic Link = target.efm")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            sevenzip_efm_member(link, "S01E18-P1.efm")

    def test_extract_stream_requires_expected_size_and_new_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "raw.efm"
            with patch("efm_decode.subprocess.run",
                       side_effect=lambda _cmd, stdout, **_kwargs:
                       (stdout.write(b"abc"), SimpleNamespace(returncode=0, stderr=b""))[1]):
                extract_7z_member(root / "7z.exe", root / "source.7z",
                                  "member.efm", 3, output)
            self.assertEqual(output.read_bytes(), b"abc")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                extract_7z_member(root / "7z.exe", root / "source.7z",
                                  "member.efm", 3, output)

    def test_extract_stream_discards_size_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "raw.efm"
            with patch("efm_decode.subprocess.run",
                       side_effect=lambda _cmd, stdout, **_kwargs:
                       (stdout.write(b"too large"), SimpleNamespace(returncode=0, stderr=b""))[1]):
                with self.assertRaisesRegex(ValueError, "differs from archive listing"):
                    extract_7z_member(root / "7z.exe", root / "source.7z",
                                      "member.efm", 3, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
