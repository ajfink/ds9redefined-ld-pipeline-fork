"""Read-only readiness report. Installs nothing, creates nothing, writes nothing.

Replaces v38 ``doctor.py`` + ``preflight.ps1`` + ``preflight_mac.py`` with one
cross-platform implementation. Orc's version cannot be read from ``orc-cli --version``
(unsupported by the tested CLI, PORTING §5), so it is reported as "verify from release
metadata" rather than guessed.
"""
from __future__ import annotations

import importlib
import platform
import shutil
import sys
from typing import Any

from fstk.config.profile import KNOWN_TOOLS
from fstk.core.errors import ConfigError
from fstk.tools import process

GIB = 1024 ** 3
PY_MIN = (3, 12)
PACKAGES = {"numpy": True, "cv2": True, "PIL": True, "PySide6": False, "vapoursynth": False}
VERSION_ARGS = {"ffmpeg": ["-version"], "ffprobe": ["-version"], "vspipe": ["--version"],
                "sevenzip": [], "curl": ["--version"]}


def _first_line(args: list[str]) -> tuple[bool, str]:
    try:
        res = process.run(args, capture_stdout=True, timeout=30)
    except Exception as exc:
        return False, str(exc)
    text = (res.stdout or b"").decode("utf-8", errors="replace").strip() or "\n".join(res.tail)
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return res.returncode == 0 or bool(line), line[:200]


def _shared_checks(profile, add) -> None:
    """Shared multi-machine workspace (ADR 0002), read-only: nothing is created here."""
    from pathlib import Path

    from fstk.core.fsio import read_json
    from fstk.workspace.device import identity_path

    if not profile.shared_root:
        add("shared", "PASS", "off (no shared_root): this machine works alone")
        return
    root = Path(profile.shared_root)
    if not root.is_dir():
        add("shared", "WARN", f"{root} not reachable; local work continues, nothing is published")
        return
    ident = identity_path(profile)
    me = ""
    if ident.is_file():
        doc = read_json(ident)
        from fstk.workspace.device import DeviceIdentity

        me = DeviceIdentity(doc.get("machine", ""), doc.get("instance_id", "")).name
    devices = root / "devices"
    peers = sorted(p.name for p in devices.iterdir() if p.is_dir() and p.name != me) if devices.is_dir() else []
    add("shared", "PASS", f"{root} — this device: {me or 'not joined yet (joins on first use)'}; "
                          f"other devices: {', '.join(peers) or 'none'}")


def run_doctor(profile=None) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    ok_py = sys.version_info[:2] >= PY_MIN
    add("python", "PASS" if ok_py else "FAIL", f"{platform.python_version()} ({sys.executable})")
    for mod, required in PACKAGES.items():
        try:
            m = importlib.import_module(mod)
            add(f"package:{mod}", "PASS", str(getattr(m, "__version__", getattr(m, "core", "") or "installed")))
        except Exception as exc:
            add(f"package:{mod}", "FAIL" if required else "WARN",
                f"not importable ({exc.__class__.__name__}){'' if required else ' — optional'}")
    if profile is not None:
        add("profile", "PASS", f"{profile.path} ({profile.machine}, {profile.os})")
        for name, spec in KNOWN_TOOLS.items():
            try:
                path = profile.tool(name)
            except ConfigError as exc:
                add(f"tool:{name}", "FAIL" if spec["required"] else "WARN", str(exc))
                continue
            if name == "orc":
                add("tool:orc", "PASS", f"{path} — version: verify from release metadata (no --version)")
            elif spec["kind"] == "dir":
                add(f"tool:{name}", "PASS", str(path))
            elif VERSION_ARGS.get(name):
                ran, line = _first_line([str(path), *VERSION_ARGS[name]])
                add(f"tool:{name}", "PASS" if ran else "FAIL", f"{path} — {line}")
            else:
                add(f"tool:{name}", "PASS", str(path))
        for label, root in (("data_root", profile.data_root), ("media_root", profile.media_root)):
            if not root.exists():
                add(f"root:{label}", "WARN", f"{root} does not exist yet (created on first use)")
                continue
            free = shutil.disk_usage(root).free
            reserve = profile.limits.min_free_gb * GIB
            add(f"root:{label}", "PASS" if free >= reserve else "FAIL",
                f"{root} free={free / GIB:.1f} GiB reserve={profile.limits.min_free_gb:g} GiB")
        for label, value in profile.sources.items():
            from pathlib import Path

            add(f"source:{label}", "PASS" if Path(value).exists() else "WARN", value)
        for label, value in profile.volumes.items():
            from pathlib import Path

            add(f"volume:{label}", "PASS" if Path(value).exists() else "WARN",
                f"{value}{'' if Path(value).exists() else ' — not mounted (adopted episodes that use it will report missing media)'}")
        _shared_checks(profile, add)
    ok = not any(c["status"] == "FAIL" for c in checks)
    return {"ok": ok, "platform": f"{platform.system()} {platform.machine()}", "checks": checks}
