"""fstk.tools.vapoursynth: the frame reader (vspipe y4m)."""
from __future__ import annotations

import os
import stat
import sys

import pytest

from fstk.core.errors import ToolError
from fstk.tools.vapoursynth import VapourSynth

W, H = 4, 2   # 4:2:2 8-bit: 4*2 + 2*(2*2) = 16 bytes a frame


def _fake_vspipe(tmp_path, body: str):
    """A stand-in vspipe: like the real one on Windows (no SYSTEMROOT, Python cannot start), it writes nothing
    unless it gets the parent's environment."""
    py = tmp_path / "fake_vspipe.py"
    py.write_text("import os, sys\n" + body)
    exe = tmp_path / "vspipe"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {py} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


@pytest.mark.skipif(os.name == "nt", reason="uses a shell-script stand-in for vspipe")
def test_the_frame_reader_gives_vspipe_the_whole_environment(tmp_path, monkeypatch):
    """PC-A 2026-11-24 14:51: vspipe got an empty environment and video-quality saw no y4m header at all."""
    monkeypatch.setenv("FSTK_TEST_PARENT_ENV", "yes")
    exe = _fake_vspipe(tmp_path, f"""
if os.environ.get("FSTK_TEST_PARENT_ENV") != "yes":
    sys.stderr.write("Fatal Python error: no environment\\n"); sys.exit(255)
out = sys.stdout.buffer
out.write(b"YUV4MPEG2 W{W} H{H} F30000:1001 Ip A0:0 C422\\n")
for n in range(3):
    out.write(b"FRAME\\n" + bytes([n]) * 16)
""")
    frames = list(VapourSynth(exe, "ffmpeg").iter_planes(tmp_path / "s.vpy"))
    assert len(frames) == 3


@pytest.mark.skipif(os.name == "nt", reason="uses a shell-script stand-in for vspipe")
def test_a_missing_header_reports_vspipes_own_message_and_exit_code(tmp_path):
    exe = _fake_vspipe(tmp_path, 'sys.stderr.write("Script evaluation failed: boom\\n"); sys.exit(3)\n')
    with pytest.raises(ToolError, match=r"vspipe exit 3: Script evaluation failed: boom"):
        list(VapourSynth(exe, "ffmpeg").iter_planes(tmp_path / "s.vpy"))
