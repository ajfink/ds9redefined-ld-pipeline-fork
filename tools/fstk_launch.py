#!/usr/bin/env python3
"""FromStunToKill launcher core. Standard library only; run by the platform launchers with Python 3.12-3.15.

    fstk_launch.py                  start the app (setting everything up first if this machine is new)
    fstk_launch.py setup [options]  run the setup wizard again (find tools, repair the profile)
    fstk_launch.py check            read-only readiness report
    fstk_launch.py cli <args...>    run an `fstk` command line (e.g. `cli episode create ...`)
    fstk_launch.py env              show where this machine keeps the environment and profile
    fstk_launch.py reset-env        delete this machine's Python environment (rebuilt on the next start)

The app folder is never written to: the Python environment, the bytecode cache and the profile live on this
machine (per user), so the same folder can sit on a share that several computers use at once.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
MIN_PY, BELOW_PY = (3, 12), (3, 16)          # PySide6 does not support 3.16 yet
EXTRAS = ("gui", "media")                    # everything the app needs; "dev" (tests) is not installed


def say(text: str = "") -> None:
    print(text, flush=True)


def system() -> str:
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")


def machine_dir() -> Path:
    """Per-user, per-machine folder for the environment (never the shared app folder)."""
    home = Path.home()
    if system() == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "fstk"
    if system() == "macos":
        return home / "Library" / "Application Support" / "FromStunToKill"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "fstk"


def venv_dir() -> Path:
    key = hashlib.sha1(str(APP).encode("utf-8")).hexdigest()[:8]      # one environment per copy of the app
    return machine_dir() / "venvs" / key


def venv_python(windowed: bool = False) -> Path:
    base = venv_dir()
    if system() == "windows":
        return base / "Scripts" / ("pythonw.exe" if windowed else "python.exe")
    return base / "bin" / "python"


LOCK_FILE = APP / "vendor" / "vapoursynth-plugins" / "LOCK.json"


def lock() -> dict:
    """The pinned VapourSynth + plugin set (see vendor/vapoursynth-plugins/README.md)."""
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"packages": {}}


def _name(requirement: str) -> str:
    return re.split(r"[\s<>=!~;\[(]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def requirements() -> list[str]:
    """pyproject's dependencies and extras, with the locked plugin set pinned to the tested versions."""
    meta = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = list(meta.get("dependencies", []))
    for extra in EXTRAS:
        reqs += meta.get("optional-dependencies", {}).get(extra, [])
    pins = {name.lower(): info["version"] for name, info in lock().get("packages", {}).items()}
    return [f"{_name(r)}=={pins[_name(r)]}" if _name(r) in pins else r for r in reqs]


def is_mac_arm64() -> bool:
    return system() == "macos" and platform.machine().lower() in ("arm64", "aarch64")


def vendored_wheels() -> list[Path] | None:
    """The sha256-verified macOS arm64 wheels from the repository, or None (use PyPI instead)."""
    if not is_mac_arm64():
        return None
    found: list[Path] = []
    for name, info in lock().get("packages", {}).items():
        path = LOCK_FILE.parent / info["file"]
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            say(f"Vendored wheel for {name} is missing ({path.name}); using PyPI for the plugins.")
            return None
        if digest != info["sha256"]:
            say(f"Vendored wheel {path.name} does not match its recorded sha256; using PyPI for the plugins.")
            return None
        found.append(path)
    return found or None


def stamp() -> dict:
    return {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system(),
            "vsconfig": 1,        # bump to make every environment run `vapoursynth config` again
            "requirements": hashlib.sha256("\n".join(sorted(requirements())).encode()).hexdigest()}


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(APP / "src"), env.get("PYTHONPATH", "")) if p)
    env["PYTHONPYCACHEPREFIX"] = str(machine_dir() / "pycache")      # no .pyc files inside the app folder
    env["FSTK_APP_ROOT"] = str(APP)
    return env


def python_ok() -> bool:
    return MIN_PY <= sys.version_info[:2] < BELOW_PY


