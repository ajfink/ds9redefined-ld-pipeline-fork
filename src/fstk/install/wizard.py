"""``fstk setup``: a re-runnable first-run wizard.

Finds each external tool, installs what a package manager can install safely (after asking), explains the rest,
checks the drive space for the work folders, writes this machine's profile and ends with the readiness report.
Everything the owner types is optional: Enter skips, and every answer can be changed later in Settings.

Re-running never discards a choice: tools already configured and still present are kept, folders of an existing
profile are left alone, and a profile that cannot be read is replaced only after asking (the old one is kept as
``.bak``).
"""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fstk.config.profile import KNOWN_TOOLS, SCHEMA, Profile, current_os, default_profile_path
from fstk.core.errors import ConfigError
from fstk.core.timeutil import utc_now_iso
from fstk.install import orc as orc_install
from fstk.install import providers
from fstk.install import rife as rife_install
from fstk.install.discover import TOOL_ORDER, Found, discover, tools_dir

LABELS = {"vspipe": "VapourSynth (vspipe)", "ffmpeg": "FFmpeg", "ffprobe": "FFprobe",
          "orc": "Decode-Orc (orc-cli)", "rife_engine": "RIFE engine (rife-ncnn-vulkan)",
          "rife_model_dir": "RIFE model folder", "sevenzip": "7-Zip", "curl": "curl"}


#: optional tools the launcher also reports when they were never set up (7-Zip for .7z captures, RIFE for PAD repair)
OPTIONAL_CHECKED = ("rife_engine", "rife_model_dir", "sevenzip")


@dataclass
class Console:
    """Questions and answers, replaceable in tests. Not interactive = never asks, never installs."""

    interactive: bool = True
    assume_yes: bool = False
    ask_fn: Callable[[str], str] = input
    out: Callable[[str], None] = print

    def say(self, text: str = "") -> None:
        self.out(text)

    def ask(self, question: str, default: str = "") -> str:
        if not self.interactive:
            return default
        try:
            answer = self.ask_fn(f"{question} ").strip()
        except EOFError:
            return default
        return answer or default

    def confirm(self, question: str, default: bool = True) -> bool:
        if self.assume_yes:
            return True
        if not self.interactive:
            return False
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
        return default if not answer else answer.startswith("y")


@dataclass
class SetupResult:
    profile_path: Path | None = None
    created: bool = False
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    ok: bool = False


def _run(cmd: list[str]) -> int:
    return subprocess.call(cmd)


def _run_text(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False).stdout


def _clean_path(text: str) -> str:
    return os.path.expanduser(text.strip().strip('"').strip("'"))


def _resolve_user_path(name: str, text: str) -> str | None:
    """A path the owner typed, made into what the profile needs (an .app bundle becomes its orc-cli)."""
    p = Path(_clean_path(text))
    spec = KNOWN_TOOLS[name]
    if name == "orc" and p.suffix == ".app":
        p = p / "Contents" / "MacOS" / "orc-cli"
    if spec["kind"] == "dir":
        return str(p) if p.is_dir() else None
    return str(p) if p.is_file() else None


