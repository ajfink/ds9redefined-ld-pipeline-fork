"""Download and install Decode-Orc when the machine has none.

Orc is the one tool the pipeline pins (its version changes the pixels and the audio), so this installs the
*pinned* release (``ORC_VERSION``) and nothing else without saying so. The release list comes from GitHub at the
moment of the install, so nothing about a release (file names, checksums) is guessed or stored here.

What each system gets (upstream ships exactly these three):

* Windows: ``Decode-Orc-<version>-windows-x64.msi`` (unsigned; needs the Microsoft Visual C++ runtime, which is
  installed first when it is missing). Windows asks for the administrator's approval once (the MSI installs for
  all users, into ``Program Files\\Decode-Orc``).
* macOS (Apple silicon): ``Decode-Orc-<version>-macos-arm64.dmg``; ``orc-gui.app`` is copied to
  ``~/Applications`` (no password) and its quarantine mark removed, because the app is only ad-hoc signed.
* Linux: ``Decode-Orc-<version>-linux.flatpak``, installed for this user; setup then writes the ``orc-cli`` wrapper.

Everything that touches the machine is a parameter (``fetch_json``, ``download``, ``run``), so the logic is
tested without a network and without installing anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fstk.install import providers

#: GitHub list of releases (newest first). The documentation site links this repository.
RELEASES_API = "https://api.github.com/repos/decode-orc/decode-orc/releases?per_page=30"
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
#: ``msiexec`` results that mean "installed" (3010 = installed, restart needed to finish)
MSI_OK = (0, 3010)

Fetch = Callable[[str], object]
Download = Callable[[str, Path], None]


class OrcInstallError(Exception):
    """Reported to the owner in plain words; setup then falls back to the manual instructions."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int | None = None
    sha256: str | None = None
    version: str = ""
    tag: str = ""


def asset_pattern(system: str, machine: str = "") -> re.Pattern[str] | None:
    """The upstream file name for this system, with the version captured (``None``: no build for it)."""
    if system == "windows":
        return re.compile(r"^Decode-Orc-(?P<v>[\w.+-]+?)-windows-x64\.msi$", re.I)
    if system == "macos":
        if machine.lower() not in ("arm64", "aarch64"):
            return None                              # the DMG is built for Apple silicon only
        return re.compile(r"^Decode-Orc-(?P<v>[\w.+-]+?)-macos-arm64\.dmg$", re.I)
    if system == "linux":
        return re.compile(r"^Decode-Orc-(?P<v>[\w.+-]+?)-linux\.flatpak$", re.I)
    return None


def pick_asset(releases: object, system: str, machine: str = "", *, version: str) -> tuple[Asset | None, Asset | None]:
    """``(pinned, newest)``: this system's file in the release of ``version``, and in the newest release.

    Draft and pre-releases are skipped. Either may be ``None``.
    """
    pattern = asset_pattern(system, machine)
    if pattern is None or not isinstance(releases, list):
        return None, None
    pinned = newest = None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        for a in rel.get("assets") or []:
            m = pattern.match(str(a.get("name", "")))
            if not m or not a.get("browser_download_url"):
                continue
            digest = str(a.get("digest") or "")
            asset = Asset(name=a["name"], url=a["browser_download_url"], size=a.get("size"),
                          sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
                          version=m.group("v").lstrip("vV"), tag=str(rel.get("tag_name", "")))
            newest = newest or asset
            if asset.version == version and pinned is None:
                pinned = asset
    return pinned, newest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(asset: Asset, path: Path) -> str:
    """Check the download against the size and checksum GitHub lists for it; returns the SHA-256 (for the log)."""
    if asset.size is not None and path.stat().st_size != asset.size:
        raise OrcInstallError(f"the download is {path.stat().st_size} bytes but GitHub lists {asset.size}; "
                              "it was cut off - try again")
    digest = sha256_of(path)
    if asset.sha256 and digest != asset.sha256:
        raise OrcInstallError(f"the downloaded file's checksum ({digest}) does not match GitHub's ({asset.sha256})")
    return digest


# --------------------------------------------------------------------- network (real implementations)

