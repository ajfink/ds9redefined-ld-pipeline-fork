from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
from scripts import extract_candidate_frames as extract  # noqa: E402


class CandidateFrameExtractionTests(unittest.TestCase):
    def test_frame_list_is_explicit_unique_in_range_and_bounded(self):
        self.assertEqual(extract.parse_frame_numbers([9, 2], None, 20), [2, 9])
        with self.assertRaisesRegex(PipelineError, "duplicate"):
            extract.parse_frame_numbers([2, 2], None, 20)
        with self.assertRaisesRegex(PipelineError, "within"):
            extract.parse_frame_numbers([20], None, 20)
        with self.assertRaisesRegex(PipelineError, "maximum per run"):
            extract.parse_frame_numbers(list(range(4)), None, 20, max_frames=3)

    def test_select_expressions_are_exact_and_chunked_at_32_terms(self):
        frames = list(range(0, 1000, 7))
        chunks = extract.frame_chunks(frames)
        self.assertTrue(all(1 <= len(chunk) <= 32 for chunk in chunks))
        self.assertEqual([n for chunk in chunks for n in chunk], frames)
        expr = extract.selection_filter([3, 900])
        self.assertIn("eq(n,3)+eq(n,900)", expr)
        self.assertNotIn("trim", expr.casefold())
        with self.assertRaisesRegex(PipelineError, "1..32"):
            extract.selection_filter(list(range(33)))

    def test_extract_writes_only_requested_frames_with_hash_manifest(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.avs"
            video.write_text("source stub", encoding="ascii")
            output = root / "probe" / "candidate-frames"
            output.parent.mkdir()
            ld = SimpleNamespace(profile={"shared": {"enabled": False},
                                         "limits": {"min_free_gb": 0}},
                                 profile_path=root / "machine.json",
                                 ffmpeg=lambda: "ffmpeg")
            commands = []

            def fake_run(_ld, command, _video, *, text=True, vs_args=None):
                commands.append(command)
                pattern = Path(command[-1])
                for number in range(2):
                    Image.new("RGB", (extract.WIDTH, extract.HEIGHT), (number, 0, 0)).save(
                        pattern.parent / f"selected_{number:08d}.png")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(extract, "run_source_ffmpeg", side_effect=fake_run):
                extract.extract_frames(ld, video, [12, 987], output)

            self.assertEqual(sorted(p.name for p in output.glob("frame_*.png")),
                             ["frame_00000012.png", "frame_00000987.png"])
            with (output / "frames.tsv").open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual([int(r["output_frame"]) for r in rows], [12, 987])
            self.assertTrue(all(len(r["sha256"]) == 64 for r in rows))
            self.assertIn("-frames:v", commands[0])
            self.assertEqual(commands[0][commands[0].index("-frames:v") + 1], "2")
            with self.assertRaisesRegex(PipelineError, "refusing to overwrite"):
                extract.extract_frames(ld, video, [12, 987], output)


if __name__ == "__main__":
    unittest.main()