def run_setup(console: Console, *, profile_path: str | os.PathLike | None = None, system: str | None = None,
              env: Mapping[str, str] | None = None, home: Path | None = None,
              run: Callable[[list[str]], int] = _run, run_text: Callable[[list[str]], str] = _run_text,
              install: bool = True, check_only: bool = False, venv_bin: Path | None = None,
              volumes=None, doctor: Callable | None = None, orc_installer: Callable | None = None,
              rife_installer: Callable | None = None, rife_model_installer: Callable | None = None) -> SetupResult:
    system = system or current_os()
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if venv_bin is None:
        venv_bin = Path(sys.prefix) / ("Scripts" if system == "windows" else "bin")
    path = Path(profile_path) if profile_path else default_profile_path()
    result = SetupResult(profile_path=path)
    say = console.say
    say(f"FromStunToKill setup — {platform.system()} {platform.machine()}, Python {platform.python_version()}")

    profile = _load_or_create(console, path, system, result, check_only=check_only, volumes=volumes)
    if profile is None:
        return result
    _tools(console, profile, system=system, env=env, home=home, run=run, run_text=run_text,
           install=install and not check_only, venv_bin=venv_bin, result=result,
           orc_installer=orc_installer or orc_install.install_orc, rife_installer=rife_installer,
           rife_model_installer=rife_model_installer)
    _ensure_vspipe(console, profile, venv_bin, run, system, result, check_only=check_only)
    if result.created and not check_only:
        _sources(console, profile, result, system=system)
    elif not check_only and console.interactive and not profile.sources.get("downloads_root"):
        _sources(console, profile, result, system=system, only_unset=True)
    if (result.changes or result.created) and not check_only:
        for root in (profile.data_root, profile.media_root):
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                result.problems.append(f"could not create {root}: {exc}")
        profile.notes.append(f"[setup] updated by `fstk setup` {utc_now_iso()} on {platform.system()}")
        profile.save(path)
        say(f"\nProfile saved: {path}")
    if doctor is None:
        from fstk.tools.doctor import run_doctor as doctor
    report = doctor(profile)
    bad = [c for c in report["checks"] if c["status"] in ("FAIL", "WARN")]
    say("\nReadiness:")
    for row in bad:
        say(f"  [{row['status']:4}] {row['name']}: {row['detail']}")
    fails = sum(1 for c in bad if c["status"] == "FAIL")
    say(f"  {len(report['checks']) - len(bad)} checks passed, {len(bad) - fails} warnings, {fails} failures")
    result.ok = bool(report["ok"])
    return result


def _load_or_create(console: Console, path: Path, system: str, result: SetupResult, *, check_only: bool,
                    volumes) -> Profile | None:
    if path.exists():
        try:
            profile = Profile.load(path)
            console.say(f"Using the existing profile {path} ({profile.machine}).")
            return profile
        except ConfigError as exc:
            console.say(f"The profile at {path} cannot be used: {exc}")
            if check_only or not console.confirm("Replace it with a fresh one? (the old file is kept as .bak)",
                                                 default=False):
                result.problems.append(f"profile unusable: {exc}")
                return None
    if check_only:
        result.problems.append(f"no profile at {path}")
        console.say(f"No profile at {path} yet: run setup (without --check) to create one.")
        return None
    from fstk.config.profile import default_data_root, default_media_root

    profile = Profile.from_dict({"schema": SCHEMA, "machine": socket.gethostname() or "this-machine",
                                 "os": system, "data_root": str(default_data_root().absolute()),
                                 "media_root": str(default_media_root().absolute())}, path=path)
    result.created = True
    console.say(f"\nNew profile for {profile.machine}. Work will be kept in:\n  {profile.data_root}\n  "
                f"{profile.media_root}")
    _check_space(console, profile, volumes)
    return profile


def _check_space(console: Console, profile: Profile, volumes) -> None:
    from fstk.workspace.volumes import advise

    try:
        advice = advise(profile, volumes)
    except Exception as exc:                        # advice is a courtesy; never stop setup for it
        console.say(f"(could not measure drive space: {exc})")
        return
    for a in advice:
        if a.level in ("warn", "block"):
            console.say(f"\n{a.text}")
            if a.suggestion and console.confirm(f"Use {a.suggestion} instead?"):
                if a.what == "Data folder":
                    profile.data_root = Path(a.suggestion).absolute()
                elif a.what == "Working folder":
                    profile.media_root = Path(a.suggestion).absolute()
            elif not a.suggestion:
                console.say("  No local drive here has more room. The free-space reserve is not lowered "
                            "automatically (limits.min_free_gb): free some space or attach a larger drive.")


def _pick_ffmpeg(found: list[Found], run_text: Callable[[list[str]], str], console: Console) -> Found:
    verdicts = [(f, providers.ffmpeg_has_x265(f.path, run_text)) for f in found]
    for f, has in verdicts:
        if has:
            return f
    first = verdicts[0][0]
    console.say(f"  warning: {first.path} does not list the libx265 encoder; the final encode needs it.")
    return first


def _shared_tool_dirs(profile: Profile) -> list[Path]:
    """Folders the owner may keep shared tools in, so every machine can use the same RIFE model and programs."""
    root = profile.shared_root
    return [root / "tools", root / "tools" / "bin", root] if root else []


