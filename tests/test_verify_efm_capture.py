from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
from verify_efm_capture import verify_efm_log, verify_source_tbc  # noqa: E402


class VerifyEfmCaptureTests(unittest.TestCase):
    def test_matching_log_paths_are_bound_to_the_expected_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tbc = root / "DS9 S01E13 Battle Lines P3.tbc"
            wav = root / "S01E13 P3 efm.wav"
            report = Path(str(wav) + ".txt")
            log = root / "orc.log"
            log.write_text("\n".join((
                f"[2026-11-14 11:27:53.781] [core] [info] tbc_source: using legacy JSON metadata: {tbc}.json",
                f"[2026-11-14 11:27:54.328] [core] [info] tbc_source: Loaded '{tbc}' — NTSC TBC Composite (82877 frames)",
                f"[2026-11-14 11:27:54.328] [core] [info] EFMSink: mode=audio, output={wav}",
                f"[2026-11-14 11:28:42.635] [core] [info] Writing decoding report to: {report}",
                f"[2026-11-14 11:28:42.637] [core] [info] EFMSink: Success: EFM decoded to {wav}",
            )), encoding="utf-8")

            result = verify_efm_log(log, tbc, wav, report)

            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(len(result["log_sha256"]), 64)

    def test_stale_cross_episode_log_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = root / "DS9-S01E13-Battle-Lines-P3.tbc"
            stale = root / "DS9-S01E10-Move-Along-Home-P3.tbc"
            wav = root / "S01E13-P3-efm.wav"
            report = Path(str(wav) + ".txt")
            log = root / "reconstructed-s01e13-efm.log"
            log.write_text("\n".join((
                f"tbc_source: using legacy JSON metadata: {stale}.json",
                f"tbc_source: Loaded '{stale}' — NTSC TBC",
                f"EFMSink: mode=audio, output={root / 'S01E10-P3-efm.wav'}",
                f"Writing decoding report to: {root / 'S01E10-P3-efm.wav.txt'}",
                f"EFMSink: Success: EFM decoded to {root / 'S01E10-P3-efm.wav'}",
            )), encoding="utf-8")

            with self.assertRaisesRegex(PipelineError, "loaded_tbc.*tbc_metadata.*efm_output"):
                verify_efm_log(log, expected, wav, report)

    def test_tbc_manifest_md5_is_rechecked_against_current_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            tbc = Path(temporary) / "capture.tbc"
            tbc.write_bytes(b"current capture")
            capture = {"label": "P3", "tbc_file": tbc.name, "tbc_bytes": tbc.stat().st_size,
                       "tbc_md5": "0" * 32}
            with self.assertRaisesRegex(PipelineError, "TBC MD5 differs"):
                verify_source_tbc(tbc, capture, {})

    def test_local_inventory_fingerprint_is_supported_without_archive_md5(self):
        with tempfile.TemporaryDirectory() as temporary:
            tbc = Path(temporary) / "capture.tbc"
            tbc.write_bytes(b"local capture")
            from local_source import fingerprint_file
            fingerprint = fingerprint_file(tbc, "full")
            capture = {"label": "P3", "tbc_file": tbc.name, "tbc_bytes": tbc.stat().st_size,
                       "tbc_md5": ""}
            manifest = {"source_provenance": {
                "mode": "local-inventory",
                "inventory": {"fingerprint_policy": "full", "captures": [{
                    "label": "P3", "files": {"tbc": {"name": tbc.name, **fingerprint}}
                }]},
            }}
            result = verify_source_tbc(tbc, capture, manifest)
            self.assertEqual(result, {"method": "local-inventory-sha256", **fingerprint})


if __name__ == "__main__":
    unittest.main()
