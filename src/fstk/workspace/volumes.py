"""Which drives this machine has, how much room each one has, and where fstk's folders should live.

Why this exists (v38 run record, execution log §6): the owner's laptop has a 238 GB system SSD with ~30 GiB free
and a 2.2 TB data drive. A work folder left on the system drive could never satisfy the free-space reserve, and the
run stopped at the first space check. The advice here is given *before* work starts, and again whenever the space
gate blocks a step.

Typical needs are measured, not guessed (``workflow/plans.py``, v38 logs):

* an episode's captures (5 TBC captures + audio) are 410–456 GB;
* the stacked master is ~500 KB per frame, ~42 GiB for a 83,000-frame episode, and the working folder also holds
  the decoded EFM audio, the finished encodes and scratch files.

Network drives and removable drives are never *suggested* for heavy work (the owner keeps the SMB USB drive as a
read-only input), but they are listed so the operator sees them.
"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

GIB = 1024 ** 3
DOWNLOADS_NEED_GIB = 460.0        # one episode's captures (largest measured: 456 GB, S01E03)
WORK_NEED_GIB = 80.0              # stack master + EFM audio + encodes + scratch for one episode
DATA_NEED_GIB = 5.0               # manifests, plans, reports, logs


@dataclass(frozen=True)
class Volume:
    mount: str                    # "C:\\" or "/home"
    total: int
    free: int
    kind: str = "local"           # local | removable | network | other
    readonly: bool = False
    fstype: str = ""

    @property
    def free_gib(self) -> float:
        return self.free / GIB

    @property
    def total_gib(self) -> float:
        return self.total / GIB

    @property
    def suggestible(self) -> bool:
        return self.kind == "local" and not self.readonly

    def label(self) -> str:
        kind = "" if self.kind == "local" else f", {self.kind}"
        return f"{self.mount} ({self.free_gib:,.0f} GiB free of {self.total_gib:,.0f}{kind})"


def _windows_kind(mount: str) -> str:
    try:
        import ctypes

        t = ctypes.windll.kernel32.GetDriveTypeW(mount)           # type: ignore[attr-defined]
    except Exception:
        return "local"
    return {2: "removable", 4: "network", 3: "local", 5: "other", 6: "other"}.get(t, "local")


def list_volumes() -> list[Volume]:
    """Every mounted volume that can be measured. Pseudo file systems (proc, tmpfs, loop) are left out."""
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return []
    out: list[Volume] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        opts = (part.opts or "").split(",")
        fstype = (part.fstype or "").lower()
        if platform.system() == "Windows":
            kind = _windows_kind(part.mountpoint)
            if "cdrom" in opts:
                kind = "other"
        elif fstype in ("nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs", "afpfs", "9p", "drvfs"):
            kind = "network"
        elif fstype in ("squashfs", "overlay", "tmpfs", "devtmpfs", "ramfs", "iso9660", "udf"):
            continue
        else:
            kind = "local"
        try:
            use = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if use.total <= 0:
            continue
        out.append(Volume(part.mountpoint, use.total, use.free, kind, "ro" in opts, fstype))
    return out


def _norm(path: str) -> tuple[str, bool]:
    """(comparable text, windows-style): drive-letter paths compare case-insensitively with ``\\`` separators."""
    text = str(path)
    if len(text) >= 2 and text[1] == ":":
        text = text.replace("/", "\\").rstrip("\\").lower()
        return text, True
    return text.rstrip("/") or "/", False


def volume_of(path: str | os.PathLike, volumes: list[Volume]) -> Volume | None:
    """The volume holding ``path`` (the longest matching mount point), even if the folder is not created yet."""
    text, windows = _norm(str(path) if windows_like(path) else str(Path(path).absolute()))
    best: Volume | None = None
    for v in volumes:
        mount, v_windows = _norm(v.mount)
        if v_windows != windows:
            continue
        sep = "\\" if windows else "/"
        inside = text == mount or text.startswith(mount.rstrip(sep) + sep) or mount in ("/", "")
        if inside and (best is None or len(v.mount) > len(best.mount)):
            best = v
    return best


def windows_like(path: str | os.PathLike) -> bool:
    text = str(path)
    return len(text) >= 2 and text[1] == ":"


@dataclass(frozen=True)
class Advice:
    level: str                    # "ok" | "info" | "warn" | "block"
    what: str                     # which folder: "Working folder", "Downloads", "Data"
    path: str
    text: str
    suggestion: str = ""          # a folder that would work better, or ""


def suggest_folder(volumes: list[Volume], need_gib: float, reserve_gib: float, *, leaf: str = "FromStunToKill",
                   avoid: str | None = None) -> Volume | None:
    """The local volume with the most room that can hold ``need_gib`` and still keep ``reserve_gib`` free."""
    ok = [v for v in volumes if v.suggestible and v.free_gib >= need_gib + reserve_gib
          and (avoid is None or v.mount != avoid)]
    return max(ok, key=lambda v: v.free) if ok else None


def _suggest_path(vol: Volume, leaf: str) -> str:
    return str(Path(vol.mount) / "FromStunToKill" / leaf)


def advise(profile, volumes: list[Volume] | None = None) -> list[Advice]:
    """Check where the profile keeps its folders against the room those drives have."""
    volumes = list_volumes() if volumes is None else volumes
    reserve = float(profile.limits.min_free_gb)
    checks: list[tuple[str, Path | None, float, str]] = [
        ("Data folder", profile.data_root, DATA_NEED_GIB, "data"),
        ("Working folder", profile.media_root, WORK_NEED_GIB, "work"),
    ]
    dl = (profile.sources or {}).get("downloads_root") if hasattr(profile, "sources") else None
    if dl:
        checks.append(("Downloads folder", Path(dl), DOWNLOADS_NEED_GIB, "downloads"))
    out: list[Advice] = []
    for what, path, need, leaf in checks:
        if path is None:
            continue
        vol = volume_of(path, volumes)
        if vol is None:
            out.append(Advice("info", what, str(path), f"{what}: could not measure the drive holding {path}."))
            continue
        better = suggest_folder(volumes, need, reserve, leaf=leaf, avoid=vol.mount)
        sug = _suggest_path(better, leaf) if better else ""
        if vol.free_gib < reserve:
            text = (f"{what} is on {vol.mount}, which has {vol.free_gib:,.0f} GiB free: less than the "
                    f"{reserve:,.0f} GiB reserve, so every step that writes there would be blocked.")
            out.append(Advice("block", what, str(path), text + (f" {better.label()} could hold it." if better else ""), sug))
        elif vol.free_gib < need + reserve:
            text = (f"{what} is on {vol.mount} with {vol.free_gib:,.0f} GiB free; one episode needs about "
                    f"{need:,.0f} GiB plus the {reserve:,.0f} GiB reserve.")
            out.append(Advice("warn", what, str(path), text + (f" {better.label()} has more room." if better else ""), sug))
        elif vol.kind in ("network", "removable"):
            out.append(Advice("info", what, str(path),
                              f"{what} is on {vol.mount}, a {vol.kind} drive: heavy writes there are slow and can "
                              "stall; a local drive is better.", sug))
        else:
            out.append(Advice("ok", what, str(path), f"{what}: {vol.free_gib:,.0f} GiB free on {vol.mount}."))
    return out


def hint_for_space(reserve_gib: float, target: str | os.PathLike, volumes: list[Volume] | None = None) -> str:
    """One sentence to add to a 'blocked by disk space' message: where there is room, and how to move there.
    Never raises: it only decorates a message that is already an error."""
    try:
        text = _hint_for_space(reserve_gib, target, volumes)
    except Exception:
        text = ""
    if sys.platform == "darwin":
        text += (f" On a Mac only space the system can hand over right now counts: files still in the Trash, "
                 f"purgeable space and local Time Machine snapshots do not. In Terminal, `df -h \"{target}\"` shows "
                 f"the number fstk measured.")
    return text.strip()


def _hint_for_space(reserve, target, volumes) -> str:
    volumes = list_volumes() if volumes is None else volumes
    here = volume_of(target, volumes)
    better = suggest_folder(volumes, WORK_NEED_GIB, reserve, avoid=here.mount if here else None)
    if better is None:
        return "No other local drive has room for one episode plus the reserve; free some space or add a bigger drive."
    return (f"{better.label()} has room: set the working folder there in Settings → Folders "
            f"(for example {_suggest_path(better, 'work')}).")


def worst(advice: list[Advice]) -> str:
    order = {"ok": 0, "info": 1, "warn": 2, "block": 3}
    return max((a.level for a in advice), key=lambda x: order[x], default="ok")
