"""How a missing tool can be obtained: a package-manager command when that is safe, otherwise exact instructions.

Tools whose output does not depend on the exact build are installed with the package manager (FFmpeg with
libx265, 7-Zip). Decode-Orc is *pinned* by the pipeline (its version changes the pixels and the audio): it is
downloaded from its GitHub release by ``fstk.install.orc`` (the pinned version, after asking), never through a
package manager. RIFE's engine is downloaded by ``fstk.install.rife``; its model is the standard ``RIFE_MODEL`` on every platform.
"""
from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

ORC_DOCS = "https://decode-orc.github.io/decode-orc/"
ORC_RELEASES = "https://github.com/decode-orc/decode-orc/releases"
#: the release the owner's episodes were built with; the installer fetches this one
ORC_VERSION = "2.11.1"
#: Flatpak application ids, current first (upstream renamed it from the simoninns one)
ORC_FLATPAKS = ("io.github.decode_orc.decode-orc", "io.github.simoninns.decode-orc")
ORC_FLATPAK = ORC_FLATPAKS[0]
#: the standard model on every platform (owner decision): it decides what a repaired PAD frame looks like
RIFE_MODEL = "rife-v4.26"
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
#: Where each manager lives when PATH does not mention it - the usual case after a Finder or Dock launch, which
#: gives a process the system PATH and not the owner's shell PATH. Homebrew is at /opt/homebrew on Apple silicon.
MANAGER_LOCATIONS: dict[str, tuple[str, ...]] = {
    "brew": ("/opt/homebrew/bin/brew", "/usr/local/bin/brew", "/home/linuxbrew/.linuxbrew/bin/brew"),
    "apt": ("/usr/bin/apt-get",),
    "dnf": ("/usr/bin/dnf",),
    "pacman": ("/usr/bin/pacman",),
    "winget": (),
}


def find_manager(system: str, env: Mapping[str, str] | None = None,
                 which: Callable[..., str | None] = shutil.which,
                 exists: Callable[[str], bool] = os.path.isfile) -> tuple[str, str] | None:
    """The package manager for this system as ``(name, program)``, or ``None``.

    PATH first, then the manager's own known location, so a double-clicked launcher still finds Homebrew.
    """
    path = None if env is None else env.get("PATH")
    for manager in MANAGERS.get(system, ()):
        hit = which(MANAGER_BINARY[manager], path=path)
        if hit:
            return manager, hit
        for spot in MANAGER_LOCATIONS.get(manager, ()):
            if exists(spot):
                return manager, spot
    return None


def detect_manager(system: str, env: Mapping[str, str] | None = None,
                   which: Callable[..., str | None] = shutil.which) -> str | None:
    found = find_manager(system, env, which)
    return found[0] if found else None


def install_command(tool: str, manager: str | None, program: str | None = None) -> list[str] | None:
    """The command that installs ``tool``, with ``program`` (the manager's resolved path) in front when given."""
    tool = INSTALLS_WITH.get(tool, tool)
    cmd = (INSTALL.get(tool) or {}).get(manager or "")
    if not cmd or not program:
        return cmd
    at = 1 if cmd[0] == "sudo" else 0               # keep `sudo` first; replace the manager's own name
    return [*cmd[:at], program, *cmd[at + 1:]]


def guidance(tool: str, system: str) -> str:
    """What to do by hand, in plain words, for a tool that is missing and not auto-installable."""
    if tool == "orc":
        page = {"windows": "installation/windows-msi/", "macos": "installation/macos-dmg/",
                "linux": "installation/linux-flatpak/"}.get(system, "")
        extra = {"macos": " The DMG is self-signed: after copying orc-gui.app to Applications run "
                          "`xattr -dr com.apple.quarantine /Applications/orc-gui.app` (or right-click → Open once).",
                 "linux": f" Install the Flatpak ({ORC_FLATPAK}); fstk can then create an `orc-cli` wrapper for it."}
        return (f"Decode-Orc is not installed. Get the release your episodes were built with ({ORC_VERSION} on the "
                f"owner's machines) from {ORC_RELEASES} (instructions: {ORC_DOCS}{page}).{extra.get(system, '')} "
                "The pipeline pins Orc's version, "
                "so install the same one on every machine.")
    if tool in ("rife_engine", "rife_model_dir"):
        extra = {"macos": " The macOS build runs on Metal through MoltenVK; unpack it to ~/tools/rife-ncnn-vulkan. "
                          "Put the rife-v4.26 folder next to it, or into the shared folder's `tools` directory, "
                          "which setup also searches.",
                 "linux": " Unpack it to ~/tools/rife-ncnn-vulkan; it needs a working Vulkan driver."}
        return (f"RIFE (rife-ncnn-vulkan) is only needed to repair single-frame PAD blanks. Download it from "
                f"{RIFE_RELEASES} and unpack it anywhere; keep the model folder next to the program. "
                f"The standard model on every machine is {RIFE_MODEL} (it changes the repaired frames); the public "
                "download only has older ones, so copy that folder next to the engine or into the shared `tools` folder."
                f"{extra.get(system, '')}")
    if tool in ("ffmpeg", "ffprobe"):
        where = {"windows": f"Install with `winget install Gyan.FFmpeg`, or download a full build from {FFMPEG_WINDOWS}.",
                 "macos": "Install with `brew install ffmpeg`, or use a static arm64 build with libx265.",
                 "linux": "Install ffmpeg with your package manager (it must include libx265)."}
        return "FFmpeg 9.x with libx265 is required. " + where.get(system, "")
    if tool == "sevenzip":
        how = {"windows": "`winget install 7zip.7zip`", "macos": "`brew install sevenzip` (the program is 7zz)",
               "linux": "your package manager's p7zip package"}
        return ("7-Zip unpacks the .7z EFM archives; without it those captures cannot be read. Install it with "
                + how.get(system, "your package manager") + ".")
    if tool == "curl":
        return "curl is optional (downloads); it comes with Windows 10+, macOS and most Linux systems."
    return f"{tool} is not installed."


def orc_flatpak_id(runner: Callable[[list[str]], int]) -> str | None:
    """The application id Decode-Orc is installed under as a Flatpak, or ``None``."""
    for app_id in ORC_FLATPAKS:
        try:
            if runner(["flatpak", "info", app_id]) == 0:
                return app_id
        except OSError:
            return None
    return None


def flatpak_has_orc(runner: Callable[[list[str]], int]) -> bool:
    return orc_flatpak_id(runner) is not None


def write_orc_flatpak_wrapper(directory: Path, app_id: str = ORC_FLATPAK) -> Path:
    """An ``orc-cli`` shell script that runs Decode-Orc's CLI inside its Flatpak with access to the file system.

    The sandbox cannot see the capture and working folders unless it is told to (``--filesystem=host`` on this
    run only, so nothing global changes).
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "orc-cli"
    path.write_text(f'#!/bin/sh\nexec flatpak run --filesystem=host --command=orc-cli {app_id} "$@"\n',
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
