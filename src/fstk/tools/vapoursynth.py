"""VapourSynth adapter (``vspipe``) — the frame server on every platform (REFACTOR-PLAN §7.3).

Carried over from v38 ``vspipe_util.py`` / ``generate_cadence_evidence.py``:
* vspipe stderr is always drained (it can emit > 64 KB of warnings and deadlock otherwise) —
  ``fstk.tools.process.run_pipeline`` reads every stage's stderr on its own thread;
* on macOS conda builds, plugins need ``DYLD_LIBRARY_PATH=<prefix>/lib`` for vspipe;
* a full decode is judged by FFmpeg's ``-progress`` frame count *and* both exit codes; a short
  decode (``-frames:v N``) may legitimately end vspipe with a broken pipe.
"""
from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fstk.core.errors import ToolError
from fstk.tools import process


@dataclass
class FullDecode:
    frames: int | None
    vspipe_rc: int | None
    ffmpeg_rc: int | None
    errors: list[str]

    @property
    def clean(self) -> bool:
        return self.vspipe_rc == 0 and self.ffmpeg_rc == 0 and self.frames is not None


class VapourSynth:
    def __init__(self, vspipe: str | Path, ffmpeg: str | Path, *, log_dir: Path | None = None):
        self.vspipe = str(vspipe)
        self.ffmpeg = str(ffmpeg)
        self.log_dir = log_dir

    @classmethod
    def from_profile(cls, profile, *, log_dir: Path | None = None) -> VapourSynth:
        return cls(profile.tool("vspipe"), profile.tool("ffmpeg"), log_dir=log_dir)

    def env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if platform.system() == "Darwin":
            lib = Path(self.vspipe).parent.parent / "lib"
            if lib.is_dir():
                prev = os.environ.get("DYLD_LIBRARY_PATH", "")
                env["DYLD_LIBRARY_PATH"] = str(lib) + (os.pathsep + prev if prev else "")
        return env

    def _log(self, name: str) -> Path | None:
        return self.log_dir / f"{name}.log" if self.log_dir else None

    def full_decode(self, script: Path, *, name: str = "vspipe-full-decode", cancel=None,
                    progress=None) -> FullDecode:
        """Evaluate every frame of ``script`` (vspipe → FFmpeg null) and count them."""
        frames: list[int] = []
        errors: list[str] = []

        def on_line(stream: str, line: str) -> None:
            if progress:
                progress(stream, line)
            m = re.fullmatch(r"frame=(\d+)\s*", line)
            if stream == "stdout" and m:
                frames.append(int(m.group(1)))
            elif stream.startswith("ffmpeg") and line.strip():
                errors.append(line.strip())

        vs_cmd = [self.vspipe, "-p", "-c", "y4m", str(script), "-"]         # -p: "Frame: n/total" on stderr
        ff_cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
                  "-stats_period", "5", "-f", "yuv4mpegpipe", "-i", "-", "-map", "0:v:0", "-an",
                  "-f", "null", "-"]
        res = process.run_pipeline([vs_cmd, ff_cmd], env=self.env(), log_path=self._log(name),
                                   on_line=on_line, cancel=cancel)
        rcs = [s.returncode for s in res.stages] or [res.returncode, res.returncode]
        return FullDecode(frames[-1] if frames else None, rcs[0], rcs[-1], errors)

    def info(self, script: Path) -> dict:
        """``vspipe --info`` → frames, fps, duration, raster, format."""
        res = process.run([self.vspipe, "--info", str(script)], capture_stdout=True, env=self.env(),
                          log_path=self._log("vspipe-info"))
        text = (res.stdout or b"").decode("utf-8", errors="replace") + "\n" + "\n".join(res.tail)
        if res.returncode != 0:
            raise ToolError(f"vspipe could not inspect {script}: {res.describe()}")
        return parse_info(text)

    def encode(self, script: Path, ffmpeg_output_args: list[str], *, name: str = "encode",
               cancel=None, on_line=None) -> process.ProcessResult:
        """vspipe y4m → FFmpeg with the given output arguments (both stages monitored)."""
        vs_cmd = [self.vspipe, "-p", "-c", "y4m", str(script), "-"]
        ff_cmd = [self.ffmpeg, "-y", "-hide_banner", "-nostdin", "-stats_period", "30",
                  "-f", "yuv4mpegpipe", "-i", "-", *ffmpeg_output_args]
        return process.run_pipeline([vs_cmd, ff_cmd], env=self.env(), log_path=self._log(name),
                                    cancel=cancel, on_line=on_line)


    # ----------------------------------------------------------------- frame access
    def serve(self, script: Path, first: int | None = None, last: int | None = None) -> list[str]:
        """First pipeline stage: vspipe writing y4m frames ``first``..``last`` (inclusive) of ``script``."""
        cmd = [self.vspipe, "-c", "y4m"]
        if first is not None:
            cmd += ["-s", str(int(first))]
        if last is not None:
            cmd += ["-e", str(int(last))]
        return [*cmd, str(script), "-"]

    def iter_planes(self, script: Path, first: int | None = None, last: int | None = None, *, cancel=None):
        """Yield every frame of ``script`` (inclusive range) as ``fstk.quality.frames.Planes``, in the chain's
        own pixel format (no scaling, no conversion). Streams from vspipe's y4m output one frame at a time,
        so a whole episode is read without holding it in memory."""
        import subprocess
        import tempfile

        from fstk.quality.frames import frame_bytes, split_frames

        formats = {"420p10": "yuv420p10le", "422p10": "yuv422p10le", "420": "yuv420p", "422": "yuv422p"}
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(self.serve(script, first, last), stdout=subprocess.PIPE, stderr=err, env=self.env())
            try:
                head = (proc.stdout.readline() or b"").decode("ascii", "replace")
                tags = {t[0]: t[1:] for t in head.split()[1:] if t}
                if not head.startswith("YUV4MPEG2") or "W" not in tags or "H" not in tags or tags.get("C") not in formats:
                    raise ToolError(f"unsupported or missing y4m header from {script}: {head.strip()!r}")
                width, height, pix = int(tags["W"]), int(tags["H"]), formats[tags["C"]]
                per = frame_bytes(width, height, pix)
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if not line.startswith(b"FRAME"):
                        raise ToolError(f"y4m stream out of sync reading {script}")
                    buf = proc.stdout.read(per)
                    while len(buf) < per:
                        more = proc.stdout.read(per - len(buf))
                        if not more:
                            raise ToolError(f"truncated frame from {script}")
                        buf += more
                    yield split_frames(buf, width, height, pix)[0]
                    if cancel is not None and cancel.is_set():
                        return
                if proc.wait() != 0:
                    err.seek(0)
                    raise ToolError(f"vspipe failed reading {script}: {err.read()[-400:].decode('utf-8', 'replace')}")
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

    def scan(self, script: Path, *, width: int = 48, height: int = 36, name: str = "pad-scan",
             cancel=None, blackdetect: str = "blackdetect=d=0:pix_th=0.10:pic_th=0.98") -> ScriptScan:
        """One sequential pass of ``script``: blackdetect events and grey ``width``×``height`` descriptors.

        v38 ran FFmpeg's blackdetect and the descriptor extraction as two separate decodes of the
        same (slow) IVTC chain; both filters read the identical frames here, so one pass serves both.
        """
        import numpy as np

        events: list[tuple[float, float, float]] = []
        errors: list[str] = []
        pat = re.compile(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)")

        def on_line(stream: str, line: str) -> None:
            m = pat.search(line)
            if m:
                events.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
            elif "[error]" in line or "[fatal]" in line:
                errors.append(line.strip())

        graph = (f"[0:v]split=2[a][b];[a]{blackdetect}[ao];"
                 f"[b]scale={width}:{height}:flags=area,format=gray[bo]")
        ff = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "level+info", "-f", "yuv4mpegpipe", "-i", "-",
              "-filter_complex", graph, "-map", "[ao]", "-an", "-f", "null", "-",
              "-map", "[bo]", "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        res = process.run_pipeline([self.serve(script), ff], env=self.env(), log_path=self._log(name),
                                   capture_stdout=True, on_line=on_line, cancel=cancel)
        data = res.stdout or b""
        size = width * height
        if res.returncode != 0 or not data or len(data) % size:
            raise ToolError(f"scan of {script} failed or was truncated: {res.describe()}")
        return ScriptScan(np.frombuffer(data, np.uint8).reshape(-1, size).copy(), events, errors)

    def frames(self, script: Path, first: int, last: int, *, width: int, height: int, pix_fmt: str = "rgb48le",
               name: str = "frames", cancel=None):
        """Frames ``first``..``last`` of ``script``, area-scaled to ``width``×``height`` (the native
        size for a lossless read), as a numpy array.

        ``rgb48le`` → (n, height, width, 3) ``uint16`` and ``rgb24`` → the same shape in ``uint8``,
        both through an explicit BT.601 limited → full conversion (the matrix the stack master uses
        and the payload clip converts back with); ``gray`` → (n, height, width) ``uint8``. The 16-bit
        form is the default because the chain is 10-bit and replacement frames are written back into it
        (audit 1, item A2). ``vspipe`` is asked for the exact range, so indices are the script's own.
        """
        import numpy as np

        first, last = int(first), int(last)
        n = last - first + 1
        if n < 1:
            raise ToolError("empty frame range")
        if pix_fmt in ("rgb24", "rgb48le"):
            vf = (f"scale={width}:{height}:in_range=tv:in_color_matrix=bt601:out_range=pc:"
                  f"flags=area+accurate_rnd+full_chroma_int,format={pix_fmt}")
        elif pix_fmt == "gray":
            vf = f"scale={width}:{height}:flags=area,format=gray"
        else:
            raise ToolError(f"unsupported pix_fmt {pix_fmt!r}")
        ff = [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "yuv4mpegpipe", "-i", "-",
              "-vf", vf, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
        res = process.run_pipeline([self.serve(script, first, last), ff], env=self.env(), log_path=self._log(name),
                                   capture_stdout=True, cancel=cancel)
        data = res.stdout or b""
        channels = 1 if pix_fmt == "gray" else 3
        dtype = np.uint16 if pix_fmt == "rgb48le" else np.uint8
        per = width * height * channels * np.dtype(dtype).itemsize
        if res.returncode != 0 or len(data) != per * n:
            raise ToolError(f"could not read frames {first}..{last} of {script} (got {len(data)} bytes for "
                            f"{n} frames of {per}): {res.describe()}")
        arr = np.frombuffer(data, dtype)
        return arr.reshape(n, height, width, 3) if channels == 3 else arr.reshape(n, height, width)


@dataclass
class ScriptScan:
    thumbs: Any                                  # uint8 (frames, width*height)
    events: list[tuple[float, float, float]]     # blackdetect (start, end, duration)
    errors: list[str]

    @property
    def frames(self) -> int:
        return len(self.thumbs)


def tfm_log_script(source: Path, pad: int, tfm_log: Path | None, *, side_by_side: bool = False) -> str:
    """VapourSynth TFM chain used for cadence measurement (v38 ``_vpy_text``, verbatim logic)."""
    lines = [
        "import vapoursynth as vs",
        "core = vs.core",
        f"src = core.bs.VideoSource({str(source)!r})",
        f"src = core.std.Trim(src, {int(pad)}, None)",
        # The frame-order property is part of the measured TFM/encode chain (v38), not a guess.
        "src = core.std.SetFrameProps(src, _FieldBased=2)",
    ]
    if side_by_side:
        lines += ["matched = core.tivtc.TFM(src, order=-1, PP=0, chroma=1, slow=2)",
                  "out = core.std.StackHorizontal(src, matched)", "out.set_output()"]
    else:
        log_value = repr(str(tfm_log)) if tfm_log else "None"
        lines += [f"matched = core.tivtc.TFM(src, order=-1, PP=0, chroma=1, slow=2, output={log_value})",
                  "matched.set_output()"]
    return "\n".join(lines) + "\n"


def field_diff_script(source: Path, pad: int, ranges: list[tuple[int, int]], out_txt: Path) -> str:
    """Same-parity field differences over source-frame ranges [a, b) (post-pad), for the film-plate test.

    For each range the frames are split into fields (top first, as the TFM chain assumes) and field ``k`` is
    compared with field ``k+2``; every evaluated frame appends ``<global index> <PlaneStatsDiff>`` to ``out_txt``
    (written from a callback, so lines may be out of order; the reader sorts). Luma only, normalised 0..1, on
    quarter-size fields so that grain and capture noise (identical-looking in a repeated and a new field) do not
    swamp the picture motion the test looks for.
    """
    lines = [
        "import threading",
        "import vapoursynth as vs",
        "core = vs.core",
        f"src = core.bs.VideoSource({str(source)!r})",
        f"src = core.std.Trim(src, {int(pad)}, None)",
        "src = core.std.SetFrameProps(src, _FieldBased=2)",
        f"RANGES = {[(int(a), int(b)) for a, b in ranges]!r}",
        f"OUT = {str(out_txt)!r}",
        "_lock = threading.Lock()",
        "parts = []",
        "for a, b in RANGES:",
        "    f = core.std.SeparateFields(core.std.Trim(src, a, b - 1), tff=True)",
        "    # quarter-size area average: grain/noise largely cancels, picture motion does not",
        "    f = core.resize.Bilinear(f, f.width // 4, f.height // 4)",
        "    parts.append(core.std.PlaneStats(core.std.Trim(f, 0, f.num_frames - 3), core.std.Trim(f, 2, None), plane=0))",
        "clip = parts[0] if len(parts) == 1 else core.std.Splice(parts)",
        "def _log(n, f):",
        "    with _lock, open(OUT, 'a', encoding='ascii') as fh:",
        "        fh.write('%d %.6f\\n' % (n, f.props['PlaneStatsDiff']))",
        "    return f",
        "clip = core.std.ModifyFrame(clip, clip, _log)",
        "clip.set_output()",
    ]
    return "\n".join(lines) + "\n"


def read_field_diffs(text: str, ranges: list[tuple[int, int]]) -> list[list[float]]:
    """Split the logged differences back into one list per range (2·(b−a)−2 values each)."""
    vals: dict[int, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            vals[int(parts[0])] = float(parts[1])
    out, i = [], 0
    for a, b in ranges:
        n = 2 * (b - a) - 2
        seg = [vals.get(k) for k in range(i, i + n)]
        if any(v is None for v in seg):
            raise ValueError(f"field-difference log is missing values for range {a}..{b}")
        out.append(seg)
        i += n
    return out


def parse_info(text: str) -> dict:
    """Parse ``vspipe --info`` output (Frames/FPS/Width/Height/Format lines)."""
    def grab(key: str) -> str | None:
        m = re.search(rf"(?mi)^{key}:\s*(.+?)\s*$", text)
        return m.group(1) if m else None

    frames_s, fps_s = grab("Frames"), grab("FPS")
    if not frames_s or not frames_s.isdigit():
        raise ValueError(f"vspipe --info did not report a frame count: {text[-300:]!r}")
    fps = re.match(r"(\d+)/(\d+)", fps_s or "")
    if not fps:
        raise ValueError(f"vspipe --info did not report a rational FPS: {fps_s!r}")
    num, den = int(fps.group(1)), int(fps.group(2))
    frames = int(frames_s)
    info = {"frames": frames, "fps_num": num, "fps_den": den, "duration": frames * den / num}
    for key in ("Width", "Height"):
        v = grab(key)
        if v and v.isdigit():
            info[key.lower()] = int(v)
    info["format"] = grab("Format Name") or grab("Format")
    return info
