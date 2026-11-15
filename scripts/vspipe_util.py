#!/usr/bin/env python3
"""Shared macOS plumbing for feeding VapourSynth scripts to FFmpeg.

These helpers used to live in ``encode_padfixed_mac.py``. That script ports
``encode_padfixed.ps1``, which **Workflow Version 4 retired** (it now throws),
so the helpers were lifted out here rather than have the live encode path and
the PAD audit import a retired module.

What lives here
---------------
* ``X265_PARAMS`` - the x265 string carried over verbatim from the Windows
  scripts (CRF 14 / slow / tune grain / Main 10 / SMPTE 170M / limited range).
* ``vspipe_bin()`` / ``vspipe_env()`` - locate vspipe and set
  ``DYLD_LIBRARY_PATH``. The bestsource and mvtools plugins link against
  conda's libav* / fftw3f via ``@rpath``; in-process Python hides this but
  ``vspipe`` fails without it (PORTING.md).
* ``pipe()`` - run ``vspipe -> ffmpeg`` without deadlocking.
* ``probe_sar()`` - read a stream's sample aspect ratio.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ldcommon import Ld, PipelineError

X265_PARAMS = ("colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:"
               "range=limited:log-level=error:info=0")


def probe_sar(ffprobe: Path, src: Path) -> str | None:
    p = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=sample_aspect_ratio",
                        "-of", "default=nk=1:nw=1", str(src)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode != 0:
        raise PipelineError(f"ffprobe failed reading SAR from {src}: {p.stderr[-400:]}")
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line:
            return line
    return None


def vspipe_bin() -> str:
    """Resolve the same vspipe executable configured for the machine's Python env."""
    ld = Ld()
    configured = ((ld.profile.get("tools") or {}).get("vspipe") or "").strip()
    if configured:
        candidate = Path(os.path.expandvars(os.path.expanduser(configured)))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise PipelineError(f"tools.vspipe points at a missing/non-executable path: {candidate}")
    found = shutil.which("vspipe")
    if found:
        return found
    python_value = ((ld.profile.get("python") or {}).get("interpreter") or sys.executable)
    python = Path(os.path.expandvars(os.path.expanduser(str(python_value))))
    cand = python.parent / "vspipe"
    if cand.is_file():
        return str(cand)
    raise PipelineError("vspipe not found - configure tools.vspipe in machine.json or place it "
                        "beside the profile-selected Python (see PORTING.md)")


def vspipe_env() -> dict:
    """Plugins here link against conda's libav* / fftw3f via @rpath; in-process
    Python hides this but vspipe fails without it (PORTING.md)."""
    env = os.environ.copy()
    prefix = Path(vspipe_bin()).parent.parent
    if (prefix / "lib").is_dir():
        lib = str(prefix / "lib")
        prev = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = lib + (os.pathsep + prev if prev else "")
    return env


def pipe(vs_cmd: list[str], ff_cmd: list[str], log: Path | None, env: dict,
         *, require_vspipe_ok: bool = True) -> int:
    """Run vspipe -> ffmpeg.

    Both processes append to the same O_APPEND log handle. Sending vspipe's
    stderr there (instead of an unread pipe) avoids a deadlock: vspipe can emit
    more than a 64 KB pipe buffer of warnings, and a blocked vspipe stops
    writing stdout, which would hang FFmpeg. On failure the log tail is reported.

    ``_tail()`` reads only the bytes **this** run appended. The log path is
    stable per episode/pass, so reading from byte 0 would report a previous
    run's output as if it were this one's - which is exactly how a stale
    10-minute V2 encode log came to be quoted as the cause of a later failure.

    ``require_vspipe_ok=False`` is for short decodes (``-frames:v N``): FFmpeg
    exits once it has its frames and closes the pipe, so vspipe is cut short and
    exits non-zero through no fault of its own. FFmpeg's exit code is then the
    only meaningful signal. A full encode keeps the strict check, because
    FFmpeg can exit 0 after encoding *fewer* frames than required if vspipe
    died partway.
    """
    logf = open(log, "ab", buffering=0) if log else None
    start = logf.tell() if logf is not None else 0
    dest = logf if logf is not None else subprocess.DEVNULL
    try:
        vsp = subprocess.Popen(vs_cmd, stdout=subprocess.PIPE, stderr=dest, env=env)
        assert vsp.stdout is not None
        ff = subprocess.Popen(ff_cmd, stdin=vsp.stdout, stdout=subprocess.DEVNULL,
                              stderr=dest)
        vsp.stdout.close()          # parent must drop it or FFmpeg never sees EOF
        ff_err = ff.wait()
        vsp_err = vsp.wait()

        def _tail() -> str:
            if log is None or not log.is_file():
                return ""
            with open(log, "rb") as f:
                f.seek(start)
                data = f.read()
            return data[-1500:].decode("utf-8", "replace")

        bad = ff_err != 0 or (require_vspipe_ok and vsp_err != 0)
        if bad:
            codes = ", ".join(n for n, c in (("vspipe", vsp_err), ("ffmpeg", ff_err))
                              if c != 0)
            raise PipelineError(f"{codes} failed - see {log}: {_tail()}")
        return 0
    finally:
        if logf is not None:
            logf.close()


