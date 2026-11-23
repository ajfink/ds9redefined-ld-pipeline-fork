"""How a missing tool can be obtained: a package-manager command when that is safe, otherwise exact instructions.

Only tools whose output does not depend on the exact build are installed automatically (FFmpeg with libx265,
7-Zip). Decode-Orc and RIFE are *pinned* by the pipeline (their version changes the pixels and the audio), so
they are never fetched silently: the wizard says where to get them and records the one the owner points at.
"""
from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

ORC_DOCS = "https://simoninns.github.io/decode-orc/"
ORC_FLATPAK = "io.github.simoninns.decode-orc"
RIFE_RELEASES = "https://github.com/nihui/rife-ncnn-vulkan/releases"
FFMPEG_WINDOWS = "https://www.gyan.dev/ffmpeg/builds/"

#: (tool -> manager -> command). ``None`` means "this manager cannot do it safely": the wizard gives instructions.
INSTALL: dict[str, dict[str, list[str] | None]] = {
    "ffmpeg": {
        "winget": ["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--accept-package-agreements",
                   "--accept-source-agreements"],
        "brew": ["brew", "install", "ffmpeg"],
        "apt": ["sudo", "apt-get", "install", "-y", "ffmpeg"],
        "pacman": ["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"],
        "dnf": None,        # Fedora's ffmpeg-free has no libx265: RPM Fusion is needed, which is the owner's call
    },
    "sevenzip": {
        "winget": ["winget", "install", "-e", "--id", "7zip.7zip", "--accept-package-agreements",
                   "--accept-source-agreements"],
        "brew": ["brew", "install", "sevenzip"],
        "apt": ["sudo", "apt-get", "install", "-y", "p7zip-full"],
        "pacman": ["sudo", "pacman", "-S", "--noconfirm", "p7zip"],
        "dnf": ["sudo", "dnf", "install", "-y", "p7zip", "p7zip-plugins"],
    },
}
#: ffprobe ships with ffmpeg
INSTALLS_WITH = {"ffprobe": "ffmpeg"}

MANAGERS = {"windows": ("winget",), "macos": ("brew",), "linux": ("apt", "dnf", "pacman")}
MANAGER_BINARY = {"winget": "winget", "brew": "brew", "apt": "apt-get", "dnf": "dnf", "pacman": "pacman"}


def detect_manager(system: str, env: Mapping[str, str] | None = None,
                   which: Callable[..., str | None] = shutil.which) -> str | None:
    path = None if env is None else env.get("PATH")
    for manager in MANAGERS.get(system, ()):
        if which(MANAGER_BINARY[manager], path=path):
            return manager
    return None


def install_command(tool: str, manager: str | None) -> list[str] | None:
    tool = INSTALLS_WITH.get(tool, tool)
    return (INSTALL.get(tool) or {}).get(manager or "")


def guidance(tool: str, system: str) -> str:
    """What to do by hand, in plain words, for a tool that is missing and not auto-installable."""
    if tool == "orc":
        page = {"windows": "installation/windows-msi/", "macos": "installation/macos-dmg/",
                "linux": "installation/linux-flatpak/"}.get(system, "")
        extra = {"macos": " The DMG is self-signed: after copying orc-gui.app to Applications run "
                          "`xattr -dr com.apple.quarantine /Applications/orc-gui.app` (or right-click → Open once).",
                 "linux": f" Install the Flatpak ({ORC_FLATPAK}); fstk can then create an `orc-cli` wrapper for it."}
        return (f"Decode-Orc is not installed. Get the release your episodes were built with (2.11.1 on the "
                f"owner's machines) from {ORC_DOCS}{page}.{extra.get(system, '')} The pipeline pins Orc's version, "
                "so install the same one on every machine.")
    if tool in ("rife_engine", "rife_model_dir"):
        return (f"RIFE (rife-ncnn-vulkan) is only needed to repair single-frame PAD blanks. Download it from "
                f"{RIFE_RELEASES} and unpack it anywhere; keep the model folder next to the program. The model "
                "(rife-v4.6 or rife-v4.26) changes the repaired frames, so it is never chosen for you.")
    if tool in ("ffmpeg", "ffprobe"):
        where = {"windows": f"Install with `winget install Gyan.FFmpeg`, or download a full build from {FFMPEG_WINDOWS}.",
                 "macos": "Install with `brew install ffmpeg`, or use a static arm64 build with libx265.",
                 "linux": "Install ffmpeg with your package manager (it must include libx265)."}
        return "FFmpeg 9.x with libx265 is required. " + where.get(system, "")
    if tool == "sevenzip":
        return "7-Zip is optional (unpacking archives). Install 7-Zip (7z/7zz) if you want it."
    if tool == "curl":
        return "curl is optional (downloads); it comes with Windows 10+, macOS and most Linux systems."
    return f"{tool} is not installed."


def flatpak_has_orc(runner: Callable[[list[str]], int]) -> bool:
    try:
        return runner(["flatpak", "info", ORC_FLATPAK]) == 0
    except OSError:
        return False


def write_orc_flatpak_wrapper(directory: Path) -> Path:
    """An ``orc-cli`` shell script that runs Decode-Orc's CLI inside its Flatpak with access to the file system.

    The sandbox cannot see the capture and working folders unless it is told to (``--filesystem=host`` on this
    run only, so nothing global changes).
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "orc-cli"
    path.write_text(f'#!/bin/sh\nexec flatpak run --filesystem=host --command=orc-cli {ORC_FLATPAK} "$@"\n',
                    encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def ffmpeg_has_x265(path: str, runner: Callable[[list[str]], str]) -> bool | None:
    """Does this FFmpeg list the libx265 encoder? ``None`` when it could not be asked."""
    try:
        text = runner([path, "-hide_banner", "-encoders"])
    except (OSError, ValueError):
        return None
    return "libx265" in text if text else None


def is_executable(path: str) -> bool:
    return os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK))
