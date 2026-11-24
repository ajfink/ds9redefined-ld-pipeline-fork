"""Download and install the RIFE engine (rife-ncnn-vulkan) when the machine has none.

The engine comes from nihui's release (one zip per system, models included). It is unpacked into fstk's own
per-machine tools folder, never into the app folder. The standard model (``providers.RIFE_MODEL``, rife-v4.26) is not in
the public zip (which stops at 4.6), so it is taken from next to the engine or the shared folder, and another
version is used only after asking.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

from fstk.install import providers
from fstk.install.orc import Asset, Download, Fetch, OrcInstallError, download, fetch_json, sha256_of, verify

#: the standard model in ncnn format, from TNTwise's rife-ncnn-vulkan fork at one fixed commit (files are verified
#: against these hashes; nothing here follows a moving branch). It is the converted Practical-RIFE 4.26.
MODEL_COMMIT = "13338e38debe2e400b3eeecf6792312d01a692f9"
MODEL_URL = "https://raw.githubusercontent.com/TNTwise/rife-ncnn-vulkan/{commit}/models/{model}/{name}"
MODEL_FILES = {
    "flownet.param": ("79f16c28903f93f8308f0c4c947f8c7e0c17d99a57b85473e5f298dd578d137b", 67885),
    "flownet.bin": ("94d58e30b75d7c7609cfa6f3bdad524deddd14f5f75e85c36d2f827ef5c64731", 11360684),
}
RELEASES_API = "https://api.github.com/repos/nihui/rife-ncnn-vulkan/releases?per_page=10"
_ASSET = {"windows": "windows", "macos": "macos", "linux": "ubuntu"}
FOLDER = "rife-ncnn-vulkan"


def pick_asset(releases: object, system: str) -> Asset | None:
    """The newest release's zip for this system, or ``None``."""
    word = _ASSET.get(system)
    if word is None or not isinstance(releases, list):
        return None
    pattern = re.compile(rf"^rife-ncnn-vulkan-(?P<v>\d+)-{word}\.zip$", re.I)
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        for a in rel.get("assets") or []:
            m = pattern.match(str(a.get("name", "")))
            if m and a.get("browser_download_url"):
                digest = str(a.get("digest") or "")
                return Asset(a["name"], a["browser_download_url"], a.get("size"),
                             digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
                             m.group("v"), str(rel.get("tag_name", "")))
    return None


def extract(archive: Path, dest: Path, system: str) -> Path:
    """Unpack ``archive`` into ``dest`` (replacing it), dropping the zip's single top folder; returns the engine."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n and not n.startswith("__MACOSX")]
        tops = {n.split("/", 1)[0] for n in names}
        strip = f"{next(iter(tops))}/" if len(tops) == 1 and all("/" in n for n in names) else ""
        staging = dest.with_name(dest.name + ".new")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        root = staging.resolve()
        for info in z.infolist():
            if info.filename.startswith("__MACOSX") or info.is_dir():
                continue
            rel = info.filename[len(strip):] if strip and info.filename.startswith(strip) else info.filename
            target = (staging / rel).resolve()
            if not rel or root not in target.parents:
                raise OrcInstallError(f"the archive holds an unsafe path ({info.filename})")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    exe = "rife-ncnn-vulkan.exe" if system == "windows" else "rife-ncnn-vulkan"
    engine = staging / exe
    if not engine.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise OrcInstallError(f"the archive does not contain {exe}")
    if system != "windows":
        engine.chmod(engine.stat().st_mode | 0o111)           # zip does not keep the executable bit reliably
    shutil.rmtree(dest, ignore_errors=True)
    staging.replace(dest)
    return dest / exe


def install_rife(console, *, system: str, env: Mapping[str, str], home: Path, dest: Path,
                 fetch: Fetch | None = None, fetch_file: Download | None = None,
                 work_dir: Path | None = None) -> bool:
    """Download, verify and unpack the engine into ``dest``. True when it is in place; failures are only words."""
    say = console.say
    fetch = fetch or (lambda url: fetch_json(url, env))
    fetch_file = fetch_file or (lambda url, to: download(url, to, env))
    base = work_dir or Path(tempfile.mkdtemp(prefix="fstk-rife-"))
    try:
        asset = pick_asset(fetch(RELEASES_API), system)
        if asset is None:
            raise OrcInstallError("GitHub lists no RIFE download for this system")
        say(f"  Downloading {asset.name}{f' ({asset.size / 1_048_576:.0f} MB)' if asset.size else ''} from GitHub...")
        base.mkdir(parents=True, exist_ok=True)
        file = base / asset.name
        fetch_file(asset.url, file)
        digest = verify(asset, file)
        say(f"  Downloaded; SHA-256 {digest}" + ("" if asset.sha256 else " (GitHub lists none to compare with)"))
        engine = extract(file, dest, system)
        say(f"  RIFE engine installed: {engine}")
        return True
    except (OrcInstallError, OSError, zipfile.BadZipFile) as exc:
        say(f"  RIFE could not be installed automatically: {exc}.")
        say("  " + providers.guidance("rife_engine", system))
        return False
    finally:
        if work_dir is None:
            shutil.rmtree(base, ignore_errors=True)


def install_model(console, *, engine_dir: Path, env: Mapping[str, str], fetch_file: Download | None = None,
                  model: str = providers.RIFE_MODEL, files: Mapping[str, tuple[str, int]] | None = None,
                  commit: str = MODEL_COMMIT) -> bool:
    """Download the standard RIFE model next to the engine, checking each file's size and SHA-256.

    The folder appears only when both files are complete and verified. Any failure is words, never a half model.
    """
    say = console.say
    files = MODEL_FILES if files is None else files
    fetch_file = fetch_file or (lambda url, to: download(url, to, env))
    target = engine_dir / model
    staging = engine_dir / f".{model}.new"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        say(f"  Downloading the standard RIFE model {model} (about {sum(v[1] for v in files.values()) / 1_048_576:.0f} MB)...")
        for name, (sha, size) in files.items():
            part = staging / name
            fetch_file(MODEL_URL.format(commit=commit, model=model, name=name), part)
            if part.stat().st_size != size or sha256_of(part) != sha:
                raise OrcInstallError(f"{name} did not match the expected size and checksum")
        shutil.rmtree(target, ignore_errors=True)
        staging.replace(target)
        say(f"  {model} installed: {target}")
        say("  (converted RIFE 4.26 in ncnn format from TNTwise/rife-ncnn-vulkan; if your other machines use a "
            "different conversion, copy that folder over this one so every machine matches)")
        return True
    except (OrcInstallError, OSError) as exc:
        say(f"  The RIFE model could not be installed automatically: {exc}.")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)
