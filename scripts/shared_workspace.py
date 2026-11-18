#!/usr/bin/env python3
"""Create and populate a stable per-device tree of isolated episode workspaces.

The README/code-bundle folder is never an output target. The preferred mutation is
under its pre-provisioned ``devices/`` subtree: one stable device folder with
series-scoped manifests, logs/references, and reviewed cut-PAD stills. If that workspace is
unavailable, the helper creates an isolated fallback under the machine profile's
local roots. It never edits ACLs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)
SERIES_RE = re.compile(r"^(DS9|VOY)$", re.IGNORECASE)
IMAGE_RE = re.compile(r"^DVD-(\d{2})-(\d{2})-(\d{2})\.(\d{3})-f(\d{8})\.png$", re.IGNORECASE)
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
DEVICE_ID_RE = re.compile(r"^[0-9A-F]{8}$")
REFERENCE_EXTENSIONS = {".md", ".txt", ".json", ".csv", ".tsv", ".log"}
LOGGING_GUIDE = """# Episode workspace logs

Keep diagnostic evidence here for this device-instance and episode. This folder is
under the `devices/` subtree beside the README that started the run. If that shared
location cannot be created, the workflow warns and uses the machine profile's local
roots/logs fallback instead; it never writes other release files.

Retain the unique stage `.log` and `.json` report for each run. Include the workflow
version, episode/stage, exact command and arguments, start/finish times, gate results,
complete tool output, and exit status. Preserve Orc refresh JSON and stderr, FFmpeg
command/version sidecars, preflight/tool-version reports, hashes, and structured
artifacts in their original form. Do not keep only filtered error lines or overwrite
prior runs; correlate evidence by its timestamped filename.

`run_stage.py` reports active elapsed, gate, action, and post-action seconds for each
invocation. Time between invocations is excluded. For a manual stage, use
`python scripts/stage_clock.py start --profile <profile> --episode <code> --stage <name>`;
pause before waiting for a human gate input, resume after the answer, then stop at stage
completion. Its JSON record separates active time, human wait, and wall time.

Never put passwords, auth headers, or access tokens in command lines or logs. Shared-mode
redaction handles configured machine-local paths, not arbitrary secrets. Preserve failed
run logs for diagnosis; do not clean them as part of a rerun.
"""

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fileutil import sha256_file as _shared_sha256_file  # noqa: E402
from dependency_lock import LockError, redact_shared_text, shared_redactions  # noqa: E402


def load_profile(path: str | None) -> tuple[Path, dict]:
    if not path:
        path = os.environ.get("LDP_PROFILE")
    if not path:
        raise LockError("shared workspace requires --profile or LDP_PROFILE pointing to a local machine.json")
    profile_path = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    if not profile_path.is_file():
        raise LockError(f"local profile not found: {profile_path}")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read local profile {profile_path}: {exc}") from exc
    shared = profile.get("shared") or {}
    if shared.get("enabled") is not True:
        raise LockError("set shared.enabled=true in the device-local profile for this README-folder workspace")
    release_value = shared.get("release_root")
    if not release_value:
        raise LockError("shared.release_root must be set to the folder containing this README")
    release = Path(os.path.expandvars(os.path.expanduser(str(release_value)))).resolve()
    if release != ROOT.resolve():
        raise LockError(f"shared.release_root ({release}) must match this code bundle ({ROOT})")
    validate_device_config_profile(profile_path, profile, release)
    return profile_path, profile


def configure_shared_profile(source_value: str, output_value: str,
                             device_name: str | None) -> Path:
    """Create an external local profile for this README-relative workspace."""
    source = Path(os.path.expandvars(os.path.expanduser(source_value))).resolve()
    output = Path(os.path.expandvars(os.path.expanduser(output_value))).resolve()
    if not source.is_file():
        raise LockError(f"local source profile not found: {source}")
    if source.is_relative_to(ROOT.resolve()) or output.is_relative_to(ROOT.resolve()):
        raise LockError("machine profiles must remain outside the shared release tree")
    if output.exists():
        raise LockError(f"refusing to overwrite local profile: {output}")
    try:
        profile = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read source profile {source}: {exc}") from exc
    name = device_name or profile.get("machine") or "device"
    profile["shared"] = {"enabled": True, "release_root": str(ROOT.resolve()),
                         "device_name": name}
    roots = profile.setdefault("roots", {})
    roots["runbook_dir"] = str(ROOT.resolve())
    tools = profile.setdefault("tools", {})
    plugins = ROOT / "avsplugins"
    if profile.get("os") in ("windows", "win32") and plugins.is_dir():
        tools["avisynth_plugin_dir"] = str(plugins)
    for name in ("downloads_root", "work", "probe", "avs", "audio", "subs", "out",
                 "prores", "stack", "logs"):
        raw = roots.get(name)
        if raw:
            candidate = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
            try:
                candidate.relative_to(ROOT.resolve())
            except ValueError:
                continue
            raise LockError(f"roots.{name} points into the shared release; set it to device-local storage first")
    profile.setdefault("notes", []).append(
        "README-relative shared-release profile: source files are read-only. Episode "
        "manifests/logs/references/cut-PAD images belong under "
        "devices/<device-instance>/episodes/<identity-folder>; run_stage creates/reuses that workspace "
        "and uses a local fallback without gating if it is unavailable.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return output


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    if not cleaned:
        raise LockError(f"cannot make a safe path component from {value!r}")
    return cleaned[:64]


def parse_episode_identity(value: str, series: str | None = None) -> tuple[str, str, str, str]:
    """Normalize a qualified identity; old bare SxxExx values remain DS9."""
    raw = str(value or "").strip()
    if ":" in raw:
        embedded, raw = raw.split(":", 1)
        if series and embedded.upper() != series.upper():
            raise LockError(f"episode identity {value!r} conflicts with series {series!r}")
        series = embedded
    chosen = str(series or "DS9").upper()
    if not SERIES_RE.fullmatch(chosen):
        raise LockError(f"unsupported workspace series {chosen!r}; use DS9 or VOY")
    if not EPISODE_RE.fullmatch(raw):
        raise LockError(f"episode must be SxxExx or SERIES:SxxExx, got {value!r}")
    code = raw.upper()
    identity = f"{chosen}:{code}"
    folder = code if chosen == "DS9" else f"{chosen}-{code}"
    return chosen, code, identity, folder


def device_identity_path(device: str) -> Path:
    """Machine-local sidecar that keeps one device folder stable across episodes."""
    configured = os.environ.get("LDP_DEVICE_ID_DIR")
    root = (Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
            if configured else Path.home() / ".config" / "ld-pipeline" / "device-identities")
    return root / f"{device}.json"


def _load_device_identity(path: Path, device: str, machine: str | None) -> str:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"invalid machine-local device identity {path}: {exc}") from exc
    instance_id = str(record.get("instance_id", "")).upper()
    if (record.get("schema") != "ld-pipeline-device-identity-v1" or
            record.get("device_name") != device or
            record.get("machine") != machine or
            not DEVICE_ID_RE.fullmatch(instance_id)):
        raise LockError(f"device identity does not match this profile/device: {path}")
    return instance_id


def _legacy_device_ids(devices: Path, device: str, machine: str | None) -> set[str]:
    """Discover old per-episode-generated folder suffixes without choosing ambiguously."""
    found = set()
    if not devices.is_dir():
        return found
    for folder in devices.glob(f"{device}-*"):
        if folder.is_symlink() or not folder.is_dir():
            continue
        suffix = folder.name[len(device) + 1:]
        if not DEVICE_ID_RE.fullmatch(suffix):
            continue
        for metadata_path in folder.glob("episodes/*/workspace.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (metadata.get("machine") == machine and
                    str(metadata.get("instance_id", "")).upper() == suffix):
                found.add(suffix)
                break
    return found


def _unverified_legacy_device_folders(devices: Path, device: str,
                                      verified_ids: set[str]) -> list[str]:
    """Find matching suffix folders whose ownership cannot be established safely."""
    if not devices.is_dir():
        return []
    result = []
    prefix = device + "-"
    for folder in devices.glob(f"{device}-*"):
        suffix = folder.name[len(prefix):]
        if DEVICE_ID_RE.fullmatch(suffix) and suffix not in verified_ids:
            result.append(folder.name)
    return sorted(result)


def _write_device_identity(path: Path, device: str, machine: str | None,
                           instance_id: str) -> str:
    """Persist a small identity record atomically without overwriting another process's choice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema": "ld-pipeline-device-identity-v1", "device_name": device,
              "machine": machine, "instance_id": instance_id,
              "created_utc": datetime.now(timezone.utc).isoformat()}
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-link creation is atomic and refuses to replace a concurrent winner.
            os.link(temp, path)
            return instance_id
        except FileExistsError:
            return _load_device_identity(path, device, machine)
    finally:
        temp.unlink(missing_ok=True)