def _tools(console: Console, profile: Profile, *, system, env, home, run, run_text, install, venv_bin,
           result: SetupResult, orc_installer: Callable | None = None, rife_installer: Callable | None = None,
           rife_model_installer: Callable | None = None) -> None:
    say = console.say
    found_manager = providers.find_manager(system, env) if install else None
    manager, manager_program = found_manager if found_manager else (None, None)
    extra_dirs = _shared_tool_dirs(profile)
    say("\nTools:")
    for name in TOOL_ORDER:
        spec = KNOWN_TOOLS[name]
        label = LABELS[name]
        configured = profile.tools.get(name) or ""
        present = bool(configured) and (Path(configured).is_dir() if spec["kind"] == "dir"
                                        else Path(configured).is_file())
        if present and name == "rife_model_dir" and Path(configured).name != providers.RIFE_MODEL:
            say(f"  changed  {label}: {configured} is not the standard model ({providers.RIFE_MODEL}); looking again")
        elif present:
            say(f"  ok       {label}: {configured}")
            continue
        elif configured:
            say(f"  changed  {label}: {configured} is gone; looking again")
        engine = profile.tools.get("rife_engine")
        found = discover(name, system=system, env=env, home=home, venv_bin=venv_bin, engine=engine,
                         extra_dirs=extra_dirs)
        if name == "rife_model_dir" and install and profile.tools.get("rife_engine") and not any(
                Path(f.path).name == providers.RIFE_MODEL for f in found):
            found = _install_rife_model(console, profile, env, found, rife_model_installer, home, system,
                                        venv_bin, extra_dirs)
        if not found and install:
            found = _try_install(console, name, system, env, home, venv_bin, manager, run, engine=engine,
                                 profile=profile, program=manager_program, extra_dirs=extra_dirs,
                                 orc_installer=orc_installer, rife_installer=rife_installer)
        choice = _choose(console, name, found, run_text, sibling_of=profile.tools.get("ffmpeg"))
        if choice is None:
            choice = _ask_path(console, name, label, spec["required"], system)
        if choice is None:
            level = "MISSING " if spec["required"] else "optional"
            say(f"  {level} {label}")
            if spec["required"]:
                result.problems.append(f"{label} is required and was not found")
            continue
        profile.tools[name] = choice
        result.changes.append(f"tools.{name} = {choice}")
        say(f"  found    {label}: {choice}")


def _install_rife_model(console: Console, profile: Profile, env, found: list[Found], installer, home, system,
                        venv_bin, extra_dirs) -> list[Found]:
    """The standard RIFE model is missing: offer to download it next to the engine, then look again."""
    engine_dir = Path(profile.tools["rife_engine"]).parent
    if not console.confirm(f"  The standard RIFE model ({providers.RIFE_MODEL}) is missing. Download it next to "
                           f"the engine now?"):
        return found
    (installer or rife_install.install_model)(console, engine_dir=engine_dir, env=env)
    return discover("rife_model_dir", system=system, env=env, home=home, venv_bin=venv_bin,
                    engine=profile.tools["rife_engine"], extra_dirs=extra_dirs)


def _try_install(console: Console, name: str, system: str, env, home: Path, venv_bin, manager, run, *, engine,
                 profile: Profile, program: str | None = None, extra_dirs=(),
                 orc_installer: Callable | None = None, rife_installer: Callable | None = None) -> list[Found]:
    def look() -> list[Found]:
        return discover(name, system=system, env=env, home=home, venv_bin=venv_bin, engine=engine,
                        extra_dirs=extra_dirs)

    if name == "orc":
        return _try_install_orc(console, system, env, home, run, look, orc_installer)
    if name == "rife_engine":
        if not console.confirm(f"  {LABELS[name]} is missing. Download it from GitHub and install it now?"):
            console.say("  " + providers.guidance(name, system))
            return []
        installer = rife_installer or rife_install.install_rife
        installer(console, system=system, env=env, home=home, dest=tools_dir(system, env, home) / rife_install.FOLDER)
        return look()
    cmd = providers.install_command(name, manager, program)
    if cmd:
        pretty = " ".join(cmd)
        if console.confirm(f"  {LABELS[name]} is missing. Install it now with `{pretty}`?"):
            rc = run(cmd)
            if rc != 0:
                console.say(f"  the installer exited with code {rc}")
                console.say("  " + providers.guidance(name, system))
            return look()
        console.say("  " + providers.guidance(name, system))
        return []
    if name not in ("vspipe", "rife_model_dir", "ffprobe"):
        console.say("  " + providers.guidance(name, system))
    return []


