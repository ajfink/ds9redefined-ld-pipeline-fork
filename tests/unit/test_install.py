"""Tool discovery, install providers, the setup wizard and the launcher core (all without touching the machine)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

from fstk.config.profile import Profile
from fstk.install import discover as disc
from fstk.install import providers as prov
from fstk.install import wizard as wiz

ROOT = Path(__file__).resolve().parents[2]


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------------------------------------------------------------- discovery

def test_discover_prefers_environment_then_path_then_known_locations(tmp_path):
    venv = tmp_path / "venv" / "bin"
    _exe(venv / "vspipe")
    on_path = tmp_path / "path"
    _exe(on_path / "vspipe")
    found = disc.discover("vspipe", system="linux", env={"PATH": str(on_path)}, home=tmp_path, venv_bin=venv)
    assert [f.how for f in found] == ["this environment", "PATH"]


def test_discover_finds_mac_homebrew_and_orc_app_without_running_anything(tmp_path):
    assert disc.discover("orc", system="macos", env={"PATH": ""}, home=tmp_path) == []
    cli = _exe(tmp_path / "Applications" / "orc-gui.app" / "Contents" / "MacOS" / "orc-cli")
    found = disc.discover("orc", system="macos", env={"PATH": ""}, home=tmp_path)
    assert [Path(f.path) for f in found] == [cli]


def test_discover_windows_locations_need_their_environment_variables(tmp_path):
    exe = _exe(tmp_path / "pf" / "Decode-Orc" / "bin" / "orc-cli.exe")
    env = {"PATH": "", "ProgramFiles": str(tmp_path / "pf")}
    assert [Path(f.path) for f in disc.discover("orc", system="windows", env=env, home=tmp_path)] == [exe]
    assert disc.discover("orc", system="windows", env={"PATH": ""}, home=tmp_path) == []


def test_discover_rife_models_next_to_the_engine(tmp_path):
    engine = _exe(tmp_path / "rife" / "rife-ncnn-vulkan")
    (engine.parent / "rife-v4.6").mkdir()
    (engine.parent / "rife-v4.26").mkdir()
    found = disc.discover("rife_model_dir", system="linux", env={"PATH": ""}, home=tmp_path, engine=str(engine))
    assert sorted(Path(f.path).name for f in found) == ["rife-v4.26", "rife-v4.6"]


def test_search_path_adds_the_folders_a_double_click_cannot_see(tmp_path):
    path = disc.search_path("macos", {"PATH": "/usr/bin"}, tmp_path)
    parts = path.split(":")
    assert parts[0] == "/usr/bin"                       # what the process was given is never dropped or reordered
    assert "/opt/homebrew/bin" in parts and "/opt/local/bin" in parts and f"{tmp_path}/.local/bin" in parts
    assert "/opt/homebrew/bin" not in disc.search_path("windows", {"PATH": "C:/w"}, tmp_path)
    assert "/home/linuxbrew/.linuxbrew/bin" in disc.search_path("linux", {"PATH": ""}, tmp_path)
    once = disc.search_path("macos", {"PATH": "/opt/homebrew/bin"}, tmp_path).split(":")
    assert once.count("/opt/homebrew/bin") == 1


def test_a_tool_on_a_homebrew_style_path_is_found_with_no_PATH_at_all(tmp_path):
    """The real macOS case: Finder gives the launcher the system PATH, so Homebrew must be searched by location."""
    brew = tmp_path / "opt" / "homebrew" / "bin"
    _exe(brew / "7zz")
    monkey = {"macos": (str(brew),)}
    original, disc.GUI_PATH = disc.GUI_PATH, monkey
    try:
        found = disc.discover("sevenzip", system="macos", env={"PATH": ""}, home=tmp_path)
    finally:
        disc.GUI_PATH = original
    assert [Path(f.path) for f in found] == [brew / "7zz"]


def test_sevenzip_and_rife_are_looked_for_where_they_really_live_on_a_mac():
    spots = disc._locations("sevenzip", "macos", Path("/t"))
    assert "/opt/homebrew/bin/7zz" in spots and "/usr/local/bin/7z" in spots
    assert disc._locations("sevenzip", "linux", Path("/t")) != []
    rife = disc._locations("rife_engine", "macos", Path("/t"))
    assert any("~/tools/rife-ncnn-vulkan" in r for r in rife) and any("Downloads" in r for r in rife)


def test_a_shared_tools_folder_serves_every_machine(tmp_path):
    shared = tmp_path / "shared" / "tools"
    shared.mkdir(parents=True)
    (shared / "7zz").write_text("", encoding="utf-8")        # no exec bit: a Windows share often drops it
    (shared / "rife-v4.26").mkdir()
    found = disc.discover("sevenzip", system="macos", env={"PATH": ""}, home=tmp_path, extra_dirs=[shared])
    assert [f.how for f in found] == ["shared folder"] and Path(found[0].path) == shared / "7zz"
    models = disc.discover("rife_model_dir", system="macos", env={"PATH": ""}, home=tmp_path, extra_dirs=[shared])
    assert [Path(f.path) for f in models] == [shared / "rife-v4.26"]


def test_tools_dir_is_per_machine_never_the_app_folder(tmp_path):
    assert disc.tools_dir("windows", {"LOCALAPPDATA": str(tmp_path / "L")}, tmp_path) == tmp_path / "L" / "fstk" / "tools"
    assert "Application Support" in str(disc.tools_dir("macos", {}, tmp_path))
    assert disc.tools_dir("linux", {"XDG_DATA_HOME": str(tmp_path / "x")}, tmp_path) == tmp_path / "x" / "fstk" / "tools"


# ---------------------------------------------------------------- providers

def test_install_commands_per_manager_and_ffprobe_rides_with_ffmpeg():
    assert prov.install_command("ffmpeg", "winget")[:3] == ["winget", "install", "-e"]
    assert prov.install_command("ffprobe", "brew") == prov.install_command("ffmpeg", "brew")
    assert prov.install_command("ffmpeg", "dnf") is None            # needs RPM Fusion: guidance only
    assert prov.install_command("orc", "brew") is None              # pinned releases are never auto-installed
    assert prov.install_command("rife_engine", "apt") is None


def test_detect_manager_uses_the_given_path():
    which = lambda name, path=None: "/x/" + name if name == "brew" else None  # noqa: E731
    assert prov.detect_manager("macos", {"PATH": "p"}, which) == "brew"
    assert prov.detect_manager("windows", {"PATH": "p"}, which) is None


def test_homebrew_is_found_by_its_own_location_when_it_is_not_on_PATH():
    nothing = lambda name, path=None: None                                    # noqa: E731
    assert prov.find_manager("macos", {"PATH": ""}, nothing, lambda p: False) is None
    found = prov.find_manager("macos", {"PATH": ""}, nothing, lambda p: p == "/opt/homebrew/bin/brew")
    assert found == ("brew", "/opt/homebrew/bin/brew")
    assert prov.detect_manager("macos", {"PATH": ""}, lambda n, path=None: "/usr/local/bin/brew") == "brew"


def test_the_install_runs_the_manager_that_was_actually_found():
    assert prov.install_command("sevenzip", "brew", "/opt/homebrew/bin/brew") == \
        ["/opt/homebrew/bin/brew", "install", "sevenzip"]
    assert prov.install_command("ffmpeg", "apt", "/usr/bin/apt-get")[:2] == ["sudo", "/usr/bin/apt-get"]
    assert prov.install_command("ffmpeg", "brew") == ["brew", "install", "ffmpeg"]
    assert prov.install_command("orc", "brew", "/opt/homebrew/bin/brew") is None


def test_guidance_names_the_version_pin_and_the_mac_quarantine_step():
    text = prov.guidance("orc", "macos")
    assert "2.11.1" in text and "xattr" in text
    assert "libx265" in prov.guidance("ffmpeg", "linux")
    assert "never chosen for you" in prov.guidance("rife_engine", "windows")


def test_mac_guidance_says_how_to_install_7zip_and_to_keep_the_same_rife_model():
    assert "brew install sevenzip" in prov.guidance("sevenzip", "macos")
    assert "winget" in prov.guidance("sevenzip", "windows")
    rife = prov.guidance("rife_engine", "macos")
    assert "MoltenVK" in rife and "rife-v4.26" in rife and "shared folder" in rife


def test_flatpak_wrapper_runs_orc_cli_inside_the_flatpak(tmp_path):
    wrapper = prov.write_orc_flatpak_wrapper(tmp_path / "tools")
    text = wrapper.read_text(encoding="utf-8")
    assert prov.ORC_FLATPAK in text and "--command=orc-cli" in text and "--filesystem=host" in text
    assert wrapper.stat().st_mode & stat.S_IXUSR


def test_flatpak_probe_and_x265_probe_fail_safe():
    assert prov.flatpak_has_orc(lambda cmd: 0) is True
    assert prov.flatpak_has_orc(lambda cmd: 1) is False

    def missing(cmd):
        raise OSError("no flatpak")

    assert prov.flatpak_has_orc(missing) is False
    assert prov.ffmpeg_has_x265("ffmpeg", lambda cmd: " V..... libx265 HEVC") is True
    assert prov.ffmpeg_has_x265("ffmpeg", lambda cmd: " V..... libx264") is False
    assert prov.ffmpeg_has_x265("ffmpeg", missing) is None


# ---------------------------------------------------------------- console

def test_console_never_asks_or_installs_when_not_interactive():
    c = wiz.Console(interactive=False, ask_fn=lambda q: pytest.fail("asked"), out=lambda t: None)
    assert c.ask("x?", "d") == "d"
    assert c.confirm("ok?") is False
    assert wiz.Console(interactive=False, assume_yes=True, out=lambda t: None).confirm("ok?") is True


def test_console_default_and_eof():
    answers = iter(["", "n"])
    c = wiz.Console(ask_fn=lambda q: next(answers), out=lambda t: None)
    assert c.confirm("go?") is True
    assert c.confirm("go?") is False

    def eof(question):
        raise EOFError

    assert wiz.Console(ask_fn=eof, out=lambda t: None).ask("x?", "dflt") == "dflt"


# ---------------------------------------------------------------- wizard

def _fake_machine(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    for name in ("ffmpeg", "ffprobe"):
        _exe(bin_dir / name)
    env = {"PATH": str(bin_dir), "XDG_DATA_HOME": str(tmp_path / "data")}
    return env, bin_dir


def _doctor_ok(profile):
    return {"ok": True, "checks": [{"name": "x", "status": "PASS", "detail": ""}]}


def test_setup_creates_a_profile_with_the_tools_it_found_and_never_installs_when_check_only(tmp_path):
    env, _ = _fake_machine(tmp_path)
    lines: list[str] = []
    console = wiz.Console(interactive=False, out=lines.append)
    path = tmp_path / "profile" / "machine.json"
    result = wiz.run_setup(console, profile_path=path, system="linux", env=env, home=tmp_path,
                           run=lambda c: pytest.fail("ran " + " ".join(c)),
                           run_text=lambda c: " V..... libx265", install=False, doctor=_doctor_ok,
                           venv_bin=tmp_path / "nope")
    assert result.ok and result.created and path.is_file()
    prof = Profile.load(path)
    assert Path(prof.tool("ffmpeg")).name == "ffmpeg" and Path(prof.tool("ffprobe")).name == "ffprobe"
    assert any("[setup]" in n for n in prof.notes)


def test_setup_check_only_writes_nothing(tmp_path):
    env, _ = _fake_machine(tmp_path)
    path = tmp_path / "profile" / "machine.json"
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), profile_path=path, system="linux", env=env,
                  home=tmp_path, run_text=lambda c: "", check_only=True, doctor=_doctor_ok, venv_bin=tmp_path / "x")
    assert not path.exists()


def test_setup_keeps_a_valid_configured_tool_on_a_second_run(tmp_path):
    env, bin_dir = _fake_machine(tmp_path)
    path = tmp_path / "profile" / "machine.json"
    kw = {"profile_path": path, "system": "linux", "env": env, "home": tmp_path, "run_text": lambda c: " libx265",
          "install": False, "doctor": _doctor_ok, "venv_bin": tmp_path / "x"}
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), **kw)
    first = Profile.load(path).tool("ffmpeg")
    other = _exe(tmp_path / "other" / "ffmpeg")
    env["PATH"] = f"{other.parent}:{bin_dir}"
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), **kw)
    assert Profile.load(path).tool("ffmpeg") == first


def test_setup_offers_the_package_manager_install_and_only_after_confirmation(tmp_path):
    env = {"PATH": str(tmp_path / "pm"), "XDG_DATA_HOME": str(tmp_path / "data")}
    _exe(tmp_path / "pm" / "apt-get")
    ran: list[list[str]] = []

    def run(cmd):
        if cmd[0] == "flatpak":                      # the read-only "is Orc installed as a Flatpak" probe
            return 1
        ran.append(cmd)
        _exe(tmp_path / "pm" / "7z")
        return 0

    path = tmp_path / "p" / "machine.json"
    declined = wiz.Console(ask_fn=lambda q: "n", out=lambda t: None)
    wiz.run_setup(declined, profile_path=path, system="linux", env=env, home=tmp_path, run=run,
                  run_text=lambda c: " libx265", doctor=_doctor_ok, venv_bin=tmp_path / "x")
    assert ran == []
    accepted = wiz.Console(assume_yes=True, ask_fn=lambda q: "", out=lambda t: None)
    wiz.run_setup(accepted, profile_path=path, system="linux", env=env, home=tmp_path, run=run,
                  run_text=lambda c: " libx265", doctor=_doctor_ok, venv_bin=tmp_path / "x")
    # (7-Zip is the observed tool: a real ffmpeg in /usr/bin would be found without any install)
    assert [["sudo", str(tmp_path / "pm" / "apt-get"), "install", "-y", "p7zip-full"]] == ran


def test_setup_takes_a_tool_from_the_shared_folder_when_the_machine_has_none(tmp_path):
    shared = tmp_path / "shared"
    (shared / "tools").mkdir(parents=True)
    (shared / "tools" / "7zz").write_text("", encoding="utf-8")
    env, _ = _fake_machine(tmp_path)
    path = tmp_path / "p" / "machine.json"
    kw = {"system": "linux", "env": env, "home": tmp_path, "run_text": lambda c: " libx265", "install": False,
          "doctor": _doctor_ok, "venv_bin": tmp_path / "x"}
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), profile_path=path, **kw)
    prof = Profile.load(path)
    assert prof.tools.get("sevenzip") is None           # not shared with this profile yet
    prof.shared_root = shared
    prof.save(path)
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), profile_path=path, **kw)
    assert Path(Profile.load(path).tool("sevenzip")) == shared / "tools" / "7zz"


def test_user_path_for_a_mac_app_bundle_becomes_its_cli(tmp_path):
    cli = _exe(tmp_path / "orc-gui.app" / "Contents" / "MacOS" / "orc-cli")
    assert wiz._resolve_user_path("orc", f'"{tmp_path / "orc-gui.app"}"') == str(cli)
    assert wiz._resolve_user_path("orc", str(tmp_path / "missing")) is None


# ---------------------------------------------------------------- launcher core

@pytest.fixture
def launch(monkeypatch):
    spec = importlib.util.spec_from_file_location("fstk_launch_under_test", ROOT / "tools" / "fstk_launch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_launcher_pins_the_locked_plugin_set(launch):
    reqs = launch.requirements()
    pins = {n: i["version"] for n, i in launch.lock()["packages"].items()}
    assert pins["vapoursynth-tivtc"] and f"vapoursynth-tivtc=={pins['vapoursynth-tivtc']}" in reqs
    assert f"vsjetpack=={pins['vsjetpack']}" in reqs
    assert not [r for r in reqs if r.startswith(("vapoursynth>=", "vsjetpack>="))]
    assert any(r.startswith("PySide6") for r in reqs)           # extras are installed too


def test_every_locked_package_is_declared_by_pyproject(launch):
    import tomllib
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {launch._name(r) for r in meta["optional-dependencies"]["media"]}
    assert set(launch.lock()["packages"]) == declared


def test_vendored_macos_wheels_match_their_recorded_hashes(launch):
    lock = launch.lock()
    assert lock["macos_min_version"] >= 15 and len(lock["packages"]) == 10
    for name, info in lock["packages"].items():
        data = (launch.LOCK_FILE.parent / info["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == info["sha256"], name
        assert len(data) == info["size"], name
        assert info["upstream"].startswith("https://github.com/"), name
        assert "arm64" in info["file"] or info["file"].endswith("-any.whl"), name


def test_vendored_wheels_are_used_only_on_apple_silicon_and_only_when_verified(launch, monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "system", lambda: "linux")
    assert launch.vendored_wheels() is None
    monkeypatch.setattr(launch, "system", lambda: "macos")
    monkeypatch.setattr(launch.platform, "machine", lambda: "arm64")
    wheels = launch.vendored_wheels()
    assert wheels and len(wheels) == 10 and all(w.is_file() for w in wheels)
    # a tampered copy is refused (PyPI is used instead), never installed
    fake = tmp_path / "vendor" / "vapoursynth-plugins"
    (fake / "macos-arm64").mkdir(parents=True)
    lock = json.loads(launch.LOCK_FILE.read_text(encoding="utf-8"))
    first = next(iter(lock["packages"].values()))
    (fake / first["file"]).write_bytes(b"tampered")
    (fake / "LOCK.json").write_text(json.dumps({"packages": {"x": first}}), encoding="utf-8")
    monkeypatch.setattr(launch, "LOCK_FILE", fake / "LOCK.json")
    assert launch.vendored_wheels() is None


def test_launcher_keeps_everything_outside_the_app_folder(launch, monkeypatch, tmp_path):
    monkeypatch.setattr(launch.Path, "home", classmethod(lambda cls: tmp_path))
    for system in ("windows", "macos", "linux"):
        monkeypatch.setattr(launch, "system", lambda s=system: s)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert tmp_path in launch.venv_dir().parents
        assert launch.APP not in launch.venv_dir().parents
    env = launch.child_env()
    assert env["PYTHONPATH"].split(launch.os.pathsep)[0] == str(launch.APP / "src")
    assert str(launch.APP) not in env["PYTHONPYCACHEPREFIX"]


def test_launcher_rejects_unsupported_python(launch, monkeypatch, capsys):
    monkeypatch.setattr(launch, "python_ok", lambda: False)
    assert launch.main([]) == 3
    assert "3.12 to 3.15" in capsys.readouterr().out


def test_each_platform_has_a_launcher_that_reaches_the_core():
    cmd = (ROOT / "FromStunToKill.cmd").read_text(encoding="utf-8")
    assert "tools\\launch.ps1" in cmd
    ps1 = (ROOT / "tools" / "launch.ps1").read_text(encoding="utf-8")
    assert "fstk_launch.py" in ps1 and "SHA256" in ps1 and "3.12" in ps1
    for name in ("FromStunToKill.command", "FromStunToKill.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh") and "tools/launch.sh" in text
    sh = (ROOT / "tools" / "launch.sh").read_text(encoding="utf-8")
    assert "fstk_launch.py" in sh and "sha256" in sh


# ---------------------------------------------------------------- first Mac run (debug bundle 2026-11-23)

def _profile_with_vspipe(tmp_path: Path, venv_bin: Path) -> Path:
    env, _ = _fake_machine(tmp_path)
    _exe(venv_bin / "vspipe")
    _exe(venv_bin / "vapoursynth")
    path = tmp_path / "p" / "machine.json"
    kw = {"system": "linux", "env": env, "home": tmp_path, "run_text": lambda c: " libx265", "install": False,
          "doctor": _doctor_ok, "venv_bin": venv_bin}
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), profile_path=path, **kw)
    assert Path(Profile.load(path).tool("vspipe")).parent == venv_bin
    return path


def test_setup_runs_vapoursynth_config_when_vspipe_exits_non_zero(tmp_path):
    """The Mac's vspipe exited 1 ("Python executable and library path couldn't be determined") until
    `vapoursynth config` had run for that environment."""
    venv_bin = tmp_path / "venv" / "bin"
    path = _profile_with_vspipe(tmp_path, venv_bin)
    configured = {"done": False}
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        if cmd[-1] == "config":
            configured["done"] = True
            return 0
        if cmd[-1] == "--version" and Path(cmd[0]).name == "vspipe":
            return 0 if configured["done"] else 1
        return 1                                   # the flatpak probe etc.

    lines: list[str] = []
    res = wiz.run_setup(wiz.Console(interactive=False, out=lines.append), profile_path=path, system="linux",
                        env={"PATH": ""}, home=tmp_path, run=run, run_text=lambda c: " libx265", install=False,
                        doctor=_doctor_ok, venv_bin=venv_bin)
    assert [str(venv_bin / "vapoursynth"), "config"] in calls
    assert "vapoursynth config" in res.changes
    assert not [x for x in res.problems if "vspipe" in x]          # (Orc is absent in this fake machine)
    assert any("vspipe now runs" in ln for ln in lines)


def test_setup_leaves_a_working_vspipe_alone_and_check_only_never_configures(tmp_path):
    venv_bin = tmp_path / "venv" / "bin"
    path = _profile_with_vspipe(tmp_path, venv_bin)
    kw = {"profile_path": path, "system": "linux", "env": {"PATH": ""}, "home": tmp_path,
          "run_text": lambda c: " libx265", "doctor": _doctor_ok, "venv_bin": venv_bin}
    seen: list[list[str]] = []
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None),
                  run=lambda c: seen.append(c) or 0, install=False, **kw)
    assert not [c for c in seen if c[-1] == "config"]
    seen.clear()
    res = wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None),
                        run=lambda c: seen.append(c) or 1, install=False, check_only=True, **kw)
    assert not [c for c in seen if c[-1] == "config"]
    assert any("not configured" in p for p in res.problems)


def test_an_existing_profile_is_asked_for_a_missing_downloads_folder(tmp_path):
    """The Mac's profile had no sources.downloads_root, so the first intake failed at the download step."""
    env, _ = _fake_machine(tmp_path)
    path = tmp_path / "p" / "machine.json"
    kw = {"system": "linux", "env": env, "home": tmp_path, "run_text": lambda c: " libx265", "install": False,
          "doctor": _doctor_ok, "venv_bin": tmp_path / "x"}
    wiz.run_setup(wiz.Console(interactive=False, out=lambda t: None), profile_path=path, **kw)
    assert not Profile.load(path).sources.get("downloads_root")
    captures = tmp_path / "captures"
    captures.mkdir()
    asked: list[str] = []

    def ask(question):
        asked.append(question)
        return str(captures) if "capture folders" in question else ""

    wiz.run_setup(wiz.Console(ask_fn=ask, out=lambda t: None), profile_path=path, **kw)
    assert Profile.load(path).sources["downloads_root"] == str(captures)
    assert not [q for q in asked if "Shared workspace" in q]       # only the unset roots, not the whole tour
    asked.clear()
    wiz.run_setup(wiz.Console(ask_fn=ask, out=lambda t: None), profile_path=path, **kw)
    assert not [q for q in asked if "capture folders" in q]       # once set, it is not asked again


