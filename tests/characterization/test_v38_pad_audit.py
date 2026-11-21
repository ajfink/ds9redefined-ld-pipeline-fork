"""Characterization: v38 tests/test_pad_audit_boundary.py (4 tests), ported by tools/port_v38.py."""
from __future__ import annotations

import unittest
from pathlib import Path

from fstk.core.errors import ArtifactError as PipelineError
from fstk.media.pads import boundary_orphan_frames

ROOT = Path(__file__).resolve().parents[1]


class PadAuditBoundaryTests(unittest.TestCase):
    def test_boundary_orphan_is_excluded_from_neighbor_scoring(self):
        self.assertEqual(boundary_orphan_frames([12, 99], {12}, 100), {99})

    def test_boundary_predicted_pad_remains_fail_closed(self):
        with self.assertRaisesRegex(PipelineError, "predicted PAD.*clip boundary"):
            boundary_orphan_frames([12, 99], {12, 99}, 100)

    def test_first_and_last_frame_orphans_are_both_tolerated(self):
        self.assertEqual(boundary_orphan_frames([0, 40, 99], {40}, 100), {0, 99})

    def test_empty_clip_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "no frames"):
            boundary_orphan_frames([0], set(), 0)