def _try_install_orc(console: Console, system: str, env, home: Path, run, look, orc_installer) -> list[Found]:
    """Decode-Orc: use a Flatpak that is already there, else offer to download and install the pinned release."""
    wrapper_dir = tools_dir(system, env, home) / "bin"
    app_id = providers.orc_flatpak_id(run) if system == "linux" else None
    if app_id is None:
        question = (f"  {LABELS['orc']} is missing. Download Decode-Orc {providers.ORC_VERSION} from GitHub "
                    "and install it now?")
        if not console.confirm(question):
            console.say("  " + providers.guidance("orc", system))
            return []
        if not (orc_installer or orc_install.install_orc)(console, system=system, env=env, home=home, run=run):
            return []
        found = look()
        if found or system != "linux":
            return found
        app_id = providers.orc_flatpak_id(run)
    if app_id is not None and system == "linux" and (
            console.assume_yes or console.confirm("  Decode-Orc is installed as a Flatpak. Create an `orc-cli` "
                                                  "wrapper for it?")):
        providers.write_orc_flatpak_wrapper(wrapper_dir, app_id)
    return look()


def _choose(console: Console, name: str, found: list[Found], run_text, sibling_of: str | None = None) -> str | None:
    if not found:
        return None
    if name == "ffmpeg":
        return _pick_ffmpeg(found, run_text, console).path
    if name == "ffprobe":
        # the probe that belongs to the chosen FFmpeg (same folder) beats one from another build
        same = [f for f in found if sibling_of and Path(f.path).parent == Path(sibling_of).parent]
        return (same or found)[0].path
    if name == "rife_model_dir":
        # one standard model on every platform; another version is never taken without being asked
        pinned = [f for f in found if Path(f.path).name == providers.RIFE_MODEL]
        if pinned:
            return pinned[0].path
        others = ", ".join(sorted(Path(f.path).name for f in found))
        console.say(f"  The standard RIFE model ({providers.RIFE_MODEL}) was not found (only {others}); a different "
                    "model repairs frames differently.")
        console.say(f"  Copy the {providers.RIFE_MODEL} folder next to the RIFE engine or into the shared folder's "
                    "`tools` directory, then run setup again.")
        # asked in words even under --yes
        if len(found) == 1 and console.ask(f"  Use {Path(found[0].path).name} for now? [y/N]").lower().startswith("y"):
            return found[0].path
        return None
    return found[0].path


def _ask_path(console: Console, name: str, label: str, required: bool, system: str) -> str | None:
    if not console.interactive:
        return None
    if name in ("vspipe", "curl", "sevenzip", "ffprobe"):
        return None                                  # found with their package, or optional enough to skip
    if name == "rife_model_dir":
        return None
    typed = console.ask(f"  Path to {label}{'' if required else ' (optional)'} if you have it (Enter to skip):")
    if not typed:
        return None
    resolved = _resolve_user_path(name, typed)
    if resolved is None:
        console.say(f"  {typed} is not a {'folder' if KNOWN_TOOLS[name]['kind'] == 'dir' else 'file'}; skipped.")
    return resolved


def _mounted_volumes(system: str) -> list[str]:
    """Names of the drives and shares mounted here, as a hint for the folder questions (names only)."""
    roots = {"macos": "/Volumes", "linux": "/media"}
    base = roots.get(system)
    try:
        names = sorted(os.listdir(base)) if base else []
    except OSError:
        return []
    return [f"{base}/{n}" for n in names if not n.startswith(".") and n != "Macintosh HD"]