def validate_device_config_profile(profile_path: Path, profile: dict,
                                   release_root: Path | None = None) -> bool:
    """Allow an in-release profile only inside its own verified device config folder.

    Ordinary shared-release paths remain invalid for machine profiles. This narrow
    exception is for ``devices/<device>-<id>/local device config/machine.json`` and
    requires the matching machine-local identity marker, so another device cannot
    accidentally use this profile merely because it can see the shared folder.
    """
    release = (release_root or workspace_release_root(profile)).resolve()
    candidate = Path(profile_path).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(release)
    except ValueError:
        return False

    shared = profile.get("shared") or {}
    if shared.get("enabled") is not True or workspace_release_root(profile) != release:
        raise LockError("a profile inside the release must enable this exact shared release")
    machine = str(profile.get("machine") or "").strip()
    device_label = safe_component(str(shared.get("device_name") or machine or "device"))
    instance_id = str(shared.get("device_instance_id") or "").upper()
    if not machine or not DEVICE_ID_RE.fullmatch(instance_id):
        raise LockError("device-local shared profile needs machine and an 8-character device_instance_id")
    expected = (release / "devices" / f"{device_label}-{instance_id}" /
                "local device config" / "machine.json").resolve(strict=False)
    if candidate != expected:
        raise LockError(
            "machine profile inside the shared release must be this device's "
            "devices/<device>-<id>/local device config/machine.json")
    for path in (release / "devices", expected.parent.parent, expected.parent, expected):
        if path.is_symlink():
            raise LockError(f"device-local configuration path may not contain symlinks: {path}")
    identity_path = device_identity_path(device_label)
    if identity_path.is_symlink() or not identity_path.is_file():
        raise LockError("this device's local identity marker is missing or unsafe")
    stored_id = _load_device_identity(identity_path, device_label, machine)
    if stored_id != instance_id:
        raise LockError("device profile instance id does not match this machine's local identity")
    return True


