"""Frame extraction on the same grid as the alignment descriptors (Mac 2026-11-23, S02E03)."""
from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

from fstk.tools.ffmpeg import FFmpeg

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.fixture
def gapped(tmp_path):
    """A 29.97 stream with every 5th frame missing (like a DVD whose repeat flags leave holes in the grid)."""
    out = tmp_path / "gapped.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=30000/1001:d=4",
                    "-vf", "select='not(eq(mod(n\\,5)\\,2))'", "-fps_mode", "passthrough", "-c:v", "ffv1",
                    str(out)], check=True)
    return out


def test_descriptor_grid_frames_are_the_descriptor_frames(gapped, tmp_path):
    ff = FFmpeg("ffmpeg", "ffprobe", log_dir=tmp_path)
    thumbs = ff.gray_thumbnails(gapped, 48, 36)
    decoded = ff.count_frames(gapped)
    assert len(thumbs) > decoded                                       # the grid repeats frames to fill holes
    want = [0, 7, 52, len(thumbs) - 1, len(thumbs) + 5]
    got = ff.frames_by_index(gapped, want, 48, 36)
    assert sorted(got) == want[:-1]                                    # past the end: skipped, not an error
    for i in want[:-1]:
        grey = (got[i].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)).ravel()
        assert np.abs(grey - thumbs[i].astype(np.float32)).mean() < 3.0, i


def test_decoded_grid_keeps_frames_that_share_a_timestamp(gapped, tmp_path):
    ff = FFmpeg("ffmpeg", "ffprobe", log_dir=tmp_path)
    decoded = ff.count_frames(gapped)
    want = list(range(0, decoded, 9))
    got = ff.frames_by_index(gapped, want, 64, 48, grid="decoded")
    assert sorted(got) == want
