"""Where the external tools usually live on each platform (pure: the OS, environment and home are parameters).

Finding is separate from choosing: ``discover`` returns every match in order of preference and the setup wizard
decides (and asks when the choice affects the output, e.g. which RIFE model).
"""
from __future__ import annotations

import glob
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fstk.config.profile import KNOWN_TOOLS

TOOL_ORDER = ("vspipe", "ffmpeg", "ffprobe", "orc", "rife_engine", "rife_model_dir", "sevenzip", "curl")

#: Folders a program started from Finder, the Dock or a desktop shortcut has no PATH for: a GUI process inherits
#: the system PATH (``/usr/bin:/bin:...``), not the one the owner's shell sets up, so Homebrew and MacPorts are
#: invisible. Searching them is what lets setup find - and offer to install - tools after a double-click.
GUI_PATH: dict[str, tuple[str, ...]] = {
    "macos": ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/opt/local/bin",
              "~/.local/bin", "~/bin"),
    "linux": ("/usr/local/bin", "~/.local/bin", "~/bin", "/home/linuxbrew/.linuxbrew/bin", "/snap/bin"),
    "windows": (),
}


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


def search_path(system: str, env: Mapping[str, str], home: Path, extra: Iterable[Path] = ()) -> str:
    """The PATH to search: what the process was given, then the folders a GUI launch leaves out, then ``extra``
    (the shared folder's own tools, when the owner keeps them there). Nothing is removed and nothing is run."""
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    for spot in (*GUI_PATH.get(system, ()), *(str(p) for p in extra)):
        text = spot.replace("~", str(home))
        if text not in parts:
            parts.append(text)
    return os.pathsep.join(parts)


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
        # the macOS and Linux releases are unpacked wherever the owner put them; look where downloads land too
        return [*own, "~/tools/rife-ncnn-vulkan/rife-ncnn-vulkan", "~/tools/rife-ncnn-vulkan*/rife-ncnn-vulkan",
                "/opt/homebrew/bin/rife-ncnn-vulkan", "/usr/local/bin/rife-ncnn-vulkan",
                "~/Applications/rife-ncnn-vulkan*/rife-ncnn-vulkan", "~/Downloads/rife-ncnn-vulkan*/rife-ncnn-vulkan"]
    if name == "sevenzip":
        if system == "windows":
            return ["%ProgramFiles%/7-Zip/7z.exe", "%ProgramFiles(x86)%/7-Zip/7z.exe"]
        # Homebrew installs the program as 7zz; p7zip and MacPorts use 7z / 7za
        return [f"{d}/{exe}" for d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin",
                                       "/usr/lib/p7zip", "~/tools") for exe in ("7zz", "7z", "7za")]
    if name == "curl":
        return ["%SystemRoot%/System32/curl.exe"] if system == "windows" else []
    return own


def _model_dirs(engine: str | None, system: str, env: Mapping[str, str], home: Path,
                extra: Iterable[Path] = ()) -> list[str]:
    """RIFE model folders next to the engine, in the places Flowframes keeps them, or in a shared tools folder.

    The model decides what a repaired frame looks like, so every machine must use the same one: a folder the
    owner shares between machines is searched as well, and the choice is never made automatically.
    """
    spots: list[str] = []
    if engine:
        spots.append(str(Path(engine).parent / "rife-v*"))
    if system == "windows":
        spots.append("%LOCALAPPDATA%/Flowframes/FlowframesData/pkgs/rife-ncnn-vs/rife-v*")
    spots += ["~/tools/rife-ncnn-vulkan/rife-v*", "~/tools/rife-v*"]
    spots += [f"{d}/rife-v*" for d in (str(p) for p in extra)]
    spots += [f"{d}/rife-ncnn-vulkan/rife-v*" for d in (str(p) for p in extra)]
    out: list[str] = []
    for spot in spots:
        for p in _expand(spot, env, home):
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def discover(name: str, *, system: str, env: Mapping[str, str] | None = None, home: Path | None = None,
             venv_bin: Path | None = None, engine: str | None = None,
             extra_dirs: Iterable[Path] = ()) -> list[Found]:
    """Every copy of ``name`` that can be found, best first. Never runs anything.

    ``extra_dirs`` are folders the owner keeps tools in (typically ``<shared folder>/tools``), searched after the
    machine's own PATH and the GUI folders.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    extra_dirs = list(extra_dirs)
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
            for p in _model_dirs(engine, system, env, home, extra_dirs):
                add(p, "known location")
        return found
    if venv_bin is not None and name == "vspipe":
        for exe in ("vspipe.exe", "vspipe"):
            if (venv_bin / exe).is_file():
                add(str(venv_bin / exe), "this environment")
    path = search_path(system, env, home, extra_dirs)
    for candidate in spec["path_names"]:
        hit = shutil.which(candidate, path=path)
        if hit:
            add(hit, "PATH")
    exe = ".exe" if system == "windows" else ""
    for folder in extra_dirs:            # a shared tools folder: by file name, since a share can drop the exec bit
        for candidate in spec["path_names"]:
            spot = Path(folder) / f"{candidate}{exe}"
            if spot.is_file():
                add(str(spot), "shared folder")
    for pattern in _locations(name, system, tools_dir(system, env, home)):
        for p in _expand(pattern, env, home):
            if os.path.isfile(p):
                add(p, "known location")
    return found
