"""Characterization: v38 tests/test_frame_selection_chunks.py (select= chunking), ported verbatim."""
from __future__ import annotations

import re
import unittest

from fstk.core.errors import ToolError as PipelineError
from fstk.tools.ffmpeg import SELECT_TERMS_PER_CALL, frame_selection_chunks

TERM = re.compile(r"^(?:eq\(n,(?P<eq>\d+)\)|"
                  r"between\(n,(?P<start>\d+),(?P<end>\d+)\)"
                  r"(?:\*not\(mod\(n-(?P<base>\d+),(?P<step>\d+)\)\))?)$")


def evaluate(expression: str, frames: range) -> list[int]:
    """Evaluate the select term grammar exactly as FFmpeg would for ``n``."""
    selected = []
    for frame in frames:
        total = 0
        for term in expression.split("+"):
            match = TERM.match(term)
            if not match:
                raise AssertionError(f"unexpected select term: {term}")
            if match.group("eq"):
                total += int(frame == int(match.group("eq")))
                continue
            start, end = int(match.group("start")), int(match.group("end"))
            if not start <= frame <= end:
                continue
            step = match.group("step")
            if step is None or (frame - int(match.group("base"))) % int(step) == 0:
                total += 1
        selected.append(frame) if total else None
    return selected


class FrameSelectionChunkTests(unittest.TestCase):
    def test_constant_step_runs_collapse_into_one_term(self):
        frames = [3300 + offset for offset in range(-36, 37, 3)]
        chunks = frame_selection_chunks(frames)
        self.assertEqual(len(chunks), 1)
        chunk_frames, expression = chunks[0]
        self.assertEqual(chunk_frames, frames)
        self.assertEqual(expression, "between(n,3264,3336)*not(mod(n-3264,3))")

    def test_consecutive_and_isolated_frames_use_compact_terms(self):
        frames = [10, 11, 12, 13, 40, 100, 103, 106]
        chunks = frame_selection_chunks(frames)
        self.assertEqual(len(chunks), 1)
        chunk_frames, expression = chunks[0]
        self.assertEqual(chunk_frames, frames)
        self.assertEqual(expression,
                         "between(n,10,13)+between(n,40,100)*not(mod(n-40,60))"
                         "+between(n,103,106)*not(mod(n-103,3))")
        self.assertEqual(evaluate(expression, range(0, 120)), frames)

    def test_expressions_select_exactly_the_requested_frames(self):
        frames = sorted({200, 201, 202} | {300 + offset for offset in range(0, 61, 3)}
                        | {900, 950, 1000, 1003})
        for chunk_frames, expression in frame_selection_chunks(frames):
            self.assertEqual(evaluate(expression, range(0, 1200)), chunk_frames)

    def test_long_lists_split_below_the_ffmpeg_expression_limit(self):
        # FFmpeg fails to initialise select='eq(...)+...' past 100 chained terms.
        frames = list(range(100, 100 + 130))
        chunks = frame_selection_chunks(frames)
        self.assertEqual([frame for chunk, _ in chunks for frame in chunk], frames)
        for _, expression in chunks:
            self.assertLessEqual(len(expression.split("+")), SELECT_TERMS_PER_CALL)
        self.assertEqual(evaluate("+".join(expression for _, expression in chunks),
                                  range(100, 230)), frames)

    def test_scattered_frames_are_split_into_bounded_passes(self):
        even = list(range(0, 390, 3))  # 130 frames, single constant-step run
        single = frame_selection_chunks(even)
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0][0], even)

        scattered = [index * 3 + index % 2 for index in range(130)]
        chunks = frame_selection_chunks(scattered)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([frame for chunk, _ in chunks for frame in chunk], scattered)
        for chunk, expression in chunks:
            self.assertLessEqual(len(expression.split("+")), SELECT_TERMS_PER_CALL)
            self.assertEqual(evaluate(expression, range(0, 400)), chunk)

    def test_duplicate_or_non_integer_indices_are_rejected(self):
        with self.assertRaises(PipelineError):
            frame_selection_chunks([1, 1, 2])
        with self.assertRaises(PipelineError):
            frame_selection_chunks([1, 2.5])
        with self.assertRaises(PipelineError):
            frame_selection_chunks([-1, 2])
        with self.assertRaises(PipelineError):
            frame_selection_chunks([1, 2], limit=0)
        with self.assertRaises(PipelineError):
            frame_selection_chunks([1, 2], limit=SELECT_TERMS_PER_CALL + 1)
