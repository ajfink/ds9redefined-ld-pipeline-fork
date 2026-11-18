from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decode_efm_captures as decoder  # noqa: E402
import verify_efm_capture as verifier  # noqa: E402


class DecodeEfmCaptureStageTests(unittest.TestCase):
    def test_orchestrates_orc_decode_and_safe_provenance_for_every_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "devices" / "TEST-ABCD1234" / "episodes" / "S01E13"
            references = instance / "references"
            logs = instance / "logs"
            manifest_dir = instance / "manifest"
            for folder in (references, logs, manifest_dir):
                folder.mkdir(parents=True)
            downloads = root / "downloads"
            audio = root / "audio"
            work = root / "work"
            for folder in (downloads, audio, work):
                folder.mkdir()
            profile_path = root / "machine.json"
            profile_path.write_text("{}\n", encoding="utf-8")
            orc_path = root / "orc-cli.exe"
            orc_path.write_bytes(b"test executable placeholder")
            sevenzip_path = root / "7z.exe"
            sevenzip_path.write_bytes(b"test executable placeholder")
            stack_project = root / "stack.orcprj"
            stack_project.write_text(
                "project:\n  video_format: NTSC\n  source_format: Composite\n"
                "  amplitude_unit: 10bit\n", encoding="utf-8")

            captures = []
            for label in ("L1", "P1"):
                tbc = downloads / f"{label}.tbc"
                tbc.write_bytes((label + "-tbc").encode())
                metadata = downloads / f"{label}.tbc.json"
                metadata.write_text('{"frames": 100}\n', encoding="utf-8")
                zipped = label == "P1"
                efm = downloads / (f"{label}.efm.7z" if zipped else f"{label}.efm")
                efm.write_bytes((label + "-archive").encode() if zipped else
                                (label + "-raw-efm").encode())
                captures.append({
                    "label": label, "tbc_file": tbc.name,
                    "tbc_bytes": tbc.stat().st_size,
                    "tbc_md5": hashlib.md5(tbc.read_bytes()).hexdigest(),
                    "json_file": metadata.name, "json_bytes": metadata.stat().st_size,
                    "json_md5": hashlib.md5(metadata.read_bytes()).hexdigest(),
                    "efm_file": efm.name, "efm_bytes": efm.stat().st_size,
                    "efm_md5": hashlib.md5(efm.read_bytes()).hexdigest(),
                })
            manifest_path = manifest_dir / "S01E13.json"
            manifest = {
                "episode": "S01E13", "series": "DS9", "identity": "DS9:S01E13",
                "captures": captures, "reference": "L1", "alignment_map": "1+0, 2+1",
                "audio": {"efm_provenance_required": True, "efm_provenance": {}},
                "files": {"stack_project": {"path": str(stack_project)}},
            }
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            class FakeLd:
                def __init__(self, _profile_path=None):
                    self.profile_path = profile_path
                    self.profile = {
                        "shared": {"enabled": True},
                        "roots": {"audio": str(audio), "work": str(work), "logs": str(logs)},
                        "tools": {"orc": str(orc_path), "sevenzip": str(sevenzip_path)},
                    }

                def root(self, key):
                    return {"audio": audio, "work": work, "logs": logs}[key]

                def tool(self, key):
                    return {"orc": str(orc_path)}[key]

            class FakeEpisode:
                def __init__(self, _identity, _ld, series=None):
                    self.data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    self._downloads = downloads

                def downloads(self):
                    return self._downloads

                def path(self, key):
                    if key != "stack_project":
                        raise AssertionError(key)
                    return stack_project

            def fake_orc(command, capture):
                project_file = Path(command[1])
                values = re.findall(r'(?m)^\s+value: "([^"]+)"\s*$', project_file.read_text(encoding="utf-8"))
                tbc, _efm, wav_value = values[:3]
                wav_path = Path(wav_value)
                report_path = Path(str(wav_path) + ".txt")
                with wave.open(str(wav_path), "wb") as output:
                    output.setnchannels(2)
                    output.setsampwidth(2)
                    output.setframerate(44100)
                    output.writeframes(b"\0\0\0\0" * 4410)
                report_path.write_text(
                    "Overall assessment : GOOD\n"
                    "Data loss (post-C2) : 0.01 %\n"
                    "C1 uncorrectable : 0 (0.00 %)\n"
                    "C2 uncorrectable : 0 (0.00 %)\n"
                    "Sections missing : 0\n"
                    "Concealed (interp) : 0\n"
                    "Silenced (muted) : 0\n", encoding="utf-8")
                tbc_path = Path(tbc)
                log_path = Path(command[command.index("--log-file") + 1])
                log_path.write_text("\n".join((
                    f"tbc_source: using legacy JSON metadata: {tbc_path}.json",
                    f"tbc_source: Loaded '{tbc_path}' — NTSC TBC Composite (100 frames)",
                    f"EFMSink: mode=audio, output={wav_path}",
                    f"Writing decoding report to: {report_path}",
                    f"EFMSink: Success: EFM decoded to {wav_path}",
                )), encoding="utf-8")

            def fake_7z_listing(_command, **_kwargs):
                return SimpleNamespace(
                    returncode=0, stderr=b"",
                    stdout=(b"Header\n----------\nPath = P1.efm.7z\nType = 7z\n\n"
                            b"----------\nPath = P1.efm\nSize = 10\nFolder = -\n"))

            def fake_extract(_sevenzip, _archive, _member, _size, output):
                output.write_bytes(b"P1-raw-efm")

            env_patch = patch.dict(os.environ, {
                "LDP_INSTANCE_ROOT": str(instance),
                "LDP_PROFILE": str(profile_path),
            }, clear=False)
            with env_patch, \
                    patch.object(decoder, "Ld", FakeLd), \
                    patch.object(verifier, "Ld", FakeLd), \
                    patch.object(decoder, "load_episode", FakeEpisode), \
                    patch.object(verifier, "load_episode", FakeEpisode), \
                    patch.object(decoder, "episode_manifest_path", return_value=manifest_path), \
                    patch.object(verifier, "episode_manifest_path", return_value=manifest_path), \
                    patch.object(decoder, "validate_device_config_profile"), \
                    patch.object(decoder, "validate_instance"), \
                    patch.object(verifier, "validate_device_config_profile"), \
                    patch.object(verifier, "validate_instance"), \
                    patch.object(decoder, "assert_writable_path"), \
                    patch.object(verifier, "assert_writable_path"), \
                    patch.object(decoder, "_run_orc", side_effect=fake_orc), \
                    patch.object(decoder.subprocess, "run", side_effect=fake_7z_listing), \
                    patch.object(decoder, "extract_7z_member", side_effect=fake_extract):
                result = decoder.decode_captures(
                    "S01E13", "DS9", ["L1", "P1"], "20261117T120000Z-a1b2c3d4",
                    hashlib.sha256(manifest_path.read_bytes()).hexdigest(), profile_path)

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(updated["audio"]["efm_provenance"]), {"L1", "P1"})
            self.assertEqual(len(result), 11)
            self.assertTrue(list(manifest_dir.glob("S01E13.json.bak-efm-provenance-*")))
            self.assertTrue(all(Path(path).is_file() for path in result))
            for label in ("L1", "P1"):
                provenance = updated["audio"]["efm_provenance"][label][-1]
                report_path = instance / provenance["report_path"]
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["source_efm_file"],
                                 f"{label}.efm.7z" if label == "P1" else f"{label}.efm")
                self.assertEqual(report["decoded_efm"]["size_bytes"],
                                 len(b"P1-raw-efm") if label == "P1" else len(b"L1-raw-efm"))
                self.assertIn(f"-{label}-", report["orc_project_name"])
            retained = list((work / "efm-decodes" / "S01E13").glob("P1-*.efm"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"P1-raw-efm")


if __name__ == "__main__":
    unittest.main()
