"""Where the external tools usually live on each platform (pure: the OS, environment and home are parameters).

Finding is separate from choosing: ``discover`` returns every match in order of preference and the setup wizard
decides (and asks when the choice affects the output, e.g. which RIFE model).
"""
from __future__ import annotations

import glob
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from fstk.config.profile import KNOWN_TOOLS

TOOL_ORDER = ("vspipe", "ffmpeg", "ffprobe", "orc", "rife_engine", "rife_model_dir", "sevenzip", "curl")


@dataclass(frozen=True)
class Found:
    name: str
    path: str
    how: str                    # "this environment" | "PATH" | "known location"


def tools_dir(system: str, env: Mapping[str, str], home: Path) -> Path:
    """fstk's own per-machine folder for tools it keeps (wrapper scripts); never inside the app folder."""
    if system == "windows":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "fstk" / "tools"
    if system == "macos":
        return home / "Library" / "Application Support" / "FromStunToKill" / "tools"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "fstk" / "tools"


def _expand(pattern: str, env: Mapping[str, str], home: Path) -> list[str]:
    text = pattern
    for key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "SystemRoot", "USERPROFILE"):
        text = text.replace(f"%{key}%", env.get(key) or "\0")
    text = text.replace("~", str(home))
    if "\0" in text:
        return []
    return sorted(glob.glob(text)) if any(c in text for c in "*?[") else [text]


def _locations(name: str, system: str, tools: Path) -> list[str]:
    exe = ".exe" if system == "windows" else ""
    own = [str(tools / f"{name}{exe}"), str(tools / "bin" / f"{name}{exe}")]
    if name in ("ffmpeg", "ffprobe"):
        if system == "windows":
            return [*own, f"%LOCALAPPDATA%/Microsoft/WinGet/Links/{name}.exe",
                    f"%LOCALAPPDATA%/Microsoft/WinGet/Packages/Gyan.FFmpeg*/*/bin/{name}.exe",
                    f"%ProgramFiles%/ffmpeg/bin/{name}.exe", f"C:/ffmpeg/bin/{name}.exe",
                    f"~/tools/ffmpeg/bin/{name}.exe", f"~/tools/ffmpeg/{name}.exe"]
        if system == "macos":
            return [*own, f"~/tools/ffmpeg/{name}", f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}",
                    f"/opt/local/bin/{name}"]
        return [*own, f"/usr/bin/{name}", f"/usr/local/bin/{name}", f"~/.local/bin/{name}", f"~/tools/ffmpeg/{name}"]
    if name == "orc":
        if system == "windows":
            return [*own, "%ProgramFiles%/Decode-Orc/bin/orc-cli.exe", "%ProgramFiles(x86)%/Decode-Orc/bin/orc-cli.exe",
                    "%LOCALAPPDATA%/Programs/Decode-Orc/bin/orc-cli.exe"]
        if system == "macos":
            return [*own, "/Applications/orc-gui.app/Contents/MacOS/orc-cli",
                    "~/Applications/orc-gui.app/Contents/MacOS/orc-cli"]
        return [*own, "/usr/bin/orc-cli", "/usr/local/bin/orc-cli", "/opt/decode-orc/bin/orc-cli",
                "~/.local/bin/orc-cli"]
    if name == "rife_engine":
        if system == "windows":
            return [*own, "%LOCALAPPDATA%/Flowframes/FlowframesData/pkgs/rife-ncnn/rife-ncnn-vulkan.exe",
                    "~/tools/rife-ncnn-vulkan/rife-ncnn-vulkan.exe"]
        return [*own, "~/tools/rife-ncnn-vulkan/rife-ncnn-vulkan", "/usr/local/bin/rife-ncnn-vulkan"]
    if name == "sevenzip":
        if system == "windows":
            return ["%ProgramFiles%/7-Zip/7z.exe", "%ProgramFiles(x86)%/7-Zip/7z.exe"]
        return []
    if name == "curl":
        return ["%SystemRoot%/System32/curl.exe"] if system == "windows" else []
    return own


def _model_dirs(engine: str | None, system: str, env: Mapping[str, str], home: Path) -> list[str]:
    """RIFE model folders next to the engine, or in the places Flowframes keeps them."""
    spots: list[str] = []
    if engine:
        spots.append(str(Path(engine).parent / "rife-v*"))
    if system == "windows":
        spots.append("%LOCALAPPDATA%/Flowframes/FlowframesData/pkgs/rife-ncnn-vs/rife-v*")
    spots += ["~/tools/rife-ncnn-vulkan/rife-v*"]
    out: list[str] = []
    for spot in spots:
        for p in _expand(spot, env, home):
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def discover(name: str, *, system: str, env: Mapping[str, str] | None = None, home: Path | None = None,
             venv_bin: Path | None = None, engine: str | None = None) -> list[Found]:
    """Every copy of ``name`` that can be found, best first. Never runs anything."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    spec = KNOWN_TOOLS[name]
    found: list[Found] = []
    seen: set[str] = set()

    def add(path: str, how: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append(Found(name, path, how))

    if spec["kind"] == "dir":
        if name == "rife_model_dir":
            for p in _model_dirs(engine, system, env, home):
                add(p, "known location")
        return found
    if venv_bin is not None and name == "vspipe":
        for exe in ("vspipe.exe", "vspipe"):
            if (venv_bin / exe).is_file():
                add(str(venv_bin / exe), "this environment")
    for candidate in spec["path_names"]:
        hit = shutil.which(candidate, path=env.get("PATH"))
        if hit:
            add(hit, "PATH")
    for pattern in _locations(name, system, tools_dir(system, env, home)):
        for p in _expand(pattern, env, home):
            if os.path.isfile(p):
                add(p, "known location")
    return found
