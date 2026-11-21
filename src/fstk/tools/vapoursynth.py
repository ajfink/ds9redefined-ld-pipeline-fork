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

    def full_decode(self, script: Path, *, name: str = "vspipe-full-decode", cancel=None) -> FullDecode:
        """Evaluate every frame of ``script`` (vspipe → FFmpeg null) and count them."""
        frames: list[int] = []
        errors: list[str] = []

        def on_line(stream: str, line: str) -> None:
            m = re.fullmatch(r"frame=(\d+)\s*", line)
            if stream == "stdout" and m:
                frames.append(int(m.group(1)))
            elif stream.startswith("ffmpeg") and line.strip():
                errors.append(line.strip())

        vs_cmd = [self.vspipe, "-c", "y4m", str(script), "-"]
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
        vs_cmd = [self.vspipe, "-c", "y4m", str(script), "-"]
        ff_cmd = [self.ffmpeg, "-y", "-hide_banner", "-nostdin", "-stats_period", "30",
                  "-f", "yuv4mpegpipe", "-i", "-", *ffmpeg_output_args]
        return process.run_pipeline([vs_cmd, ff_cmd], env=self.env(), log_path=self._log(name),
                                    cancel=cancel, on_line=on_line)


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
