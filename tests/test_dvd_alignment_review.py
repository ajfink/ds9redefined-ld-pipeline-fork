from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_alignment_review_assets as assets  # noqa: E402
from workflow_app_model import WorkflowAppError  # noqa: E402
from dvd_alignment_review import DvdAlignmentReviewWindow  # noqa: E402
from run_workflow import load_plan  # noqa: E402
from workflow_workbenches import (build_dvd_alignment_review_plan,
                                  build_dvd_alignment_review_decision_plan)  # noqa: E402


class AlignmentReviewTests(unittest.TestCase):
    def test_review_points_cover_edges_weak_matches_transitions_and_measured_anchor(self):
        rows = [
            {"master_frame": 200 + i * 30, "master_time_s": (200 + i * 30) / assets.FPS,
             "offset_frames": 10 if i < 3 else 80, "dvd_frame": 210 + i * 30 if i < 3 else 280 + i * 30,
             "ncc": 0.9 if i not in (1, 4) else 0.2,
             "local_margin": 0.4 if i != 5 else 0.01,
             "search_edge": i in (0, 5), "row": i}
            for i in range(6)
        ]
        points, deferred = assets.select_review_points(
            rows, warn_ncc=0.5, transition_threshold=20,
            anchors=[("measured origin", 290)], limit=12)
        reasons = {reason for row in points for reason in row["reasons"]}
        self.assertIn("start of sampled path", reasons)
        self.assertIn("end of sampled path", reasons)
        self.assertIn("measured origin", reasons)
        self.assertIn("offset search-range edge", reasons)
        self.assertIn("lowest correlation below configured marker", reasons)
        self.assertTrue(any("large offset transition" in reason for reason in reasons))
        self.assertFalse(deferred)

    def test_alignment_review_plan_sequences_descriptor_alignment_and_visual_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            manifest, profile, master, dvd = (
                root / "manifest.json", root / "machine.json", root / "master.mkv", root / "dvd.mkv")
            for path in (manifest, profile, master, dvd):
                path.write_bytes(path.name.encode("ascii"))
            context = {
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "title": "Vortex", "profile": profile, "manifest": manifest,
                "instance": root, "probe": probe,
                "file_paths": {"stack_master": str(master), "dvd": str(dvd)},
                "manifest_data": {
                    "dvd_reference_available": True,
                    "video": {"delta_s": 0.4, "duration_s": 2500},
                },
            }
            plan = build_dvd_alignment_review_plan(context)
            self.assertEqual(
                [step["stage"] for step in plan["steps"]],
                ["master-descriptors", "dvd-descriptors", "measure-dvd-alignment",
                 "alignment-review-assets"])
            alignment_args = next(step["args"] for step in plan["steps"]
                                  if step["stage"] == "measure-dvd-alignment")
            self.assertIn("--required-master-frame", alignment_args)
            self.assertTrue(plan["steps"][-1]["outputs"][0].endswith(".npy") is False)
            saved = root / "alignment-review-plan.json"
            saved.write_text(json.dumps(plan), encoding="utf-8")
            loaded, _raw = load_plan(saved)
            self.assertEqual(
                [step["stage"] for step in loaded["steps"]],
                ["master-descriptors", "dvd-descriptors", "measure-dvd-alignment",
                 "alignment-review-assets"])

    def test_explicit_no_dvd_blocks_alignment_review_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            manifest, profile, master, dvd = (
                root / "manifest.json", root / "machine.json", root / "master.mkv", root / "dvd.mkv")
            for path in (manifest, profile, master, dvd):
                path.write_bytes(b"x")
            context = {
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "profile": profile, "manifest": manifest, "instance": root, "probe": probe,
                "file_paths": {"stack_master": str(master), "dvd": str(dvd)},
                "manifest_data": {"dvd_reference_available": False},
            }
            with self.assertRaisesRegex(WorkflowAppError, "no-DVD"):
                build_dvd_alignment_review_plan(context)

    def test_approval_decision_plan_binds_all_review_assets_and_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe, instance = root / "probe", root / "instance"
            probe.mkdir()
            instance.mkdir()
            manifest, profile = root / "manifest.json", root / "machine.json"
            manifest.write_text('{"episode":"S01E12"}\n', encoding="utf-8")
            profile.write_text("{}\n", encoding="utf-8")
            master, dvd = root / "master.mkv", root / "dvd.mkv"
            master.write_bytes(b"master")
            dvd.write_bytes(b"dvd")
            alignment, report = probe / "alignment.npy", probe / "alignment.tsv"
            master_desc, dvd_desc = probe / "master.npy", probe / "dvd.npy"
            master_meta, dvd_meta = Path(str(master_desc) + ".meta.json"), Path(str(dvd_desc) + ".meta.json")
            contact, playback = probe / "contact.png", probe / "pair.gif"
            files = [alignment, report, master_desc, dvd_desc, master_meta, dvd_meta, contact, playback]
            for path in files:
                path.write_bytes(path.name.encode("ascii"))
            index = probe / "review" / "index.json"
            index.parent.mkdir()
            index.write_text(json.dumps({
                "schema": "dvd-alignment-review-assets-v1",
                "identity": "DS9:S01E12", "candidate_is_approval": False,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "alignment_path": str(alignment), "alignment_sha256": hashlib.sha256(alignment.read_bytes()).hexdigest(),
                "diagnostics_path": str(report), "diagnostics_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "master_source": str(master), "dvd_source": str(dvd),
                "master_descriptors": str(master_desc), "master_descriptors_meta": str(master_meta),
                "dvd_descriptors": str(dvd_desc), "dvd_descriptors_meta": str(dvd_meta),
            }), encoding="utf-8")
            context = {
                "episode": "S01E12", "series": "DS9", "identity": "DS9:S01E12",
                "title": "Vortex", "profile": profile, "manifest": manifest,
                "instance": instance, "probe": probe, "ld": SimpleNamespace(),
                "file_paths": {"stack_master": str(master), "dvd": str(dvd)},
                "manifest_data": {"video": {}},
            }
            with patch("ldcommon.episode_manifest_path", return_value=manifest):
                plan = build_dvd_alignment_review_decision_plan(
                    context, index, "APPROVED", "Verified scene pair matches",
                    expected_index_sha256=hashlib.sha256(index.read_bytes()).hexdigest())
            step = plan["steps"][0]
            self.assertEqual(step["stage"], "record-dvd-alignment-review")
            self.assertIn(str(index.parent), step["inputs"])
            self.assertIn(str(alignment), step["inputs"])
            self.assertIn(str(manifest.resolve()), step["outputs"])

    def test_real_ffmpeg_builds_hashable_paired_contact_and_playback_assets(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master, dvd = root / "master.mkv", root / "dvd.mkv"
            for source, generator in ((master, "testsrc"), (dvd, "testsrc2")):
                proc = subprocess.run([
                    ffmpeg, "-y", "-hide_banner", "-v", "error",
                    "-f", "lavfi", "-i",
                    f"{generator}=size=320x240:rate=30000/1001:duration=45",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
                ], capture_output=True, text=True)
                if proc.returncode:
                    self.skipTest(f"FFmpeg synthetic H.264 source generation unavailable: {proc.stderr[-300:]}")
            master_desc = self._make_descriptors(ffmpeg, master, root, "master")
            dvd_desc = self._make_descriptors(ffmpeg, dvd, root, "dvd")
            frames = [200, 500, 800, 1100]
            alignment = root / "alignment.npy"
            np.save(alignment, np.array([[frame, 0] for frame in frames], dtype=np.int32))
            report = root / "alignment.tsv"
            with report.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, delimiter="\t")
                writer.writerow(("master_frame", "master_time_s", "dvd_minus_master_frames",
                                 "ncc", "local_margin", "search_edge"))
                for index, frame in enumerate(frames):
                    writer.writerow((frame, f"{frame / assets.FPS:.6f}", 0,
                                     0.4 if index == 1 else 0.8, 0.05, int(index == 0)))

            class Episode:
                code, series, identity = "S01E12", "DS9", "DS9:S01E12"
                data = {"files": {}, "video": {"delta_s": 8.0, "duration_s": 20.0}}
                def __init__(self):
                    self.ld = SimpleNamespace()
                def path(self, key):
                    return {"stack_master": master, "dvd": dvd}[key]

            class Tools:
                @staticmethod
                def ffmpeg():
                    return ffmpeg

            episode = Episode()
            episode.ld = Tools()
            # load_desc uses source metadata; the fake ld is only needed for roots and tools.
            output = root / "review"
            with patch.object(assets, "assert_writable_path",
                              side_effect=lambda path, _ld: Path(path)), \
                    patch.object(assets, "episode_manifest_path", return_value=root / "manifest.json"):
                (root / "manifest.json").write_text("{}\n", encoding="utf-8")
                index_path = assets.build_assets(
                    episode, alignment, report, master_desc, dvd_desc, output,
                    0.5, 20, 8, episode.ld)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["candidate_is_approval"], False)
            self.assertIsNotNone(index["contact_sheet"])
            self.assertTrue(Path(index["contact_sheet"]).is_file())
            self.assertTrue(index["review_points"])
            self.assertTrue(all(Path(row["playback_path"]).is_file()
                                for row in index["review_points"]))
            reviewer = DvdAlignmentReviewWindow(
                object(), index_path, lambda *_args: True, "DS9:S01E12")
            self.assertEqual(len(reviewer.points), len(index["review_points"]))

    @staticmethod
    def _make_descriptors(ffmpeg: str, source: Path, root: Path, role: str) -> Path:
        proc = subprocess.run([
            ffmpeg, "-hide_banner", "-v", "error", "-i", str(source),
            "-vf", "scale=48:36:flags=area,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ], capture_output=True)
        if proc.returncode:
            raise AssertionError(proc.stderr.decode("utf-8", "replace"))
        data = np.frombuffer(proc.stdout, dtype=np.uint8)
        if len(data) % 1728:
            raise AssertionError("FFmpeg returned a partial descriptor frame")
        descriptors = data.reshape(-1, 1728)
        path = root / f"{role}.npy"
        np.save(path, descriptors)
        stat = source.stat()
        path.with_suffix(".npy.meta.json").write_text(json.dumps({
            "episode": "S01E12", "role": role, "descriptor_size": [48, 36],
            "output_grid_fps": "30000/1001", "input_path": str(source.resolve()),
            "input_size": stat.st_size, "input_mtime_ns": stat.st_mtime_ns,
        }), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