# Historical name kept so existing call sites and the retired port still work.
_pipe = pipe


class _Result:
    """The subset of CompletedProcess that the source-reading call sites use."""

    def __init__(self, returncode: int, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_source_ffmpeg(ld, ff_cmd: list[str], video: Path, *, text: bool = True,
                      vs_args: list[str] | None = None) -> _Result:
    """Run ``ff_cmd`` over the source-derived chain, on either platform.

    Windows reads the chain with ``-f avisynth -i <avs>``. This FFmpeg build has
    no AviSynth demuxer on macOS, so a ``.vpy`` chain is piped in with
    ``vspipe -c y4m`` and FFmpeg reads ``-f yuv4mpegpipe -i -``. Everything
    downstream of the input specifier is byte-identical, which is what keeps
    blackdetect logs and descriptor bytes comparable across machines.

    ``vs_args`` are extra vspipe options inserted before the script path
    (``-s``/``-e`` for frame-accurate ranges, which replace the ``-ss`` input
    seek Windows uses - a pipe cannot be seeked, and the frame index is exact
    where a time-based seek is not).

    vspipe's stderr goes to a temp file, not a pipe. It is only read *after*
    FFmpeg exits, and an unread 64 KB pipe buffer would block vspipe, which
    would stop it writing stdout and hang the whole pipeline. The text is
    appended to FFmpeg's stderr only when FFmpeg itself reported nothing
    useful, so it never pollutes a healthy log (the ``frame=`` totals and
    ``black_start:`` lines are parsed out of it verbatim).
    """
    if video.suffix.lower() != ".vpy":
        p = subprocess.run(ff_cmd, capture_output=True, text=text,
                           encoding="utf-8" if text else None,
                           errors="replace" if text else None)
        return _Result(p.returncode, p.stdout, p.stderr or "")

    vs_cmd = [vspipe_bin(), *(vs_args or []), "-c", "y4m", str(video), "-"]
    with tempfile.NamedTemporaryFile(prefix="vspipe-", suffix=".log",
                                     delete=False) as fh:
        err_path = Path(fh.name)
    try:
        with err_path.open("wb") as ef:
            vs = subprocess.Popen(vs_cmd, stdout=subprocess.PIPE,
                                  stderr=ef, env=vspipe_env())
            assert vs.stdout is not None
            p = subprocess.run(ff_cmd, stdin=vs.stdout, capture_output=True,
                               text=text, encoding="utf-8" if text else None,
                               errors="replace" if text else None)
            vs.stdout.close()      # parent must drop it or FFmpeg never sees EOF
            vs.wait()
        vspipe_err = err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        err_path.unlink(missing_ok=True)
    if not text:
        # raw extraction: keep stderr bytes, only add vspipe's text when
        # FFmpeg reported nothing, so the failure is still diagnosable.
        if p.stderr:
            return _Result(p.returncode, p.stdout, p.stderr)
        return _Result(p.returncode, p.stdout, vspipe_err.encode("utf-8", "replace"))
    err = p.stderr or ""
    if vspipe_err.strip() and not re.search(r"black_start:|error|invalid|failed",
                                            err, re.I):
        err += "\n" + vspipe_err
    return _Result(p.returncode, p.stdout, err)
