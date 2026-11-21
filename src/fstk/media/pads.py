"""PAD (inserted placeholder frame) audit math.

Ported verbatim from v38 ``scripts/pad_audit.py``. The detection/descriptor I/O parts
(blackdetect over the source-derived clip, descriptor cache) become a stage.
"""
from __future__ import annotations

from fstk.core.errors import ArtifactError as PipelineError

W, H = 48, 36
F24 = 24000.0 / 1001.0
BLIP_MAX_S = 0.050
TOLERANCE_FRAMES = 2


def boundary_orphan_frames(candidate_frames: list[int], matched_candidates: set[int],
                           frame_count: int) -> set[int]:
    """Exclude boundary orphans, while failing closed for boundary repair candidates."""
    if frame_count < 1:
        raise PipelineError("source-derived clip has no frames")
    boundaries = {frame for frame in candidate_frames if frame in (0, frame_count - 1)}
    unsafe = boundaries & matched_candidates
    if unsafe:
        raise PipelineError(
            "a predicted PAD is at a clip boundary; neighbor-based audit is undefined: "
            f"{sorted(unsafe)}")
    return boundaries
