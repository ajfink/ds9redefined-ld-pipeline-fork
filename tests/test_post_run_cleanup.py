from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import post_run_cleanup  # noqa: E402
from post_run_cleanup import (CleanupError, LARGE_AUDIO_BYTES, _file_fingerprint,
                              build_cleanup_inventory, cleanup_review_available,
                              execute_cleanup)  # noqa: E402


class FakeLd:
    def __init__(self, roots: dict[str, Path]):
        self.profile = {"shared": {"enabled": False}}
        self.roots = roots

    def root(self, key: str) -> Path:
        return self.roots[key]


class PostRunCleanupTests(unittest.TestCase):
    def test_post_run_review_requires_successful_executed_qc_plan(self):
        qc_plan = {"steps": [{"stage": "qc"}]}
        self.assertTrue(cleanup_review_available("0", "run", qc_plan))
        self.assertTrue(cleanup_review_available("0", "resume", qc_plan))
        self.assertFalse(cleanup_review_available("0", "preview", qc_plan))
        self.assertFalse(cleanup_review_available("3", "run", qc_plan))
        self.assertFalse(cleanup_review_available("0", "run", {"steps": [{"stage": "encode-final"}]}))

    def _fixture(self, root: Path, *, qc_report_text: str | None = None):
        downloads = root / "downloads" / "DS9 S01E07 LaserDisc"
        downloads.mkdir(parents=True)
        (downloads / "capture-L1.tbc").write_bytes(b"tbc")
        (downloads / "capture-L1.tbc.json").write_text('{"source":"capture-L1.tbc"}\n', encoding="utf-8")
        (downloads / "download-manifest.csv").write_text("Name,Size\ncapture-L1.tbc,3\n", encoding="utf-8")
        (downloads / "item.txt").write_text("Archive item metadata\n", encoding="utf-8")

        audio_root = root / "audio"
        audio_root.mkdir()
        large_audio = audio_root / "S01E07-L1-efm.wav"
        with large_audio.open("wb") as stream:
            stream.truncate(LARGE_AUDIO_BYTES + 1)
        small_audio = audio_root / "S01E07-L1-corrected.wav"
        small_audio.write_bytes(b"small audio")

        out_root = root / "out"
        out_root.mkdir()
        deliverable = out_root / "S01E07-final.mkv"
        deliverable.write_bytes(b"validated deliverable")
        qc_report = root / "probe" / "S01E07-qc-report-run.md"
        qc_report.parent.mkdir()
        qc_report.write_text(qc_report_text or (
            "# Delivery QC\n\n| gate | status | detail |\n|---|---|---|\n"
            "| decode | PASS | all frames decoded |\n"
            "| optional subtitle | SKIP | no subtitle track requested |\n\n"
            "**2 pass, 0 warn, 1 skip, 0 fail/unverified**\n"), encoding="utf-8")
        instance = root / "devices" / "TEST-1234ABCD" / "episodes" / "S01E07"
        (instance / "logs").mkdir(parents=True)
        manifest = instance / "manifest" / "S01E07.json"
        manifest.parent.mkdir()
        manifest.write_text("{}\n", encoding="utf-8")
        profile = root / "machine.json"
        profile.write_text("{}\n", encoding="utf-8")
        roots = {"dvd_root": root / "dvd", "prores": root / "prores",
                 "out": out_root, "audio": audio_root}
        for path in roots.values():
            path.mkdir(exist_ok=True)
        context = {
            "episode": "S01E07", "series": "DS9", "identity": "DS9:S01E07",
            "downloads": downloads, "audio_root": audio_root, "instance": instance,
            "profile": profile, "manifest": manifest, "ld": FakeLd(roots),
            "captures": [{"label": "L1", "tbc_file": "capture-L1.tbc",
                          "json_file": "capture-L1.tbc.json"}],
            "file_paths": {"pre_upscale": str(deliverable), "ld_native": str(large_audio),
                           "ld_corrected": str(small_audio)},
            "manifest_data": {"deliverable": "pre_upscale", "files": {
                "pre_upscale": {"root": "out", "name": deliverable.name},
                "ld_native": {"root": "audio", "name": large_audio.name},
                "ld_corrected": {"root": "audio", "name": small_audio.name},
            }},
        }
        plan = {"episode": "S01E07", "series": "DS9", "fingerprint_policy": "balanced", "steps": [
            {"id": "delivery-qc", "stage": "qc", "outputs": [str(qc_report)]}]}
        plan_path = root / "delivery-plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        state_path = instance / "logs" / "workflow-test.json"
        state_path.write_text(json.dumps({
            "status": "PASS", "episode": "S01E07", "series": "DS9", "job_id": "test",
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "steps": [{"id": "delivery-qc", "status": "PASS", "exit_code": 0,
                       "input_fingerprints": [_file_fingerprint(deliverable, "balanced"),
                                              _file_fingerprint(manifest, "balanced")],
                       "output_fingerprints": [_file_fingerprint(qc_report, "balanced")]}],
        }), encoding="utf-8")
        return context, plan, plan_path, state_path, large_audio, small_audio, downloads

    def test_inventory_requires_clean_qc_and_lists_episode_scoped_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, plan, plan_path, state_path, large_audio, small_audio, downloads = self._fixture(Path(temporary))
            inventory = build_cleanup_inventory(context, plan, state_path, plan_path)
            self.assertEqual({row.key for row in inventory.candidates}, {"downloads", next(
                row.key for row in inventory.candidates if row.path == large_audio)})
            self.assertNotIn(str(small_audio), {str(row.path) for row in inventory.candidates})
            self.assertEqual(inventory.downloads, downloads.resolve())
            self.assertEqual({path.name for path in inventory.metadata_paths},
                             {"capture-L1.tbc.json", "download-manifest.csv", "item.txt"})

    def test_warn_or_limit_qc_blocks_source_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, plan, plan_path, state_path, *_ = self._fixture(
                Path(temporary), qc_report_text=(
                    "| gate | status | detail |\n|---|---|---|\n| sync | LIMIT | unresolved limit |\n"
                    "**1 pass, 1 warn, 0 skip, 0 fail/unverified**\n"))
            with self.assertRaisesRegex(CleanupError, "zero warnings/limits"):
                build_cleanup_inventory(context, plan, state_path, plan_path)

    def test_unsupported_trash_platform_stops_without_deleting(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preserve-me.wav"
            path.write_bytes(b"user data")
            with patch.object(post_run_cleanup.sys, "platform", "linux"):
                with self.assertRaisesRegex(CleanupError, "not enabled on this platform"):
                    post_run_cleanup.send_to_trash(path)
            self.assertTrue(path.is_file())

    def test_changed_workflow_plan_or_incomplete_job_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, plan, plan_path, state_path, *_ = self._fixture(Path(temporary))
            plan["episode_title"] = "Changed plan"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(CleanupError, "does not match"):
                build_cleanup_inventory(context, plan, state_path, plan_path)

    def test_changed_delivery_after_qc_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, plan, plan_path, state_path, *_ = self._fixture(Path(temporary))
            Path(context["file_paths"]["pre_upscale"]).write_bytes(b"changed after QC")
            with self.assertRaisesRegex(CleanupError, "final deliverable changed after QC"):
                build_cleanup_inventory(context, plan, state_path, plan_path)

    def test_changed_qc_report_after_workflow_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, plan, plan_path, state_path, *_ = self._fixture(Path(temporary))
            report = Path(plan["steps"][0]["outputs"][0])
            report.write_text(report.read_text(encoding="utf-8") + "modified\n", encoding="utf-8")
            with self.assertRaisesRegex(CleanupError, "QC report changed after QC"):
                build_cleanup_inventory(context, plan, state_path, plan_path)

    def test_changed_manifest_after_qc_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, plan, plan_path, state_path, *_ = self._fixture(Path(temporary))
            Path(context["manifest"]).write_text('{"updated":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(CleanupError, "episode manifest changed after QC"):
                build_cleanup_inventory(context, plan, state_path, plan_path)

    def test_confirmed_cleanup_preserves_metadata_and_logs_before_trashing_selected_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, plan, plan_path, state_path, large_audio, _small_audio, downloads = self._fixture(root)
            inventory = build_cleanup_inventory(context, plan, state_path, plan_path)
            moved = []

            def fake_trash(path: Path) -> None:
                moved.append(path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()

            result = execute_cleanup(inventory, {row.key for row in inventory.candidates},
                                     context, plan, state_path, plan_path, trash=fake_trash)
            self.assertTrue(result["complete"])
            self.assertEqual({path for path in moved}, {downloads.resolve(), large_audio.resolve()})
            self.assertFalse(downloads.exists())
            self.assertFalse(large_audio.exists())
            preserved_root = context["instance"] / "references" / "post-run-cleanup"
            preserved = list(preserved_root.rglob("*"))
            self.assertTrue(any(path.name == "download-manifest.csv" and path.is_file() for path in preserved))
            self.assertTrue(any(path.name == "capture-L1.tbc.json" and path.is_file() for path in preserved))
            self.assertTrue(context["manifest"].is_file())
            self.assertTrue(context["instance"].joinpath("logs").is_dir())
            self.assertTrue(result["record"].is_file())


if __name__ == "__main__":
    unittest.main()