def _context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _request(url: str, env: Mapping[str, str]) -> urllib.request.Request:
    headers = {"User-Agent": "FromStunToKill-setup", "Accept": "application/vnd.github+json, */*"}
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def fetch_json(url: str, env: Mapping[str, str] | None = None) -> object:
    env = os.environ if env is None else env
    try:
        with urllib.request.urlopen(_request(url, env), timeout=30, context=_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise OrcInstallError(f"could not read the release list from GitHub ({exc})") from exc


def download(url: str, dest: Path, env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(_request(url, env), timeout=60, context=_context()) as resp, part.open("wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        part.replace(dest)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise OrcInstallError(f"the download failed ({exc})") from exc


# --------------------------------------------------------------------- per-system installs

def _vcruntime_present(env: Mapping[str, str]) -> bool:
    root = Path(env.get("SystemRoot") or r"C:\Windows") / "System32"
    return all((root / n).is_file() for n in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"))


def install_windows(asset_file: Path, *, env: Mapping[str, str], work: Path, say: Callable[[str], None],
                    run: Callable[[list[str]], int], fetch_file: Download) -> None:
    if not _vcruntime_present(env):
        say("  Decode-Orc needs the Microsoft Visual C++ runtime, which this PC lacks; installing it first...")
        redist = work / "vc_redist.x64.exe"
        fetch_file(VC_REDIST_URL, redist)
        rc = run([str(redist), "/install", "/passive", "/norestart"])
        if rc not in (0, 1638, 3010):                # 1638: a newer one is already installed
            raise OrcInstallError(f"the Visual C++ runtime installer exited with code {rc}")
    say("  Installing Decode-Orc (Windows will ask for permission once)...")
    rc = run(["msiexec", "/i", str(asset_file), "/passive", "/norestart"])
    if rc not in MSI_OK:
        raise OrcInstallError({1602: "the installation was cancelled", 1603: "the Windows Installer failed"}.get(
            rc, f"the Windows Installer exited with code {rc}"))
    if rc == 3010:
        say("  Windows says a restart is needed to finish; Orc may already work.")


def install_macos(asset_file: Path, *, home: Path, work: Path, say: Callable[[str], None],
                  run: Callable[[list[str]], int]) -> None:
    mount = work / "dmg"
    mount.mkdir(parents=True, exist_ok=True)
    say("  Opening the disk image...")
    if run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", str(mount),
            str(asset_file)]) != 0:
        raise OrcInstallError("the disk image could not be opened")
    try:
        app = next(iter(sorted(mount.glob("*.app"))), None)
        if app is None or app.name != "orc-gui.app":
            raise OrcInstallError("the disk image does not contain orc-gui.app")
        target_dir = home / "Applications"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "orc-gui.app"
        say(f"  Copying orc-gui.app to {target_dir} ...")
        if target.exists():
            shutil.rmtree(target)
        if run(["ditto", str(app), str(target)]) != 0:
            raise OrcInstallError("copying orc-gui.app failed")
    finally:
        run(["hdiutil", "detach", str(mount), "-quiet"])
    # the app is ad-hoc signed, not notarised: without this Gatekeeper blocks the first run of orc-cli
    run(["xattr", "-dr", "com.apple.quarantine", str(target)])


def install_linux(asset_file: Path, *, env: Mapping[str, str], home: Path, say: Callable[[str], None],
                  run: Callable[[list[str]], int]) -> None:
    if not shutil.which("flatpak"):
        raise OrcInstallError("Flatpak is not installed (Debian/Ubuntu: sudo apt install flatpak; "
                              "Fedora: sudo dnf install flatpak; Arch: sudo pacman -S flatpak)")
    say("  Installing the Flatpak for this user (its KDE runtime is fetched from Flathub, which can be large)...")
    run(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO])
    if run(["flatpak", "install", "--user", "-y", str(asset_file)]) != 0:
        raise OrcInstallError("`flatpak install` failed (see its message above)")


# --------------------------------------------------------------------- the whole thing

def install_orc(console, *, system: str, env: Mapping[str, str], home: Path, run: Callable[[list[str]], int],
                machine: str | None = None, fetch: Fetch | None = None, fetch_file: Download | None = None,
                version: str = providers.ORC_VERSION, work_dir: Path | None = None) -> bool:
    """Offer, download, verify and install Decode-Orc. True when an install ran to the end.

    ``console`` supplies ``say`` and ``confirm``. Any failure is reported in words and returns False: the caller
    then shows the manual instructions, so setup never stops here.
    """
    say = console.say
    machine = platform.machine() if machine is None else machine
    if asset_pattern(system, machine) is None:
        say("  " + providers.guidance("orc", system))
        return False
    fetch = fetch or (lambda url: fetch_json(url, env))
    fetch_file = fetch_file or (lambda url, dest: download(url, dest, env))
    try:
        pinned, newest = pick_asset(fetch(RELEASES_API), system, machine, version=version)
        asset = pinned
        if asset is None:
            if newest is None:
                raise OrcInstallError("GitHub lists no Decode-Orc file for this system")
            say(f"  Decode-Orc {version} (the version your episodes were built with) is not on GitHub's list; "
                f"the newest is {newest.version}.")
            say("  A different Orc version can change the pixels and audio, so install it only if you mean to.")
            if not console.confirm(f"  Install Decode-Orc {newest.version} instead?", default=False):
                say("  " + providers.guidance("orc", system))
                return False
            asset = newest
        say(f"  Downloading {asset.name}"
            f"{f' ({asset.size / 1_048_576:.0f} MB)' if asset.size else ''} from GitHub...")
        base = work_dir or Path(tempfile.mkdtemp(prefix="fstk-orc-"))
        base.mkdir(parents=True, exist_ok=True)
        try:
            file = base / asset.name
            fetch_file(asset.url, file)
            digest = verify(asset, file)
            say(f"  Downloaded; SHA-256 {digest}"
                + ("" if asset.sha256 else " (GitHub lists none to compare with)"))
            if system == "windows":
                say("  Note: this installer is unsigned, which is expected for Decode-Orc.")
                install_windows(file, env=env, work=base, say=say, run=run, fetch_file=fetch_file)
            elif system == "macos":
                install_macos(file, home=home, work=base, say=say, run=run)
            else:
                install_linux(file, env=env, home=home, say=say, run=run)
        finally:
            if work_dir is None:
                shutil.rmtree(base, ignore_errors=True)
        say(f"  Decode-Orc {asset.version} installed.")
        return True
    except OrcInstallError as exc:
        say(f"  Decode-Orc could not be installed automatically: {exc}.")
        say("  " + providers.guidance("orc", system))
        return False
    except OSError as exc:
        say(f"  Decode-Orc could not be installed automatically: {exc}.")
        say("  " + providers.guidance("orc", system))
        return False
