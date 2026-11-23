"""Debug bundle: one zip with everything needed to diagnose a problem, and nothing that is media or a secret.

Contents: the application logs and crash log, an environment report (``fstk doctor`` plus package versions and free
disk space), the machine profile, the space ledger and timings, and for each chosen episode its manifest, job plans and
states, tool-version records and every step's tool log (long logs are cut to their last part). Text files are
redacted: the user's home folder becomes ``~`` and the user name ``<user>``. Media files are never included.
"""
from __future__ import annotations

import getpass
import json
import platform
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from fstk.core.applog import log_dir

TEXT_SUFFIXES = {".json", ".log", ".txt", ".md", ".csv", ".py", ".vpy", ".orcprj", ".yaml", ".yml", ".tsv", ".lock", ".jsonl"}
MAX_LOG_BYTES = 2_000_000
MAX_SMALL_BYTES = 5_000_000


def redactor():
    home = str(Path.home())
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    variants = {home, home.replace("\\", "/"), home.replace("/", "\\")}

    def redact(text: str) -> str:
        for v in sorted(variants, key=len, reverse=True):
            if v:
                text = text.replace(v, "~")
        if user and len(user) > 2:
            text = text.replace(user, "<user>")
        return text

    return redact


def _read_tail(path: Path, limit: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > limit:
            stream.seek(size - limit)
        raw = stream.read()
    text = raw.decode("utf-8", errors="replace")
    return (f"[... cut: showing the last {limit} of {size} bytes ...]\n" + text) if size > limit else text


def _environment(profile) -> dict[str, Any]:
    env: dict[str, Any] = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                           "machine": platform.machine(), "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        from fstk.tools.doctor import run_doctor

        env["doctor"] = run_doctor(profile)
    except Exception as exc:
        env["doctor_error"] = repr(exc)
    try:
        from importlib import metadata

        from fstk.workflow.engine import Engine  # noqa: F401

        env["fstk_version"] = metadata.version("fstk") if _has_dist("fstk") else "dev"
    except Exception:
        pass
    disks = {}
    if profile is not None:
        for label, p in (("data_root", profile.data_root), ("media_root", profile.media_root),
                         ("downloads_root", profile.sources.get("downloads_root"))):
            if p:
                try:
                    u = shutil.disk_usage(_existing(Path(p)))
                    disks[label] = {"path": str(p), "free_gib": round(u.free / 1024 ** 3, 1),
                                    "total_gib": round(u.total / 1024 ** 3, 1)}
                except OSError as exc:
                    disks[label] = {"path": str(p), "error": str(exc)}
    env["disks"] = disks
    try:
        from fstk.ui.localfolder import running_elevated

        env["running_as_administrator"] = running_elevated()
    except Exception:
        pass
    return env


def _has_dist(name: str) -> bool:
    from importlib import metadata

    try:
        metadata.version(name)
        return True
    except metadata.PackageNotFoundError:
        return False


def _existing(p: Path) -> Path:
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def episode_dirs(profile) -> list[Path]:
    root = profile.data_root / "episodes"
    return sorted(c for s in root.iterdir() if s.is_dir() for c in s.iterdir() if c.is_dir()) if root.is_dir() else []


def build_bundle(profile, out: str | Path, *, episodes: list[str] | None = None, logs_dir: Path | None = None) -> dict[str, Any]:
    """Write the zip. ``episodes`` are canonical ids (``DS9:S01E20``); None = every episode with saved state."""
    redact = redactor()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    listing: list[str] = []
    skipped: list[str] = []

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def add_text(arc: str, text: str) -> None:
            z.writestr(arc, redact(text))
            listing.append(arc)

        def add_file(arc: str, path: Path, limit: int) -> None:
            if path.suffix.lower() not in TEXT_SUFFIXES and path.suffix != "":
                skipped.append(f"{arc} (not a text file)")
                return
            try:
                add_text(arc, _read_tail(path, limit))
            except OSError as exc:
                skipped.append(f"{arc} ({exc})")

        add_text("environment.json", json.dumps(_environment(profile), indent=2, default=str))
        if profile is not None:
            add_text("profile.json", json.dumps(profile.to_dict(), indent=2, default=str))
            ledger = (profile.path.parent if profile.path else profile.data_root) / "space-ledger.json"
            for extra in (ledger, profile.data_root / "timings.json"):
                if extra.is_file():
                    add_file(f"state/{extra.name}", extra, MAX_SMALL_BYTES)
        ld = logs_dir or log_dir()
        if ld.is_dir():
            for f in sorted(ld.glob("fstk*.log*")):
                add_file(f"logs/{f.name}", f, MAX_LOG_BYTES)
        if profile is not None:
            want = {e.upper() for e in episodes} if episodes else None
            for d in episode_dirs(profile):
                canonical = f"{d.parent.name}:{d.name}".upper()
                if want is not None and canonical not in want:
                    continue
                base = f"episodes/{d.parent.name}-{d.name}"
                for f in sorted(d.rglob("*")):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(d).as_posix()
                    top = rel.split("/")[0]
                    if top in ("logs", "plans", "manifest", "reports", "approvals", "review"):
                        add_file(f"{base}/{rel}", f, MAX_LOG_BYTES if top == "logs" else MAX_SMALL_BYTES)
        readme = ("FromStunToKill debug bundle\n"
                  f"created {time.strftime('%Y-%m-%d %H:%M:%S')} local, machine {platform.node()}\n"
                  "Text files only (home folder shown as ~, user name as <user>); no media. Long tool logs are cut to "
                  "their last part.\n\nFiles:\n" + "\n".join(f"  {a}" for a in listing)
                  + ("\n\nNot included:\n" + "\n".join(f"  {s}" for s in skipped) if skipped else ""))
        z.writestr("README.txt", readme)
    return {"path": str(out), "files": len(listing) + 1, "bytes": out.stat().st_size, "skipped": skipped}
