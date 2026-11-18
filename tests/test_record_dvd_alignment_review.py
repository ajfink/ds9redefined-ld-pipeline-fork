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
sys.path.insert(0, str(ROOT / "scripts"))

import record_dvd_alignment_review as recorder  # noqa: E402
from ldcommon import PipelineError  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecordDvdAlignmentReviewTests(unittest.TestCase):
    def _fixture(self, root: Path):
        probe = root / "probe"
        probe.mkdir()
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
            "video": {}, "files": {},
        }), encoding="utf-8")
        master, dvd = root / "master.mkv", root / "dvd.mkv"
        master.write_bytes(b"master video")
        dvd.write_bytes(b"dvd video")
        master_desc, dvd_desc = probe / "master.npy", probe / "dvd.npy"
        master_desc.write_bytes(b"master desc")
        dvd_desc.write_bytes(b"dvd desc")
        master_meta, dvd_meta = Path(str(master_desc) + ".meta.json"), Path(str(dvd_desc) + ".meta.json")
        master_meta.write_text('{"role":"master"}\n', encoding="utf-8")
        dvd_meta.write_text('{"role":"dvd"}\n', encoding="utf-8")
        alignment, diagnostics = probe / "alignment.npy", probe / "alignment.tsv"
        alignment.write_bytes(b"alignment candidate")
        diagnostics.write_text("master_frame\tvalue\n10\t0\n", encoding="utf-8")
        contact, playback = probe / "contact.png", probe / "pair.gif"
        contact.write_bytes(b"contact sheet")
        playback.write_bytes(b"paired playback")
        index = probe / "review" / "index.json"
        index.parent.mkdir()
        index.write_text(json.dumps({
            "schema": "dvd-alignment-review-assets-v1", "identity": "DS9:S01E12",
            "candidate_is_approval": False,
            "manifest_sha256": digest(manifest),
            "alignment_path": str(alignment), "alignment_sha256": digest(alignment),
            "diagnostics_path": str(diagnostics), "diagnostics_sha256": digest(diagnostics),
            "master_source": str(master), "master_source_sha256": digest(master),
            "dvd_source": str(dvd), "dvd_source_sha256": digest(dvd),
            "master_descriptors": str(master_desc), "master_descriptors_sha256": digest(master_desc),
            "master_descriptors_meta": str(master_meta),
            "master_descriptors_meta_sha256": digest(master_meta),
            "dvd_descriptors": str(dvd_desc), "dvd_descriptors_sha256": digest(dvd_desc),
            "dvd_descriptors_meta": str(dvd_meta),
            "dvd_descriptors_meta_sha256": digest(dvd_meta),
            "contact_sheet": str(contact), "contact_sheet_sha256": digest(contact),
            "review_points": [{"asset_status": "READY", "playback_path": str(playback),
                               "playback_sha256": digest(playback)}],
        }), encoding="utf-8")
        ld = SimpleNamespace(probe=lambda: probe, profile={})

        class Episode:
            code, series, identity = "S01E12", "DS9", "DS9:S01E12"
            def __init__(self):
                self.ld = ld
            def path(self, key):
                return {"stack_master": master, "dvd": dvd}[key]

        return (probe, manifest, alignment, diagnostics, index,
                Episode(), ld)

    def test_approval_records_exact_hash_marker_with_atomic_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, manifest, alignment, diagnostics, index, ep, ld = self._fixture(Path(temporary))
            original_hash = digest(manifest)
            report_out = probe / "approved-review.json"
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)):
                result = recorder.record_review(
                    ep, ld, alignment=alignment, diagnostics=diagnostics, assets_index=index,
                    decision="APPROVED", review_note="Matched scene anchors visually",
                    expected_manifest_sha256=original_hash,
                    expected_alignment_sha256=digest(alignment),
                    expected_diagnostics_sha256=digest(diagnostics),
                    expected_assets_sha256=digest(index), report_out=report_out)
            updated = json.loads(manifest.read_text(encoding="utf-8"))
            marker = updated["video"]["dvd_alignment_review"]
            self.assertEqual(marker["status"], "APPROVED")
            self.assertEqual(marker["sha256"], digest(alignment))
            self.assertEqual(updated["files"]["align_npy"]["name"], alignment.relative_to(probe).as_posix())
            self.assertEqual(updated["files"]["master_descriptors"]["name"], "master.npy")
            self.assertEqual(updated["files"]["dvd_descriptors"]["name"], "dvd.npy")
            self.assertTrue(Path(result["manifest_backup"]).is_file())
            self.assertTrue(result["manifest_updated"])
            self.assertTrue(report_out.is_file())

    def test_rejection_is_audited_without_manifest_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, manifest, alignment, diagnostics, index, ep, ld = self._fixture(Path(temporary))
            original = manifest.read_bytes()
            report_out = probe / "rejected-review.json"
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)):
                result = recorder.record_review(
                    ep, ld, alignment=alignment, diagnostics=diagnostics, assets_index=index,
                    decision="REJECTED", review_note="Offset path did not match scenes",
                    expected_manifest_sha256=digest(manifest),
                    expected_alignment_sha256=digest(alignment),
                    expected_diagnostics_sha256=digest(diagnostics),
                    expected_assets_sha256=digest(index), report_out=report_out)
            self.assertEqual(manifest.read_bytes(), original)
            self.assertFalse(result["manifest_updated"])
            self.assertEqual(json.loads(report_out.read_text())["decision"], "REJECTED")

    def test_stale_alignment_hash_refuses_approval_without_manifest_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe, manifest, alignment, diagnostics, index, ep, ld = self._fixture(Path(temporary))
            original = manifest.read_bytes()
            with patch.object(recorder, "episode_manifest_path", return_value=manifest), \
                    patch.object(recorder, "assert_writable_path",
                                 side_effect=lambda path, _ld=None: Path(path)):
                with self.assertRaisesRegex(PipelineError, "differs from the reviewed candidate"):
                    recorder.record_review(
                        ep, ld, alignment=alignment, diagnostics=diagnostics, assets_index=index,
                        decision="APPROVED", review_note="Matched scene anchors visually",
                        expected_manifest_sha256=digest(manifest),
                        expected_alignment_sha256="wrong",
                        expected_diagnostics_sha256=digest(diagnostics),
                        expected_assets_sha256=digest(index), report_out=probe / "stale.json")
            self.assertEqual(manifest.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