def _ensure_vspipe(console: Console, profile: Profile, venv_bin: Path, run, system: str, result: SetupResult, *,
                   check_only: bool) -> None:
    """The pip VapourSynth needs `vapoursynth config` once per environment, or vspipe exits 1 with
    "Python executable and library path couldn't be determined". Repair it when vspipe is this environment's own."""
    try:
        vspipe = Path(profile.tool("vspipe"))
    except Exception:
        return
    if vspipe.parent != venv_bin or run([str(vspipe), "--version"]) == 0:
        return
    exe = venv_bin / ("vapoursynth.exe" if system == "windows" else "vapoursynth")
    if check_only or not exe.is_file():
        result.problems.append("vspipe does not run: its VapourSynth runtime is not configured "
                               "(run `setup` without --check to repair it)")
        return
    console.say("\nvspipe does not start yet: configuring this environment's VapourSynth (`vapoursynth config`)...")
    run([str(exe), "config"])
    if run([str(vspipe), "--version"]) == 0:
        console.say("  vspipe now runs.")
        result.changes.append("vapoursynth config")
    else:
        result.problems.append("vspipe still does not run after `vapoursynth config`")


def _sources(console: Console, profile: Profile, result: SetupResult, *, system: str = "",
             only_unset: bool = False) -> None:
    if not console.interactive:
        return
    console.say("\nWhere your material is (change later in Settings → Folders):")
    mounted = _mounted_volumes(system)
    if mounted:
        console.say("  Mounted here: " + ", ".join(mounted))
    for key, question in (("downloads_root", "Folder holding your LaserDisc capture folders"),
                          ("dvd_root", "Folder holding your DVD rips")):
        if only_unset and profile.sources.get(key):
            continue
        typed = console.ask(f"  {question} (Enter to skip):")
        if not typed:
            continue
        p = Path(_clean_path(typed))
        if p.is_dir():
            profile.sources[key] = str(p)
            result.changes.append(f"sources.{key} = {p}")
        else:
            console.say(f"  {p} is not reachable from here; skipped (set it later in Settings).")
    if only_unset:
        return
    typed = console.ask("  Shared workspace folder, to hand episodes between your machines "
                        "(Enter = work alone on this machine):")
    if typed:
        p = Path(_clean_path(typed))
        if p.is_dir():
            profile.shared_root = p
            result.changes.append(f"shared_root = {p}")
        else:
            console.say(f"  {p} is not reachable from here; skipped.")


def check_environment() -> list[str]:
    """Problems that stop the app starting at all (Python version, GUI libraries); empty means fine."""
    problems = []
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except ImportError as exc:
        hint = ""
        if current_os() == "linux":
            hint = (" On Debian/Ubuntu: sudo apt install libxcb-cursor0 libegl1 libgl1 libxkbcommon-x11-0 "
                    "libdbus-1-3 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0")
        problems.append(f"the window toolkit (PySide6) cannot load: {exc}.{hint}")
    return problems


def profile_problems(profile_path: str | os.PathLike | None = None) -> list[str]:
    """Tools this machine's profile relies on that are no longer there, or that a required tool still lacks.

    The launcher asks this on every start so a tool that disappeared (a cleaned-up folder, an uninstalled Homebrew
    package) is offered for repair at once instead of failing a step hours later. Empty means nothing to fix; a
    profile that cannot be read is not reported here (the app reports that itself).
    """
    path = Path(profile_path) if profile_path else default_profile_path()
    try:
        profile = Profile.load(path)
    except Exception:
        return []
    problems = []
    for name in TOOL_ORDER:
        spec = KNOWN_TOOLS[name]
        configured = profile.tools.get(name) or ""
        if configured:
            ok = Path(configured).is_dir() if spec["kind"] == "dir" else Path(configured).is_file()
            if not ok:
                problems.append(f"{LABELS[name]} is gone: {configured}")
            elif name == "rife_model_dir" and Path(configured).name != providers.RIFE_MODEL:
                problems.append(f"{LABELS[name]} is {Path(configured).name}; the standard is {providers.RIFE_MODEL}")
        elif spec["required"]:
            try:
                profile.tool(name)
            except Exception:
                problems.append(f"{LABELS[name]} is required and was not found")
        elif name in OPTIONAL_CHECKED:
            problems.append(f"{LABELS[name]} is not set up")
    return problems


__all__ = ["Console", "SetupResult", "check_environment", "profile_problems", "run_setup"]
