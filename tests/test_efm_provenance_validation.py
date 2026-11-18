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
sys.path.insert(0, str(ROOT / "scripts"))

from validate_episode import Checks, validate_efm_capture_provenance  # noqa: E402


class EfmProvenanceValidationTests(unittest.TestCase):
    def test_selected_wav_must_match_verified_capture_and_log_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "devices" / "MAC-ABCD1234" / "episodes" / "S01E13"
            references = instance / "references"
            references.mkdir(parents=True)
            audio_root = root / "audio"
            audio_root.mkdir()
            wav = audio_root / "S01E13-P3-efm.wav"
            wav.write_bytes(b"native EFM wave")
            efm_report = Path(str(wav) + ".txt")
            efm_report.write_text("EFMSink report", encoding="utf-8")
            downloads = root / "downloads"
            downloads.mkdir()
            tbc = downloads / "DS9-S01E13-P3.tbc"
            tbc.write_bytes(b"x" * 100)
            tbc_md5 = hashlib.md5(b"x" * 100).hexdigest()
            metadata = downloads / (tbc.name + ".json")
            metadata.write_text('{"frames":100}\n', encoding="utf-8")
            metadata_md5 = hashlib.md5(metadata.read_bytes()).hexdigest()
            efm = downloads / "DS9-S01E13-P3.efm"
            efm.write_bytes(b"raw efm bytes")
            efm_md5 = hashlib.md5(efm.read_bytes()).hexdigest()
            efm_report_rel = efm_report.relative_to(audio_root).as_posix()
            report = {
                "schema": "ld-pipeline-efm-capture-provenance-v1", "status": "VERIFIED",
                "identity": "DS9:S01E13", "capture": "P3",
                "source_tbc": tbc.name, "source_tbc_bytes": 100,
                "source_tbc_manifest_md5": tbc_md5,
                "source_tbc_fingerprint": {"method": "manifest-md5", "md5": tbc_md5},
                "source_tbc_json": metadata.name,
                "source_tbc_json_bytes": metadata.stat().st_size,
                "source_tbc_json_md5": metadata_md5,
                "source_tbc_json_fingerprint": {"method": "manifest-md5", "md5": metadata_md5},
                "source_efm_file": efm.name, "source_efm_bytes": efm.stat().st_size,
                "source_efm_manifest_md5": efm_md5,
                "source_efm_fingerprint": {"method": "manifest-md5", "md5": efm_md5},
                "decoded_efm": {"root": "downloads", "relative_path": efm.name,
                                "sha256": hashlib.sha256(efm.read_bytes()).hexdigest(),
                                "size_bytes": efm.stat().st_size},
                "path_checks": {key: True for key in
                                ("loaded_tbc", "tbc_metadata", "efm_output", "efm_report", "efm_success")},
                "wav_relative_path": wav.name, "wav_size_bytes": wav.stat().st_size,
                "wav_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
                "efm_report_relative_path": efm_report_rel,
                "efm_report_size_bytes": efm_report.stat().st_size,
                "efm_report_sha256": hashlib.sha256(efm_report.read_bytes()).hexdigest(),
            }
            report_path = references / "efm-provenance.json"
            report_bytes = (json.dumps(report, indent=2) + "\n").encode("utf-8")
            report_path.write_bytes(report_bytes)
            manifest_hash = hashlib.sha256(report_bytes).hexdigest()
            audio_data = {"efm_provenance_required": True,
                          "pad_frames": 0,
                          "capture_selection": {
                              "schema": "ld-pipeline-efm-capture-selection-v1", "capture": "P3",
                              "verified_provenance_sha256": manifest_hash,
                              "verified_provenance_report": "references/efm-provenance.json",
                              "selection_note": "selected after reviewing candidate reports",
                              "alignment_map": "1+0, 2+0",
                              "pad_frames": 0,
                          },
                          "efm_provenance": {"P3": [{
                              "report_path": "references/efm-provenance.json",
                              "report_sha256": manifest_hash,
                              "wav_sha256": report["wav_sha256"],
                          }]}}
            episode = SimpleNamespace(identity="DS9:S01E13", data={
                "audio": audio_data, "alignment_map": "1+0, 2+0",
            }, downloads=lambda: downloads)
            capture = {"label": "P3", "tbc_file": tbc.name, "tbc_bytes": 100,
                       "tbc_md5": tbc_md5, "json_file": metadata.name,
                       "json_bytes": metadata.stat().st_size, "json_md5": metadata_md5,
                       "efm_file": efm.name, "efm_bytes": efm.stat().st_size,
                       "efm_md5": efm_md5}
            ld = SimpleNamespace(root=lambda key: audio_root if key == "audio" else root)
            checks = Checks()
            with patch.dict(os.environ, {"LDP_INSTANCE_ROOT": str(instance)}):
                validate_efm_capture_provenance(
                    checks, episode, ld, capture, wav)
            self.assertFalse(checks.failed)
            efm.write_bytes(b"changed raw EFM")
            changed = Checks()
            with patch.dict(os.environ, {"LDP_INSTANCE_ROOT": str(instance)}):
                validate_efm_capture_provenance(
                    changed, episode, ld, capture, wav)
            self.assertTrue(any(name == "efm-provenance-source" and status == "FAIL"
                                for status, name, _detail in changed.rows))

    def test_report_bound_to_another_episode_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            references = instance / "references"
            references.mkdir(parents=True)
            audio_root = root / "audio"
            audio_root.mkdir()
            wav = audio_root / "capture.wav"
            wav.write_bytes(b"wav")
            report = {"schema": "ld-pipeline-efm-capture-provenance-v1", "status": "VERIFIED",
                      "identity": "DS9:S01E10", "capture": "P3"}
            report_path = references / "wrong.json"
            report_bytes = (json.dumps(report) + "\n").encode()
            report_path.write_bytes(report_bytes)
            episode = SimpleNamespace(identity="DS9:S01E13", data={
                "audio": {"efm_provenance_required": True, "efm_provenance": {"P3": [{
                    "report_path": "references/wrong.json",
                    "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
                }]}}
            })
            checks = Checks()
            ld = SimpleNamespace(root=lambda _key: audio_root)
            with patch.dict(os.environ, {"LDP_INSTANCE_ROOT": str(instance)}):
                validate_efm_capture_provenance(
                    checks, episode, ld,
                    {"label": "P3", "tbc_file": "DS9-S01E13-P3.tbc", "tbc_bytes": 1, "tbc_md5": "x"},
                    wav)
            self.assertTrue(checks.failed)
            self.assertTrue(any(name == "efm-provenance-identity" and status == "FAIL"
                                for status, name, _detail in checks.rows))

    def test_legacy_manifest_warns_instead_of_silently_claiming_provenance(self):
        checks = Checks()
        episode = SimpleNamespace(identity="DS9:S01E13", data={"audio": {}})
        with patch.dict(os.environ, {}, clear=True):
            validate_efm_capture_provenance(checks, episode, None, {"label": "P3"}, None)
        self.assertFalse(checks.failed)
        self.assertEqual(checks.rows[0][0], "WARN")


if __name__ == "__main__":
    unittest.main()
