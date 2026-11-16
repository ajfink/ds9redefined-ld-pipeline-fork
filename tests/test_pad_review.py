from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pad_review import (PadReviewError, PreviewCache, _extract_outer_frames,
                        discover_review_items)  # noqa: E402


class PadReviewTests(unittest.TestCase):
    def test_discovery_lists_only_generated_rife_rows_for_episode_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            work = probe / "rife-padframes-local" / "S99E99"
            neighbors, interpolated = work / "neighbors", work / "interpolated"
            neighbors.mkdir(parents=True)
            interpolated.mkdir()
            replacement = interpolated / "pad_000100.png"
            replacement.write_bytes(b"fixture")
            (neighbors / "prev_000100.png").write_bytes(b"prev")
            (neighbors / "next_000100.png").write_bytes(b"next")
            video = root / "source.avs"
            video.write_text("fixture", encoding="utf-8")
            profile = root / "profile.json"
            profile.write_text(json.dumps({"roots": {"probe": str(probe)}, "tools": {}}), encoding="utf-8")
            manifest = root / "S01E07.json"
            manifest.write_text(json.dumps({
                "episode": "S01E07", "pad_report_label": "S01E07",
                "files": {"avs_encode": {"path": str(video)}},
            }), encoding="utf-8")
            report = probe / "rife-pad-replacements-local.tsv"
            with report.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("episode", "frame", "method", "png"), delimiter="\t")
                writer.writeheader()
                writer.writerow({"episode": "S99E99", "frame": 100, "method": "RIFE-v4-CPU-TTA",
                                 "png": str(replacement.relative_to(probe))})
                writer.writerow({"episode": "S01E08", "frame": 101, "method": "RIFE-v4-CPU-TTA",
                                 "png": str(replacement.relative_to(probe))})
                writer.writerow({"episode": "S99E99", "frame": 102, "method": "hold-prev-scene-cut",
                                 "png": str(replacement.relative_to(probe))})

            manifest.write_text(json.dumps({
                "episode": "S99E99", "pad_report_label": "S99E99",
                "files": {"avs_encode": {"path": str(video)}},
            }), encoding="utf-8")
            items, found_probe, found_report = discover_review_items("S99E99", manifest, profile)
            self.assertEqual(found_probe, probe.resolve())
            self.assertEqual(found_report, report)
            self.assertEqual([(item.kind, item.frame) for item in items], [("rife", 100)])
            self.assertEqual(items[0].before.name, "prev_000100.png")
            self.assertEqual(items[0].after.name, "next_000100.png")

    def test_rife_image_path_outside_probe_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            outside = root / "outside.png"
            outside.write_bytes(b"external")
            video = root / "source.avs"
            video.write_text("fixture", encoding="utf-8")
            profile = root / "profile.json"
            profile.write_text(json.dumps({"roots": {"probe": str(probe)}, "tools": {}}), encoding="utf-8")
            manifest = root / "S01E07.json"
            manifest.write_text(json.dumps({"episode": "S01E07", "files": {"avs_encode": {"path": str(video)}}}),
                                encoding="utf-8")
            (probe / "rife-pad-replacements-local.tsv").write_text(
                "episode\tframe\tmethod\tpng\nS99E99\t100\tRIFE-v4\t" + str(outside) + "\n",
                encoding="utf-8")
            manifest.write_text(json.dumps({
                "episode": "S99E99", "files": {"avs_encode": {"path": str(video)}}}), encoding="utf-8")
            with self.assertRaisesRegex(PadReviewError, "no cut-PAD stills"):
                discover_review_items("S99E99", manifest, profile)

    def test_preview_cache_cleanup_is_scoped_to_its_owned_temp_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "canonical.png"
            source.write_bytes(b"canonical")
            cache = PreviewCache()
            preview = cache.file("preview.png")
            preview.write_bytes(b"temporary")
            cache_root = cache.path
            self.assertTrue(cache.cleanup())
            self.assertFalse(cache_root.exists())
            self.assertTrue(source.exists())
            self.assertFalse(cache.cleanup())

    def test_rife_outer_context_uses_frames_two_before_and_two_after(self):
        class FakeLd:
            profile_path = Path("profile.json")

            @staticmethod
            def ffmpeg():
                return "ffmpeg"

        with tempfile.TemporaryDirectory() as temporary:
            cache = PreviewCache()
            video = Path(temporary) / "source.vpy"
            video.write_text("fixture", encoding="utf-8")

            def fake_run(_ld, command, _video, *, text, vs_args):
                self.assertFalse(text)
                self.assertEqual(vs_args, ["-s", "98", "-e", "102"])
                self.assertIn("select='eq(n,0)+eq(n,4)',format=rgb24", command)
                pattern = command[-1]
                Path(pattern.replace("%02d", "01")).write_bytes(b"minus two")
                Path(pattern.replace("%02d", "02")).write_bytes(b"plus two")
                return SimpleNamespace(returncode=0, stderr=b"")

            with patch("pad_review.run_source_ffmpeg", side_effect=fake_run):
                before2, after2 = _extract_outer_frames(FakeLd(), video, 100, cache,
                                                        "S01E07", None)
            self.assertEqual(before2.read_bytes(), b"minus two")
            self.assertEqual(after2.read_bytes(), b"plus two")
            cache.cleanup()

    def test_avisynth_outer_context_uses_exact_trimmed_source_indices(self):
        class FakeLd:
            profile_path = Path("profile.json")

            @staticmethod
            def ffmpeg():
                return "ffmpeg"

        with tempfile.TemporaryDirectory() as temporary:
            cache = PreviewCache()
            video = Path(temporary) / "source.avs"
            video.write_text("fixture", encoding="utf-8")

            def fake_run(_ld, command, input_video, *, text, vs_args):
                self.assertFalse(text)
                self.assertIsNone(vs_args)
                self.assertEqual(command[command.index("-f") + 1], "avisynth")
                self.assertNotIn("-ss", command)
                wrapper = Path(command[command.index("-i") + 1])
                self.assertEqual(input_video, wrapper)
                self.assertIn("src.Trim(98, 102)", wrapper.read_text(encoding="utf-8"))
                pattern = command[-1]
                Path(pattern.replace("%02d", "01")).write_bytes(b"minus two")
                Path(pattern.replace("%02d", "02")).write_bytes(b"plus two")
                return SimpleNamespace(returncode=0, stderr=b"")

            with patch("pad_review.run_source_ffmpeg", side_effect=fake_run):
                before2, after2 = _extract_outer_frames(FakeLd(), video, 100, cache,
                                                        "S01E07", None)
            self.assertEqual(before2.read_bytes(), b"minus two")
            self.assertEqual(after2.read_bytes(), b"plus two")
            cache.cleanup()


if __name__ == "__main__":
    unittest.main()