def test_the_download_error_says_where_to_fix_it(tmp_path):
    from fstk.config.profile import ConfigError, EpisodeId

    prof = Profile.from_dict({"schema": "fstk-machine-profile-v1", "machine": "m", "os": "linux",
                              "data_root": str(tmp_path / "d"), "media_root": str(tmp_path / "m")})
    with pytest.raises(ConfigError, match="Settings → Folders"):
        prof.downloads_dir(EpisodeId.parse("DS9:S02E02"))


def test_doctor_fails_a_vspipe_that_prints_an_error_and_exits_non_zero(tmp_path, monkeypatch):
    from fstk.tools import doctor

    fake = _exe(tmp_path / "bin" / "vspipe")

    class Res:
        returncode = 1
        stdout = b"Failed to initialize VSScript. Run `vapoursynth config` to set it\n"
        tail = ()

    monkeypatch.setattr(doctor.process, "run", lambda *a, **k: Res())
    ok, line = doctor._first_line([str(fake), "--version"], strict=True)
    assert ok is False and "vapoursynth config" in line
    assert doctor._first_line([str(fake), "--version"])[0] is True          # other tools keep the lenient rule


def test_doctor_warns_when_state_lives_inside_the_shared_app_folder(tmp_path, monkeypatch):
    from fstk.tools import doctor

    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setenv("FSTK_APP_ROOT", str(app))
    prof = Profile.from_dict({"schema": "fstk-machine-profile-v1", "machine": "m", "os": "linux",
                              "data_root": str(app / "data"), "media_root": str(tmp_path / "elsewhere")})
    rows = {c["name"]: c for c in doctor.run_doctor(prof)["checks"]}
    assert rows["root:data_root:location"]["status"] == "WARN" and "app folder" in rows["root:data_root:location"]["detail"]
    assert "root:media_root:location" not in rows


def test_the_launcher_configures_vapoursynth_and_re_runs_it_for_existing_environments(launch):
    assert launch.stamp()["vsconfig"] >= 1
    assert callable(launch.configure_vapoursynth)
