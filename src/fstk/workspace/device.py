"""Stable device identity (ADR 0002 §2), ported from v38 ``shared_workspace.py``.

Each machine gets an 8-hex instance id, created once and kept in a small machine-local file next
to its profile (never in the shared tree, never in the code tree). The shared device folder is
``<DEVICE>-<ID>`` where DEVICE is the profile's machine name made path-safe. A ``device.json``
marker inside that folder records which machine owns it; a folder whose marker names another
machine or id is never reused.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from fstk import __version__
from fstk.core.errors import ConfigError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso

IDENTITY_SCHEMA = "fstk-device-identity-v1"
MARKER_SCHEMA = "fstk-shared-device-v1"
ID_RE = re.compile(r"^[0-9A-F]{8}$")


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    if not cleaned:
        raise ConfigError(f"cannot make a safe folder name from {value!r}")
    return cleaned[:48].upper()


@dataclass(frozen=True)
class DeviceIdentity:
    machine: str
    instance_id: str

    @property
    def name(self) -> str:
        """Shared device folder name, e.g. ``PC-A-0A1B2C3D``."""
        return f"{safe_component(self.machine)}-{self.instance_id}"


def identity_path(profile) -> Path:
    """``device.json`` beside the profile file (the profile itself is machine-local)."""
    from fstk.config.profile import default_profile_path

    base = Path(profile.path) if profile.path else default_profile_path()
    return base.with_name("device.json")


def load_or_create(profile) -> DeviceIdentity:
    """Read this machine's identity, creating it once. ``profile.instance_id`` may pin an existing
    id (e.g. imported from v38's ``shared.device_instance_id``); a conflicting saved id is refused."""
    path = identity_path(profile)
    pinned = (profile.instance_id or "").upper()
    if path.is_file():
        doc = read_json(path)
        if doc.get("schema") != IDENTITY_SCHEMA or not ID_RE.fullmatch(str(doc.get("instance_id", ""))):
            raise ConfigError(f"invalid device identity file {path}")
        if doc.get("machine") != profile.machine:
            raise ConfigError(f"{path} belongs to machine {doc.get('machine')!r}, this profile is "
                              f"{profile.machine!r}; profiles and identities are never copied between machines")
        if pinned and pinned != doc["instance_id"]:
            raise ConfigError(f"profile instance_id {pinned} conflicts with this machine's saved id "
                              f"{doc['instance_id']} ({path})")
        return DeviceIdentity(profile.machine, doc["instance_id"])
    chosen = pinned or secrets.token_hex(4).upper()
    try:
        atomic_write_json(path, {"schema": IDENTITY_SCHEMA, "machine": profile.machine, "instance_id": chosen,
                                 "created_utc": utc_now_iso()}, exclusive=True)
    except FileExistsError:   # another process won the race: use its choice
        return load_or_create(profile)
    return DeviceIdentity(profile.machine, chosen)


def claim_folder(devices_dir: Path, ident: DeviceIdentity, *, os_name: str) -> Path:
    """Create or verify this device's shared folder and its ownership marker."""
    folder = devices_dir / ident.name
    for p in (devices_dir, folder):
        if p.is_symlink():
            raise ConfigError(f"shared device path may not be a symlink: {p}")
    marker = folder / "device.json"
    if folder.is_dir() and not marker.is_file() and any(folder.iterdir()):
        raise ConfigError(f"{folder} already exists without an fstk device marker (a v38 device workspace?); "
                          "fstk never writes into it — use a separate shared_root")
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"schema": MARKER_SCHEMA, "machine": ident.machine, "instance_id": ident.instance_id,
           "os": os_name, "created_utc": utc_now_iso(), "fstk_version": __version__}
    try:
        atomic_write_json(marker, doc, exclusive=True)
    except FileExistsError:
        have = read_json(marker)
        if have.get("machine") != ident.machine or have.get("instance_id") != ident.instance_id:
            raise ConfigError(f"shared device folder {folder} is owned by {have.get('machine')!r}/"
                              f"{have.get('instance_id')!r}, not this machine; it is never reused") from None
    atomic_write_json(folder / "heartbeat.json", {"utc": utc_now_iso(), "fstk_version": __version__,
                                                  "pid": os.getpid()})
    return folder