def ensure_env() -> Path | None:
    """The environment's Python, building or updating the environment first when needed. None = failed."""
    vpy = venv_python()
    marker = venv_dir() / ".fstk-stamp.json"
    want = stamp()
    have = None
    with contextlib.suppress(OSError, ValueError):
        have = json.loads(marker.read_text(encoding="utf-8"))
    if vpy.is_file() and have == want:
        return vpy
    if vpy.is_file() and have and have.get("python") != want["python"]:
        say(f"Python changed ({have.get('python')} -> {want['python']}): rebuilding the environment.")
        shutil.rmtree(venv_dir(), ignore_errors=True)
    if system() == "macos" and int((platform.mac_ver()[0] or "0").split(".")[0] or 0) < 15:
        say("Note: the VapourSynth plugin wheels need macOS 15 or newer; on this macOS pip would have to build "
            "them from source, which usually fails.")
    first = not vpy.is_file()
    say(("Creating" if first else "Updating") + f" this machine's Python environment:\n  {venv_dir()}")
    say("(one time, a few minutes: it downloads the window toolkit, VapourSynth and the image libraries)")
    try:
        if first:
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir())], check=True)
        subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        wheels = vendored_wheels()
        if wheels:
            say("Installing the VapourSynth plugin set from the verified macOS arm64 wheels in vendor/.")
            subprocess.run([str(vpy), "-m", "pip", "install", *map(str, wheels)], check=True)
        subprocess.run([str(vpy), "-m", "pip", "install", *requirements()], check=True)
        if not configure_vapoursynth():
            say("Note: `vapoursynth config` did not run cleanly; `setup` will try again and report vspipe's state.")
    except (subprocess.CalledProcessError, OSError) as exc:
        say(f"\nCould not set up the Python environment: {exc}")
        if system() == "linux":
            say("On Debian/Ubuntu the `venv` module is a separate package: sudo apt install python3-venv")
        say("Fix the problem (network? disk space?) and start again; nothing else was changed.")
        return None
    marker.write_text(json.dumps(want, indent=2), encoding="utf-8")
    say("Environment ready.\n")
    return vpy


def configure_vapoursynth() -> bool:
    """Tell the pip VapourSynth where this environment's Python library is (`vapoursynth config`).

    Without it ``vspipe`` stops at "Python executable and library path couldn't be determined" and every VapourSynth
    step fails. The setting is kept per environment in the user's own config file (never in the app folder), so
    several environments on one computer do not disturb each other. Returns True when the command ran cleanly.
    """
    base = venv_dir()
    exe = base / ("Scripts/vapoursynth.exe" if system() == "windows" else "bin/vapoursynth")
    if not exe.is_file():
        return False
    return subprocess.run([str(exe), "config"], env=child_env(), check=False).returncode == 0


def run_fstk(vpy: Path, args: list[str], *, cwd: str | None = None) -> int:
    return subprocess.call([str(vpy), "-m", "fstk", *args], env=child_env(), cwd=cwd)


def pause() -> None:
    if sys.stdin and sys.stdin.isatty():
        with contextlib.suppress(EOFError):
            input("\nPress Enter to close this window. ")


def launch_gui(vpy: Path, extra: list[str]) -> int:
    probe = subprocess.run(
        [str(vpy), "-c", "import sys\nfrom fstk.install.wizard import check_environment\n"
                         "p = check_environment()\nprint('\\n'.join(p))\nsys.exit(1 if p else 0)"],
        env=child_env(), capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        say("The app cannot start:\n" + (probe.stdout or probe.stderr).strip())
        pause()
        return 4
    env = child_env()
    if system() == "windows":                       # no console window behind the app
        flags = 0x00000008 | 0x00000200             # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(venv_python(windowed=True)), "-m", "fstk.ui.app", *extra], env=env,
                         cwd=str(Path.home()), creationflags=flags, close_fds=True)
        return 0
    os.chdir(Path.home())
    os.execve(str(vpy), [str(vpy), "-m", "fstk.ui.app", *extra], env)
    return 0                                        # not reached


def has_profile(vpy: Path) -> bool:
    return subprocess.run([str(vpy), "-m", "fstk", "profile", "show"], env=child_env(), capture_output=True,
                          check=False).returncode == 0


def main(argv: list[str]) -> int:
    if not python_ok():
        say(f"Python {platform.python_version()} cannot run this app: 3.12 to 3.15 is required "
            f"(this is {sys.executable}).")
        return 3
    mode = argv[0] if argv and not argv[0].startswith("-") else "gui"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    if mode == "env":
        say(f"app folder  : {APP}\nenvironment : {venv_dir()}\nmachine dir : {machine_dir()}")
        return 0
    if mode == "reset-env":
        shutil.rmtree(venv_dir(), ignore_errors=True)
        say(f"Removed {venv_dir()}; the next start rebuilds it.")
        return 0
    vpy = ensure_env()
    if vpy is None:
        pause()
        return 2
    if mode == "setup":
        return run_fstk(vpy, ["setup", *rest])
    if mode == "check":
        return run_fstk(vpy, ["doctor", *rest])
    if mode in ("cli", "run"):
        return run_fstk(vpy, rest)
    if mode != "gui":
        say(f"unknown command {mode!r}; one of: gui (default), setup, check, cli, env, reset-env")
        return 2
    if not has_profile(vpy):
        say("This machine has no profile yet: running the setup wizard first.\n")
        run_fstk(vpy, ["setup"])
        if not has_profile(vpy):
            say("\nSetup did not create a profile, so the app was not started. Run it again to retry.")
            pause()
            return 1
        if sys.stdin and sys.stdin.isatty():
            try:
                if input("\nStart the app now? [Y/n] ").strip().lower().startswith("n"):
                    return 0
            except EOFError:
                pass
    return launch_gui(vpy, rest)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