def _template_for_platform(bundle_root: Path, system: str | None = None) -> tuple[dict, str]:
    system = (system or platform.system()).casefold()
    target_os = "windows" if system in ("windows", "win32") else "darwin" if system in ("darwin", "macos") else "linux"
    template_name = {
        "windows": "machine.example.json",
        "darwin": "machine-macos.example.json",
        "linux": "machine-linux-x86_64.example.json",
    }[target_os]
    template_path = bundle_root / template_name
    try:
        value = json.loads(template_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read device profile template {template_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LockError(f"device profile template must be an object: {template_path}")
    return value, target_os


def _existing_avisynth_plugin_dir(machine: str) -> str | None:
    """Reuse one unambiguous, verified plugin path from this machine's prior profiles."""
    local = Path(os.environ.get("LOCALAPPDATA") or
                 (Path.home() / "AppData" / "Local"))
    directory = local / "ld-pipeline" / "profiles"
    found: set[str] = set()
    if not directory.is_dir():
        return None
    for path in directory.glob("machine*.json"):
        try:
            profile = json.loads(path.read_text(encoding="utf-8-sig"))
            if str(profile.get("machine", "")).casefold() != machine.casefold():
                continue
            value = str((profile.get("tools") or {}).get("avisynth_plugin_dir") or "").strip()
            plugins = Path(os.path.expandvars(os.path.expanduser(value))) if value else None
            if (plugins and plugins.is_dir() and
                    all((plugins / name).is_file()
                        for name in ("ffms2.dll", "RemapFrames_x64.dll"))):
                found.add(str(plugins.resolve()))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return next(iter(found)) if len(found) == 1 else None


def bootstrap_device_profile(bundle_root: Path, *, system: str | None = None,
                             machine: str | None = None) -> tuple[Path, bool]:
    """Create the device directory first, then its ignored, device-bound profile.

    The stable id lives in the user's local config directory as a small identity
    pointer; the full machine settings live under the device's ignored release
    subtree. No existing profile or device workspace is overwritten.
    """
    release = Path(bundle_root).expanduser().resolve(strict=True)
    # Canonicalize host casing so the bootstrap finds identities previously
    # created from Windows COMPUTERNAME (usually uppercase) as well as socket hostname.
    machine_name = str(machine or socket.gethostname()).strip().upper()
    if not machine_name:
        raise LockError("machine/device name is empty")
    device = safe_component(machine_name)
    identity_path = device_identity_path(device)
    if identity_path.is_symlink():
        raise LockError(f"refusing device-identity symlink: {identity_path}")
    try:
        identity_path.resolve(strict=False).relative_to(release)
    except ValueError:
        pass
    else:
        raise LockError("machine-local device identity marker must stay outside the app release")
    if identity_path.is_file():
        instance_id = _load_device_identity(identity_path, device, machine_name)
    else:
        instance_id = _write_device_identity(
            identity_path, device, machine_name, secrets.token_hex(4).upper())

    device_root = release / "devices" / f"{device}-{instance_id}"
    for path in (release / "devices", device_root):
        if path.is_symlink():
            raise LockError(f"device workspace path may not be a symlink: {path}")
    device_root.mkdir(parents=True, exist_ok=True)
    config_dir = device_root / "local device config"
    if config_dir.is_symlink():
        raise LockError(f"device configuration directory may not be a symlink: {config_dir}")
    config_dir.mkdir(exist_ok=True)
    profile_path = config_dir / "machine.json"

    if profile_path.is_file():
        try:
            existing = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LockError(f"existing device profile is invalid and was not changed: {profile_path}: {exc}") from exc
        validate_device_config_profile(profile_path, existing, release)
        return profile_path, False
    if profile_path.exists() or profile_path.is_symlink():
        raise LockError(f"refusing non-regular device profile path: {profile_path}")

    profile, target_os = _template_for_platform(release, system)
    profile["machine"] = machine_name
    profile["os"] = target_os
    profile["shared"] = {
        "enabled": True,
        "release_root": str(release),
        "device_name": machine_name,
        "device_instance_id": instance_id,
    }
    profile["_readme"] = (
        "Device-scoped, Git-ignored machine configuration at "
        "devices/<device>-<id>/local device config/machine.json. It is validated against "
        "this machine's local identity marker and must not be copied to another device.")
    roots = profile.setdefault("roots", {})
    folder_names = {
        "downloads_root": "downloads", "dvd_root": "dvd", "work": "work",
        "probe": "probe", "avs": "avs", "audio": "audio", "subs": "subs",
        "out": "out", "prores": "prores", "stack": "stack", "logs": "logs",
        "upscale_root": "upscale",
    }
    for key, folder in folder_names.items():
        roots[key] = str(device_root / folder)
        (device_root / folder).mkdir(parents=True, exist_ok=True)
    roots["downloads_template"] = "{series} {episode} LaserDisc"
    roots["runbook_dir"] = str(release)
    profile.setdefault("notes", []).append(
        "This ignored profile belongs only to the verified device folder above. "
        "Episode manifests and durable workflow logs are stored below its per-episode devices workspace.")
    plugin_dir = _existing_avisynth_plugin_dir(machine_name)
    if plugin_dir:
        profile.setdefault("tools", {})["avisynth_plugin_dir"] = plugin_dir

    temp = profile_path.with_name(f".machine.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, profile_path)
            created = True
        except FileExistsError:
            created = False
    except OSError as exc:
        raise LockError(f"could not create device-local profile {profile_path}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
    try:
        saved = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        validate_device_config_profile(profile_path, saved, release)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot verify created device profile {profile_path}: {exc}") from exc
    return profile_path, created


def record_device_identity(profile: dict, device: str, instance_id: str) -> str:
    """Persist an explicitly selected legacy folder id; never replace an existing id."""
    instance_id = str(instance_id).upper()
    if not DEVICE_ID_RE.fullmatch(instance_id):
        raise LockError("device instance suffix must be exactly 8 hexadecimal characters")
    path = device_identity_path(device)
    if path.is_symlink():
        raise LockError(f"refusing device-identity symlink: {path}")
    try:
        path.resolve().relative_to(workspace_release_root(profile))
    except ValueError:
        pass
    else:
        raise LockError("machine-local device identity must stay outside the shared release tree")
    if path.exists():
        current = _load_device_identity(path, device, profile.get("machine"))
        if current != instance_id:
            raise LockError(f"selected workspace id {instance_id} conflicts with saved device id {current}")
        return current
    return _write_device_identity(path, device, profile.get("machine"), instance_id)


def adopt_workspace_device_identity(root: Path, profile: dict, metadata: dict) -> None:
    """Bind a validated, explicitly selected legacy workspace to this machine's identity."""
    devices = workspace_release_root(profile) / "devices"
    try:
        relative = root.resolve().relative_to(devices.resolve())
    except ValueError:
        return
    device = safe_component(str((profile.get("shared") or {}).get("device_name") or
                                profile.get("machine") or "device"))
    if len(relative.parts) != 3 or relative.parts[1] != "episodes":
        return
    prefix = device + "-"
    folder_name = relative.parts[0]
    if not folder_name.startswith(prefix):
        return
    suffix = folder_name[len(prefix):]
    if str(metadata.get("instance_id", "")).upper() != suffix.upper():
        raise LockError("selected workspace folder suffix does not match its metadata")
    record_device_identity(profile, device, suffix)


def stable_device_instance_id(profile: dict, device: str, devices: Path) -> str:
    """Resolve/persist the machine's stable suffix; never guess among legacy duplicates."""
    shared = profile.get("shared") or {}
    machine = profile.get("machine")
    override = (shared.get("device_instance_id") or
                os.environ.get("LDP_DEVICE_INSTANCE_ID"))
    if override is not None:
        override = str(override).upper()
        if not DEVICE_ID_RE.fullmatch(override):
            raise LockError("device_instance_id must be exactly 8 hexadecimal characters")
    identity_path = device_identity_path(device)
    if identity_path.is_symlink():
        raise LockError(f"refusing device-identity symlink: {identity_path}")
    try:
        identity_path.resolve().relative_to(workspace_release_root(profile))
    except ValueError:
        pass
    else:
        raise LockError("machine-local device identity must stay outside the shared release tree")
    if identity_path.exists():
        stored = _load_device_identity(identity_path, device, machine)
        if override and override != stored:
            raise LockError("configured device_instance_id conflicts with the saved machine identity")
        return stored
    if override:
        chosen = override
    else:
        legacy = _legacy_device_ids(devices, device, machine)
        unverified = _unverified_legacy_device_folders(devices, device, legacy)
        if unverified:
            raise LockError(
                "existing device folder ownership cannot be verified: "
                + ", ".join(unverified)
                + "; select/adopt one explicitly before creating another episode folder")
        if len(legacy) > 1:
            choices = ", ".join(sorted(legacy))
            raise LockError(
                f"multiple legacy device folders exist for {device} ({choices}); "
                "operator must set shared.device_instance_id or LDP_DEVICE_INSTANCE_ID "
                "to the intended existing folder before creating another episode")
        chosen = next(iter(legacy)) if legacy else secrets.token_hex(4).upper()
    return record_device_identity(profile, device, chosen)


def existing_device_instance_id(profile: dict, device: str) -> str | None:
    """Read an explicit/saved machine id without creating or adopting one."""
    shared = profile.get("shared") or {}
    override = shared.get("device_instance_id") or os.environ.get("LDP_DEVICE_INSTANCE_ID")
    if override is not None:
        value = str(override).upper()
        if not DEVICE_ID_RE.fullmatch(value):
            raise LockError("device_instance_id must be exactly 8 hexadecimal characters")
        return value
    path = device_identity_path(device)
    if path.is_symlink():
        raise LockError(f"refusing device-identity symlink: {path}")
    if path.exists():
        try:
            path.resolve().relative_to(workspace_release_root(profile))
        except ValueError:
            pass
        else:
            raise LockError("machine-local device identity must stay outside the shared release tree")
        return _load_device_identity(path, device, profile.get("machine"))
    return None


def workspace_release_root(profile: dict) -> Path:
    shared = profile.get("shared") or {}
    return Path(os.path.expandvars(os.path.expanduser(str(shared.get("release_root") or ROOT)))).resolve()


def ensure_logging_guide(logs_dir: Path) -> None:
    guide = logs_dir / "README.md"
    if guide.exists():
        return
    try:
        with guide.open("x", encoding="utf-8", newline="\n") as f:
            f.write(LOGGING_GUIDE)
    except FileExistsError:
        pass


def ensure_workspace_dirs(root: Path) -> None:
    for name in ("manifest", "logs", "references", "cut-pad-images"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_logging_guide(root / "logs")


def workspace_storage_mode(root: Path, profile: dict, metadata: dict) -> str:
    """Return the validated storage mode, inferring it for V7-era metadata.

    Legacy workspace records are not rewritten: their ``workflow_version`` remains
    the version at creation. The resolved mode is added only to the in-memory record
    returned to callers such as ``stage_clock.py``.
    """
    mode = metadata.get("storage_mode")
    if mode in ("shared-release", "readme-workspace", "local-fallback"):
        return mode
    if mode not in (None, ""):
        raise LockError(f"unsupported workspace storage_mode: {mode!r}")

    devices = workspace_release_root(profile) / "devices"
    try:
        root.resolve().relative_to(devices.resolve())
    except ValueError:
        key = metadata.get("local_root_key")
        if key in ("logs", "work", "probe"):
            return "local-fallback"
        raise LockError("legacy workspace has no storage_mode or valid local_root_key")
    shared = profile.get("shared") or {}
    return "shared-release" if shared.get("enabled") is True else "readme-workspace"


def normalize_workspace_record(root: Path, profile: dict, metadata: dict) -> dict:
    """Normalize legacy metadata in memory without changing its saved provenance."""
    if not metadata.get("storage_mode"):
        metadata["storage_mode"] = workspace_storage_mode(root, profile, metadata)
    return metadata


def initialize_workspace(profile_path: Path, profile: dict, episode: str,
                         series: str | None = None) -> dict:
    chosen, code, identity, _ = parse_episode_identity(episode, series)
    # Create a new workspace only when no matching workspace already exists.
    existing, warning = resolve_instance_workspace(
        profile_path, profile, code, create=False, series=chosen)
    if existing:
        raise LockError(
            "a matching workspace already exists; use 'ensure' or pass its explicit "
            "--instance-root instead of creating a duplicate")
    if warning and "workspaces for this device/episode" in warning:
        raise LockError(warning + "; use 'ensure' with an explicit --instance-root")
    return create_workspace(profile_path, profile, code, series=chosen)


def _shell_exports(profile_path: Path, root: Path, mode: str) -> dict[str, str]:
    try:
        metadata = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
        series = str(metadata.get("series", "DS9")).upper()
        episode = str(metadata.get("episode", root.name)).upper()
        identity = str(metadata.get("identity", f"{series}:{episode}"))
    except (OSError, json.JSONDecodeError):
        series, episode = "DS9", root.name.upper()
        identity = f"{series}:{episode}"
    return {
        "LDP_PROFILE": str(profile_path),
        "LDP_INSTANCE_ROOT": str(root.resolve()),
        "LDP_MANIFEST_DIR": str((root / "manifest").resolve()),
        "LDP_INSTANCE_LOGS": str((root / "logs").resolve()),
        "LDP_CUT_PAD_IMAGES": str((root / "cut-pad-images").resolve()),
        "LDP_INSTANCE_MODE": mode,
        "LDP_SERIES": series,
        "LDP_EPISODE": episode,
        "LDP_IDENTITY": identity,
    }


def _local_fallback_candidates(profile: dict, episode: str,
                                series: str | None = None) -> list[tuple[str, Path]]:
    roots = profile.get("roots") or {}
    shared = profile.get("shared") or {}
    release = workspace_release_root(profile)
    device = safe_component(str(shared.get("device_name") or profile.get("machine") or "device"))
    chosen, code, _, folder = parse_episode_identity(episode, series)
    candidates = []
    for key in ("logs", "work", "probe"):
        raw = roots.get(key)
        if not raw:
            continue
        base = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
        try:
            base.relative_to(release)
        except ValueError:
            candidates.append((key, base / "ld-pipeline-local" / device / "episodes" / folder))
    return candidates


def create_local_fallback(profile_path: Path, profile: dict, episode: str,
                          series: str | None = None) -> dict:
    """Create/use a device-local workspace when README-folder devices/ is unavailable."""
    chosen, code, identity, folder = parse_episode_identity(episode, series)
    failures = []
    for base_key, root in _local_fallback_candidates(profile, code, chosen):
        try:
            root.mkdir(parents=True, exist_ok=True)
            metadata_path = root / "workspace.json"
            if metadata_path.exists():
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (saved.get("storage_mode") != "local-fallback" or
                        saved.get("machine") != profile.get("machine") or
                        str(saved.get("episode", "")).casefold() != code.casefold() or
                        str(saved.get("series", "DS9")).upper() != chosen or
                        saved.get("identity", f"DS9:{saved.get('episode', '')}") != identity):
                    failures.append(f"refusing to reuse unrelated local folder {root}")
                    continue
            else:
                metadata = {
                    "schema": "ld-pipeline-shared-instance-v1",
                    "storage_mode": "local-fallback",
                    "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "episode": code,
                    "series": chosen,
                    "identity": identity,
                    "machine": profile.get("machine"),
                    "instance_id": "LOCAL-" + secrets.token_hex(4).upper(),
                    "release_root": ".",
                    "local_root_key": base_key,
                    "write_scope": "this device-local episode workspace; shared publication is unavailable",
                    "environment": {
                        "LDP_INSTANCE_ROOT": ".",
                        "LDP_MANIFEST_DIR": "manifest",
                        "LDP_INSTANCE_LOGS": "logs",
                        "LDP_CUT_PAD_IMAGES": "cut-pad-images",
                        "LDP_INSTANCE_MODE": "local-fallback",
                    },
                }
                with metadata_path.open("x", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)
                    f.write("\n")
            dirs = {"manifest": root / "manifest", "logs": root / "logs",
                    "references": root / "references", "cut-pad-images": root / "cut-pad-images"}
            for folder in dirs.values():
                folder.mkdir(parents=True, exist_ok=True)
            ensure_logging_guide(dirs["logs"])
            result = json.loads(metadata_path.read_text(encoding="utf-8"))
            result["instance_root"] = str(root.resolve())
            result["storage_mode"] = "local-fallback"
            result["shell_exports"] = _shell_exports(profile_path, root, "local-fallback")
            return result
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{root}: {exc}")
    detail = "; ".join(failures) if failures else "profile has no external local roots.logs/roots.work/roots.probe"
    raise LockError("could not create a device-local fallback workspace: " + detail)


def create_workspace(profile_path: Path, profile: dict, episode: str,
                     series: str | None = None) -> dict:
    chosen, code, identity, folder = parse_episode_identity(episode, series)
    shared = profile.get("shared") or {}
    devices = workspace_release_root(profile) / "devices"
    if not devices.is_dir():
        try:
            # The only permitted source-tree child is devices/. If the ACL does
            # not allow creating this one directory, use the local fallback.
            devices.mkdir()
        except OSError as exc:
            raise LockError(f"could not create README-folder devices directory {devices}: {exc}") from exc
    device = safe_component(str(shared.get("device_name") or profile.get("machine") or "device"))
    instance_id = stable_device_instance_id(profile, device, devices)
    instance_name = f"{device}-{instance_id}"
    device_instance = devices / instance_name
    if device_instance.is_symlink():
        raise LockError(f"refusing device workspace symlink: {device_instance}")
    if device_instance.exists() and not device_instance.is_dir():
        raise LockError(f"device workspace path is not a directory: {device_instance}")
    device_instance.mkdir(exist_ok=True)
    episodes_dir = device_instance / "episodes"
    if episodes_dir.is_symlink():
        raise LockError(f"refusing episodes-directory symlink: {episodes_dir}")
    episodes_dir.mkdir(exist_ok=True)
    episode_root = device_instance / "episodes" / folder
    if episode_root.is_symlink():
        raise LockError(f"refusing episode workspace symlink: {episode_root}")
    try:
        episode_root.mkdir()
    except FileExistsError:
        # A concurrent init may have reserved this same stable device/episode
        # workspace. Reuse it only if its provenance validates; never create a
        # sibling random folder as a collision workaround.
        validate_instance(str(episode_root), profile_path, profile, identity)
        metadata_path = episode_root / "workspace.json"
        metadata = normalize_workspace_record(
            episode_root, profile, json.loads(metadata_path.read_text(encoding="utf-8")))
        metadata["instance_root"] = str(episode_root.resolve())
        metadata["shell_exports"] = _shell_exports(
            profile_path, episode_root, metadata.get("storage_mode", "shared-release"))
        ensure_workspace_dirs(episode_root)
        return metadata
    dirs = {
        "manifest_dir": episode_root / "manifest",
        "logs": episode_root / "logs",
        "references": episode_root / "references",
        "cut_pad_images": episode_root / "cut-pad-images",
    }
    for path in dirs.values():
        path.mkdir()
    ensure_logging_guide(dirs["logs"])
    metadata = {
        "schema": "ld-pipeline-shared-instance-v1",
        "storage_mode": "shared-release" if shared.get("enabled") is True else "readme-workspace",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": code,
        "series": chosen,
        "identity": identity,
        "machine": profile.get("machine"),
        "instance_id": instance_id,
        "profile_name": profile_path.name,
        "release_root": ".",
        "instance_relative_path": episode_root.resolve().relative_to(ROOT.resolve()).as_posix(),
        "write_scope": "this machine's episode workspace only",
        "environment": {
            "LDP_INSTANCE_ROOT": ".",
            "LDP_MANIFEST_DIR": "manifest",
            "LDP_INSTANCE_LOGS": "logs",
            "LDP_CUT_PAD_IMAGES": "cut-pad-images",
            "LDP_INSTANCE_MODE": "shared-release" if shared.get("enabled") is True else "readme-workspace",
        },
    }
    with (episode_root / "workspace.json").open("x", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
    # Show the caller's device-local profile only in this command's output; do
    # not put that private path into the shared workspace metadata.
    metadata["instance_root"] = str(episode_root.resolve())
    metadata["shell_exports"] = _shell_exports(
        profile_path, episode_root,
        "shared-release" if shared.get("enabled") is True else "readme-workspace")
    return metadata


def resolve_instance_workspace(profile_path: Path, profile: dict, episode: str,
                               requested_root: str | None = None,
                               *, create: bool = True,
                               series: str | None = None) -> tuple[dict | None, str | None]:
    """Reuse or create the README-folder workspace; fall back locally without gating.

    The second return value is a warning for callers to surface in console and logs.
    A supplied but invalid/ambiguous shared path is never silently reused.
    """
    chosen, code, identity, folder = parse_episode_identity(episode, series)
    notes: list[str] = []
    if requested_root:
        try:
            root = validate_instance(requested_root, profile_path, profile, identity)
            ensure_workspace_dirs(root)
            meta = normalize_workspace_record(
                root, profile, json.loads((root / "workspace.json").read_text(encoding="utf-8")))
            adopt_workspace_device_identity(root, profile, meta)
            meta["instance_root"] = str(root)
            meta["shell_exports"] = _shell_exports(profile_path, root,
                                                   meta.get("storage_mode", "shared-release"))
            return meta, None
        except (LockError, OSError, json.JSONDecodeError) as exc:
            notes.append(f"requested README-folder workspace unavailable ({exc})")

    shared = profile.get("shared") or {}
    devices = Path(os.path.expandvars(os.path.expanduser(
        str(shared.get("release_root") or ROOT)))).resolve() / "devices"
    device = safe_component(str(shared.get("device_name") or profile.get("machine") or "device"))
    preferred_id = existing_device_instance_id(profile, device)
    candidates = []
    if devices.is_dir():
        for root in devices.glob(f"{device}-*/episodes/{folder}"):
            suffix = root.parents[1].name[len(device) + 1:]
            if preferred_id and suffix != preferred_id:
                continue
            try:
                candidates.append(validate_instance(str(root), profile_path, profile, identity))
            except (LockError, OSError, json.JSONDecodeError):
                continue
    if len(candidates) == 1 and not requested_root:
        root = candidates[0]
        try:
            ensure_workspace_dirs(root)
        except OSError as exc:
            notes.append(f"existing README-folder workspace is not writable ({exc})")
            candidates = []
        else:
            meta = normalize_workspace_record(
                root, profile, json.loads((root / "workspace.json").read_text(encoding="utf-8")))
            adopt_workspace_device_identity(root, profile, meta)
            meta["instance_root"] = str(root)
            meta["shell_exports"] = _shell_exports(profile_path, root,
                                                   meta.get("storage_mode", "shared-release"))
            return meta, None
    if len(candidates) > 1:
        notes.append(f"found {len(candidates)} README-folder workspaces for this device/episode; not choosing one")
    if not requested_root:
        for _, local_root in _local_fallback_candidates(profile, code, chosen):
            if not (local_root / "workspace.json").is_file():
                continue
            try:
                root = validate_instance(str(local_root), profile_path, profile, identity)
                ensure_workspace_dirs(root)
                meta = normalize_workspace_record(
                    root, profile, json.loads((root / "workspace.json").read_text(encoding="utf-8")))
                meta["instance_root"] = str(root)
                meta["shell_exports"] = _shell_exports(profile_path, root, "local-fallback")
                return meta, "; ".join(notes) if notes else "reusing existing device-local fallback workspace"
            except (LockError, OSError, json.JSONDecodeError):
                continue
    if not requested_root and not candidates and create:
        try:
            return create_workspace(profile_path, profile, code, series=chosen), None
        except (LockError, OSError, json.JSONDecodeError) as exc:
            notes.append(f"could not create workspace under README-folder devices/: {exc}")
    if not create:
        return None, "; ".join(notes) if notes else "no existing README-folder workspace"
    local = create_local_fallback(profile_path, profile, code, series=chosen)
    prefix = "; ".join(notes) if notes else "README-folder workspace was not initialized"
    return local, (prefix + f"; continuing with device-local fallback {local['instance_root']}")


def validate_instance(root_value: str, profile_path: Path, profile: dict,
                      expected_identity: str | None = None) -> Path:
    root = Path(os.path.expandvars(os.path.expanduser(root_value))).resolve()
    try:
        metadata = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"invalid/missing workspace.json at {root}: {exc}") from exc
    chosen, code, identity, folder = parse_episode_identity(
        expected_identity or metadata.get("identity") or metadata.get("episode", ""),
        metadata.get("series", "DS9"))
    saved_series = str(metadata.get("series", "DS9")).upper()
    saved_identity = metadata.get("identity", f"DS9:{metadata.get('episode', '')}")
    if (saved_series != chosen or str(metadata.get("episode", "")).upper() != code or
            saved_identity != identity or root.name.casefold() != folder.casefold()):
        raise LockError(f"workspace identity does not match requested {identity}: {root}")
    device = safe_component(str(profile.get("shared", {}).get("device_name") or
                                profile.get("machine") or "device"))
    mode = workspace_storage_mode(root, profile, metadata)
    if mode == "local-fallback":
        key = metadata.get("local_root_key")
        if key not in ("logs", "work", "probe"):
            raise LockError("local fallback workspace has an invalid local_root_key")
        raw = (profile.get("roots") or {}).get(key)
        if not raw:
            raise LockError(f"local fallback profile roots.{key} is no longer configured")
        base = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
        expected = base / "ld-pipeline-local" / device / "episodes" / folder
        if (root != expected.resolve() or metadata.get("machine") != profile.get("machine") or
                str(metadata.get("episode", "")).casefold() != code.casefold()):
            raise LockError("device-local fallback workspace does not match the selected profile/series/episode")
    else:
        devices = workspace_release_root(profile) / "devices"
        try:
            relative = root.relative_to(devices.resolve())
        except ValueError as exc:
            raise LockError(f"instance root must be under README-folder devices/: {root}") from exc
        if len(relative.parts) != 3 or relative.parts[1] != "episodes":
            raise LockError("instance root must have form devices/<device-instance>/episodes/<identity-folder>")
        if (not relative.parts[0].startswith(device + "-") or
                metadata.get("release_root") != "." or
                metadata.get("instance_relative_path") != root.relative_to(ROOT.resolve()).as_posix() or
                relative.parts[0].rsplit("-", 1)[-1] != metadata.get("instance_id")):
            raise LockError("workspace metadata does not match this README-folder release/device")
        if metadata.get("machine") != profile.get("machine"):
            raise LockError("shared workspace belongs to a different machine profile")
        if str(metadata.get("episode", "")).casefold() != code.casefold():
            raise LockError("shared workspace metadata episode code does not match its identity")
    if not root.is_dir():
        raise LockError(f"instance directory is missing: {root}")
    return root


def sha256(path: Path) -> str:
    return _shared_sha256_file(path)


def copy_exclusive(source: Path, destination: Path) -> None:
    if destination.exists():
        raise LockError(f"refusing to overwrite shared instance file: {destination}")
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def publish_reference(source: Path, instance: Path, name: str | None,
                      profile_path: Path, profile: dict) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise LockError(f"reference source is not a file: {source}")
    if source.suffix.casefold() not in REFERENCE_EXTENSIONS:
        raise LockError(f"reference extension {source.suffix!r} is not allowed; use a report/text format")
    filename = name or source.name
    if not SAFE_COMPONENT.fullmatch(filename) or Path(filename).name != filename:
        raise LockError(f"unsafe reference filename: {filename!r}")
    destination = instance / "references" / filename
    provenance = destination.with_name(destination.name + ".reference.json")
    if destination.exists() or provenance.exists():
        raise LockError(f"reference/provenance already exists; refusing overwrite: {destination}")
    source_text = source.read_text(encoding="utf-8", errors="replace")
    source_text = redact_shared_text(source_text, shared_redactions(profile_path, profile))
    with destination.open("x", encoding="utf-8", newline="\n") as f:
        f.write(source_text)
    record = {"schema": "ld-pipeline-reference-v1",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "filename": destination.name,
              "source_filename": source.name,
              "size_bytes": destination.stat().st_size,
              "sha256": sha256(destination)}
    with provenance.open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    return destination


def publish_cut_pad(source: Path, instance: Path, output_frame: int,
                    source_dvd: str | None, dvd_sha256: str,
                    mapping_verified: bool, color_match_note: str) -> tuple[Path, Path]:
    source = source.expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".png":
        raise LockError(f"cut-PAD source must be an existing PNG: {source}")
    match = IMAGE_RE.fullmatch(source.name)
    if not match:
        raise LockError("cut-PAD PNG name must be DVD-HH-MM-SS.mmm-fNNNNNNNN.png")
    if output_frame < 0:
        raise LockError("output cut-PAD frame must be nonnegative")
    if not mapping_verified:
        raise LockError("confirm the DVD scene/timestamp-to-output-PAD mapping before publishing this still")
    color_match_note = color_match_note.strip()
    if not color_match_note:
        raise LockError("record the reviewed color-match method before publishing this still")
    dvd_sha256 = dvd_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", dvd_sha256):
        raise LockError("provide the verified source DVD's 64-character SHA-256")
    with source.open("rb") as f:
        header = f.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise LockError(f"invalid PNG header: {source}")
    width, height = struct.unpack(">II", header[16:24])
    if (width, height) != (768, 484):
        raise LockError(f"cut-PAD PNG must match the LD raster 768x484; got {width}x{height}")
    image = instance / "cut-pad-images" / source.name
    provenance = image.with_suffix(".provenance.json")
    if image.exists() or provenance.exists():
        raise LockError(f"cut-PAD image/provenance already exists; refusing overwrite: {image}")
    copy_exclusive(source, image)
    workspace_metadata = json.loads((instance / "workspace.json").read_text(encoding="utf-8"))
    series = str(workspace_metadata.get("series", "DS9")).upper()
    episode = str(workspace_metadata.get("episode", instance.name)).upper()
    record = {
        "schema": "ld-pipeline-cut-pad-provenance-v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": episode,
        "series": series,
        "identity": str(workspace_metadata.get("identity", f"{series}:{episode}")),
        "output_pad_frame": output_frame,
        "dvd_timestamp": f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}",
        "dvd_frame": int(match.group(5)),
        "source_dvd": source_dvd or "user-supplied verified DVD",
        "source_dvd_sha256": dvd_sha256,
        "png_sha256": sha256(image),
        "reviewed_episode_scene_and_pad_mapping": True,
        "color_match_note": color_match_note,
        "source_filename": source.name,
    }
    with provenance.open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    return image, provenance


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a new workspace only if no matching one exists; otherwise use ensure")
    init.add_argument("--profile", required=True)
    init.add_argument("--episode", required=True)
    init.add_argument("--series", choices=("DS9", "VOY"), help="episode series; bare legacy episodes default to DS9")
    ref = sub.add_parser("publish-reference", help="copy one useful report into this instance's references folder")
    ref.add_argument("--profile", required=True)
    ref.add_argument("--instance-root", required=True)
    ref.add_argument("--source", required=True)
    ref.add_argument("--name")
    cut = sub.add_parser("publish-cut-pad", help="publish a reviewed timestamped PNG and provenance into this instance")
    cut.add_argument("--profile", required=True)
    cut.add_argument("--instance-root", required=True)
    cut.add_argument("--source", required=True)
    cut.add_argument("--output-pad-frame", required=True, type=int)
    cut.add_argument("--source-dvd", help="DVD identity/filename for the provenance record")
    cut.add_argument("--source-dvd-sha256", required=True)
    cut.add_argument("--confirm-mapping", action="store_true", required=True,
                     help="assert that the timestamp/scene and output PAD frame were independently verified")
    cut.add_argument("--color-match-note", required=True,
                     help="brief record of the reviewed DVD-to-LD color-match method/evidence")
    ensure = sub.add_parser("ensure", help="reuse/create the README-folder workspace or use a local fallback")
    ensure.add_argument("--profile", required=True)
    ensure.add_argument("--episode", required=True)
    ensure.add_argument("--series", choices=("DS9", "VOY"), help="episode series; bare legacy episodes default to DS9")
    ensure.add_argument("--instance-root", help="reuse this existing workspace when valid")
    cfg = sub.add_parser("configure-profile", help="create a new local profile for this shared release")
    cfg.add_argument("--source", required=True, help="existing device-local profile to copy")
    cfg.add_argument("--output", required=True, help="new profile path outside the shared release; no overwrite")
    cfg.add_argument("--device-name", help="sanitized device label stored in the profile")
    args = ap.parse_args()
    try:
        if args.command == "configure-profile":
            path = configure_shared_profile(args.source, args.output, args.device_name)
            print(f"created local shared profile (not in release tree): {path}")
            print(f"shared.release_root={ROOT.resolve()}")
            return 0
        profile_path, profile = load_profile(args.profile)
        if args.command == "ensure":
            result, warning = resolve_instance_workspace(
                profile_path, profile, args.episode, args.instance_root, create=True,
                series=args.series)
            if not result:
                raise LockError(warning or "could not prepare episode workspace")
            print(json.dumps({"workspace": result, "warning": warning}, indent=2))
            return 0
        if args.command == "init":
            result = initialize_workspace(profile_path, profile, args.episode,
                                          series=getattr(args, "series", None))
            print(json.dumps(result, indent=2))
        else:
            instance = validate_instance(args.instance_root, profile_path, profile)
            if args.command == "publish-reference":
                destination = publish_reference(Path(args.source), instance, args.name,
                                                profile_path, profile)
                print(f"published reference (no overwrite): {destination}")
            else:
                image, provenance = publish_cut_pad(
                    Path(args.source), instance, args.output_pad_frame,
                    args.source_dvd, args.source_dvd_sha256, args.confirm_mapping,
                    args.color_match_note)
                print(f"published cut-PAD image: {image}\nprovenance: {provenance}")
        return 0
    except (LockError, OSError, json.JSONDecodeError) as exc:
        print(f"SHARED WORKSPACE ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
