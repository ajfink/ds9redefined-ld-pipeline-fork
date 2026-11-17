from __future__ import annotations

import sys
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
import record_efm_capture_selection as selection  # noqa: E402
from record_efm_capture_selection import parse_alignment_map  # noqa: E402


class EfmCaptureSelectionTests(unittest.TestCase):
    def test_maps_capture_labels_using_refreshed_orc_input_order(self):
        labels = ["M1", "L1", "L2", "P1", "P2", "P3"]
        self.assertEqual(parse_alignment_map("1+0, 2+0, 3+1, 4+206, 5+30, 6+29", labels),
                         {"M1": 0, "L1": 0, "L2": 1, "P1": 206, "P2": 30, "P3": 29})

    def test_refuses_incomplete_or_misindexed_alignment_map(self):
        with self.assertRaisesRegex(PipelineError, "exactly one entry"):
            parse_alignment_map("1+0, 2+1", ["M1", "L1", "P1"])
        with self.assertRaisesRegex(PipelineError, "source indices"):
            parse_alignment_map("1+0, 3+1", ["M1", "L1"])

    def test_refuses_negative_prepended_pad_counts(self):
        with self.assertRaisesRegex(PipelineError, "negative prepended-pad"):
            parse_alignment_map("1+-1", ["M1"])

    def test_end_to_end_selection_records_orc_order_pad_count_and_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "devices" / "MAC-ABCD1234" / "episodes" / "S01E13"
            references = instance / "references"
            references.mkdir(parents=True)
            downloads = root / "downloads"
            downloads.mkdir()
            audio_root = root / "audio"
            audio_root.mkdir()
            project = root / "project.oxp"
            project.write_text("fixture project", encoding="utf-8")
            captures = []
            provenance = {}
            reports = {}
            for label, bytes_value in (("M1", b"master capture"), ("P3", b"pressing capture")):
                tbc_name = f"DS9-S01E13-{label}.tbc"
                tbc = downloads / tbc_name
                tbc.write_bytes(bytes_value)
                tbc_md5 = hashlib.md5(bytes_value).hexdigest()
                json_name = tbc_name + ".json"
                sidecar = downloads / json_name
                sidecar.write_text(json.dumps({"frames": 2}), encoding="utf-8")
                json_md5 = hashlib.md5(sidecar.read_bytes()).hexdigest()
                captures.append({"label": label, "tbc_file": tbc_name,
                                 "tbc_bytes": len(bytes_value), "tbc_md5": tbc_md5,
                                 "json_file": json_name, "json_bytes": sidecar.stat().st_size,
                                 "json_md5": json_md5})
                wav = audio_root / f"S01E13-{label}-efm.wav"
                wav.write_bytes((label + "-wav").encode())
                efm_text = Path(str(wav) + ".txt")
                efm_text.write_text(label + " EFM report", encoding="utf-8")
                report_data = {
                    "schema": "ld-pipeline-efm-capture-provenance-v1", "status": "VERIFIED",
                    "identity": "DS9:S01E13", "capture": label,
                    "source_tbc": tbc_name, "source_tbc_bytes": len(bytes_value),
                    "source_tbc_manifest_md5": tbc_md5,
                    "source_tbc_fingerprint": {"method": "manifest-md5", "md5": tbc_md5},
                    "source_tbc_json": json_name,
                    "source_tbc_json_bytes": sidecar.stat().st_size,
                    "source_tbc_json_md5": json_md5,
                    "source_tbc_json_fingerprint": {"method": "manifest-md5", "md5": json_md5},
                    "wav_relative_path": wav.name, "wav_size_bytes": wav.stat().st_size,
                    "wav_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
                    "efm_report_relative_path": efm_text.name,
                    "efm_report_size_bytes": efm_text.stat().st_size,
                    "efm_report_sha256": hashlib.sha256(efm_text.read_bytes()).hexdigest(),
                }
                report_path = references / f"{label}-provenance.json"
                report_path.write_text(json.dumps(report_data) + "\n", encoding="utf-8")
                report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
                provenance[label] = [{"report_path": f"references/{label}-provenance.json",
                                      "report_sha256": report_sha,
                                      "wav_sha256": report_data["wav_sha256"]}]
                reports[label] = (report_path, report_sha)
            manifest_path = instance / "manifest.json"
            manifest_data = {
                "episode": "S01E13", "series": "DS9", "identity": "DS9:S01E13",
                "captures": captures, "alignment_map": "1+0, 2+7", "reference": "M1",
                "audio": {"capture": "", "efm_provenance_required": True,
                          "efm_provenance": provenance},
                "files": {"stack_project": {"name": str(project)}},
            }
            manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
            manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            fake_ld = SimpleNamespace(
                profile={"shared": {"enabled": True}}, profile_path=root / "machine.json",
                downloads_dir=lambda *_args: downloads,
                root=lambda key: audio_root if key == "audio" else root)

            class FakeEpisode:
                def __init__(self, _code, data, _ld, _series):
                    self.data = data
                    self.reference = data["reference"]

                def path(self, key):
                    self.assert_key = key
                    return project

            with patch.object(selection, "Ld", return_value=fake_ld), \
                    patch.object(selection, "validate_device_config_profile"), \
                    patch.object(selection, "validate_instance"), \
                    patch.object(selection, "assert_writable_path"), \
                    patch.object(selection, "episode_manifest_path", return_value=manifest_path), \
                    patch("ldcommon.Episode", FakeEpisode), \
                    patch.object(selection, "project_source_labels", return_value=["M1", "P3"]), \
                    patch.dict("os.environ", {"LDP_INSTANCE_ROOT": str(instance)}, clear=False):
                selection.record_selection(
                    "S01E13", "DS9", "P3", reports["P3"][0], reports["P3"][1],
                    manifest_sha, "P3 has the stronger verified decode report", apply=True)

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["audio"]["capture"], "P3")
            self.assertEqual(updated["audio"]["pad_frames"], 7)
            self.assertEqual(updated["audio"]["capture_selection"]["orc_source_order"], ["M1", "P3"])
            self.assertTrue(list(instance.glob("manifest.json.bak-efm-selection-*")))
            self.assertEqual(updated["files"]["ld_native"], {"root": "audio", "name": "S01E13-P3-efm.wav"})


if __name__ == "__main__":
    unittest.main()
