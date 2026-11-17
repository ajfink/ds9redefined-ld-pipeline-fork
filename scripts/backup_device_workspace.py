#!/usr/bin/env python3
"""Create, verify, and safely restore a checksum-verified device workspace backup.

Only metadata/evidence below an episode workspace is archived; media roots are not
followed. Backups are immutable, stored outside the shared release, and restoration
is dry-run unless --apply is explicitly supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA = "fromstuntokill-device-workspace-backup-v1"
EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)
SAFE_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ALLOWED_EPISODE_DIRS = {"manifest", "logs", "references", "plans", "reviews", "cut-pad-images"}
VOLATILE_NAMES = {".DS_Store"}
VOLATILE_SUFFIXES = (".tmp", ".lock")
DEFAULT_MAX_GIB = 10.0


class BackupError(RuntimeError):
    """Backup is malformed, unsafe, oversized, or conflicts with existing data."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_workspace(instance: Path) -> tuple[dict, str, str, str]:
    if instance.is_symlink():
        raise BackupError(f"episode workspace root may not be a symlink: {instance}")
    workspace_path = instance / "workspace.json"
    if workspace_path.is_symlink() or not workspace_path.is_file():
        raise BackupError(f"device episode workspace.json is missing or unsafe: {workspace_path}")
    try:
        workspace = json.loads(workspace_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"cannot read workspace metadata: {exc}") from exc
    if not isinstance(workspace, dict) or workspace.get("schema") != "ld-pipeline-shared-instance-v1":
        raise BackupError("unsupported workspace.json schema")
    device_id = str(workspace.get("instance_id") or "").upper()
    device_name = str(workspace.get("machine") or "")
    episode = str(workspace.get("episode") or "").upper()
    if (not SAFE_DEVICE_RE.fullmatch(device_id) or not SAFE_DEVICE_RE.fullmatch(device_name) or
            not EPISODE_RE.fullmatch(episode)):
        raise BackupError("workspace.json has an invalid device or episode identity")
    expected_relative = PurePosixPath("devices") / f"{device_name}-{device_id}" / "episodes" / episode
    declared = str(workspace.get("instance_relative_path") or "").replace("\\", "/")
    if PurePosixPath(declared) != expected_relative:
        raise BackupError(f"workspace identity/path mismatch: {declared!r} != {expected_relative}")
    return workspace, device_name, device_id, episode


def collect_files(instance: Path) -> tuple[list[dict], list[str]]:
    files: list[dict] = []
    excluded: list[str] = []
    for path in sorted(instance.rglob("*")):
        rel = path.relative_to(instance)
        if path.is_symlink():
            raise BackupError(f"refusing to follow symlink in workspace: {rel}")
        if path.is_dir():
            if rel.parts and rel.parts[0] not in ALLOWED_EPISODE_DIRS:
                excluded.append(rel.as_posix() + "/")
            continue
        if not path.is_file():
            raise BackupError(f"workspace contains a non-regular entry: {rel}")
        if path.name in VOLATILE_NAMES or path.name.endswith(VOLATILE_SUFFIXES):
            excluded.append(rel.as_posix())
            continue
        if rel.parts and rel.parts[0] not in ALLOWED_EPISODE_DIRS and rel.name != "workspace.json":
            excluded.append(rel.as_posix())
            continue
        files.append({"path": rel.as_posix(), "size_bytes": path.stat().st_size,
                      "sha256": sha256_file(path)})
    if not any(row["path"] == "workspace.json" for row in files):
        raise BackupError("backup inventory omitted workspace.json")
    return files, excluded


def _ensure_outside_release(destination: Path, release_root: Path) -> Path:
    resolved = destination.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(release_root.resolve(strict=True))
    except ValueError:
        return resolved
    raise BackupError(f"backup destination must be outside the release tree: {resolved}")


def _write_archive(target_dir: Path, archive_name: str, inventory: dict,
                   source_root: Path, archive_prefix: str) -> Path:
    archive = target_dir / archive_name
    if archive.exists() or archive.is_symlink():
        raise BackupError(f"refusing to overwrite existing backup: {archive}")
    files = inventory["files"]
    temp = target_dir / f".{archive.name}.{os.getpid()}.{secrets.token_hex(6)}.partial"
    checksum_temp = target_dir / f".{archive.name}.{os.getpid()}.{secrets.token_hex(6)}.sha256.partial"
    try:
        with zipfile.ZipFile(temp, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("backup-manifest.json", json.dumps(inventory, ensure_ascii=False, indent=2) + "\n")
            for row in files:
                path = source_root / Path(*PurePosixPath(row["path"]).parts)
                zf.write(path, archive_prefix + row["path"])
        with temp.open("rb+") as stream:
            os.fsync(stream.fileno())
        verify_archive(temp, require_sidecar=False)
        archive_hash = sha256_file(temp)
        with checksum_temp.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{archive_hash}  {archive.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if archive.exists() or archive.with_suffix(archive.suffix + ".sha256").exists():
            raise BackupError(f"backup or checksum appeared during creation; refusing overwrite: {archive}")
        os.replace(temp, archive)
        os.replace(checksum_temp, archive.with_suffix(archive.suffix + ".sha256"))
    finally:
        temp.unlink(missing_ok=True)
        checksum_temp.unlink(missing_ok=True)
    verify_archive(archive)
    return archive


def create_backup(instance_root: str | Path, destination: str | Path,
                  release_root: str | Path, *, max_gib: float = DEFAULT_MAX_GIB) -> Path:
    raw_instance = Path(instance_root).expanduser()
    if raw_instance.is_symlink():
        raise BackupError(f"instance root may not be a symlink: {raw_instance}")
    instance = raw_instance.resolve(strict=True)
    release = Path(release_root).expanduser().resolve(strict=True)
    if instance.is_symlink() or not instance.is_dir():
        raise BackupError(f"instance root is missing or unsafe: {instance}")
    try:
        relative = instance.relative_to(release / "devices")
    except ValueError as exc:
        raise BackupError("instance root must be under this release's devices directory") from exc
    if len(relative.parts) != 3 or relative.parts[1] != "episodes":
        raise BackupError("select exactly one devices/<device>/episodes/<episode> workspace")
    workspace, device_name, device_id, episode = _read_workspace(instance)
    files, excluded = collect_files(instance)
    total = sum(row["size_bytes"] for row in files)
    if max_gib <= 0 or total > max_gib * 1024**3:
        raise BackupError(f"backup is {total / 1024**3:.2f} GiB; configured cap is {max_gib:.2f} GiB")
    target_dir = _ensure_outside_release(Path(destination), release)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = target_dir / f"{device_name}-{device_id}-{episode}-{stamp}.zip"
    if archive.exists() or archive.is_symlink():
        raise BackupError(f"refusing to overwrite existing backup: {archive}")
    inventory = {
        "schema": SCHEMA, "scope": "episode", "archive_prefix": "workspace/",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "device_name": device_name, "device_instance_id": device_id,
        "episode": episode, "instance_relative_path": relative.as_posix(),
        "workflow_version": workspace.get("workflow_version"),
        "files": files, "excluded_volatile_or_unscoped": excluded,
        "total_bytes": total,
    }
    archive = _write_archive(target_dir, archive.name, inventory, instance, "workspace/")
    print(f"verified backup: {archive} ({total} bytes, {len(files)} files)")
    return archive


def create_device_backup(device_root: str | Path, destination: str | Path,
                         release_root: str | Path, *, max_gib: float = DEFAULT_MAX_GIB) -> Path:
    """Back up all episode metadata/evidence for one device, plus its change notes/profile."""
    raw_device = Path(device_root).expanduser()
    if raw_device.is_symlink():
        raise BackupError(f"device root may not be a symlink: {raw_device}")
    device = raw_device.resolve(strict=True)
    release = Path(release_root).expanduser().resolve(strict=True)
    try:
        device.relative_to((release / "devices").resolve(strict=True))
    except ValueError as exc:
        raise BackupError("device root must be under this release's devices directory") from exc
    if (not device.is_dir() or not (device / "episodes").is_dir() or
            (device / "episodes").is_symlink()):
        raise BackupError(f"device root has no episodes directory: {device}")
    try:
        device_name, device_id = device.name.rsplit("-", 1)
    except ValueError as exc:
        raise BackupError("device directory name must end in -<8-char-instance-id>") from exc
    if not SAFE_DEVICE_RE.fullmatch(device_name) or not SAFE_DEVICE_RE.fullmatch(device_id):
        raise BackupError("device directory contains an unsafe name or instance id")

    files: list[dict] = []
    excluded: list[str] = []
    workspaces = []
    for workspace_path in sorted((device / "episodes").glob("*/workspace.json")):
        instance = workspace_path.parent
        workspace, found_name, found_id, episode = _read_workspace(instance)
        if found_name != device_name or found_id != device_id or instance.name.upper() != episode:
            raise BackupError(f"device/episode workspace identity mismatch: {workspace_path}")
        episode_files, episode_excluded = collect_files(instance)
        for row in episode_files:
            files.append({**row, "path": (Path("episodes") / episode / row["path"]).as_posix()})
        excluded.extend((Path("episodes") / episode / item).as_posix() for item in episode_excluded)
        workspaces.append({"episode": episode, "instance_relative_path": workspace["instance_relative_path"],
                           "workflow_version": workspace.get("workflow_version")})
    if not workspaces:
        raise BackupError("device root contains no episode workspace.json files")

    for path in sorted(device.iterdir()):
        if path.name == "episodes" or path.name in VOLATILE_NAMES:
            continue
        if path.is_symlink():
            raise BackupError(f"device-root entry may not be a symlink: {path}")
        if path.is_file() and path.name.startswith("WORKFLOW") and path.suffix.casefold() in (".md", ".txt"):
            files.append({"path": path.name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
        elif path.is_dir() and path.name.casefold() == "local device config":
            for config in sorted(path.rglob("*.json")):
                if config.is_symlink() or not config.is_file():
                    raise BackupError(f"device config entry is unsafe: {config}")
                rel = config.relative_to(device)
                files.append({"path": rel.as_posix(), "size_bytes": config.stat().st_size,
                              "sha256": sha256_file(config)})
        else:
            excluded.append(path.name + ("/" if path.is_dir() else ""))
    files.sort(key=lambda row: row["path"])
    if len({row["path"] for row in files}) != len(files):
        raise BackupError("device backup inventory contains duplicate relative paths")
    total = sum(row["size_bytes"] for row in files)
    if max_gib <= 0 or total > max_gib * 1024**3:
        raise BackupError(f"device backup is {total / 1024**3:.2f} GiB; configured cap is {max_gib:.2f} GiB")
    target_dir = _ensure_outside_release(Path(destination), release)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive_name = f"{device.name}-device-{stamp}.zip"
    inventory = {"schema": SCHEMA, "scope": "device", "archive_prefix": "device/",
                 "created_utc": datetime.now(timezone.utc).isoformat(),
                 "device_name": device_name, "device_instance_id": device_id,
                 "device_folder": device.name, "workspaces": workspaces,
                 "files": files, "excluded_volatile_or_unscoped": excluded,
                 "total_bytes": total}
    archive = _write_archive(target_dir, archive_name, inventory, device, "device/")
    print(f"verified device backup: {archive} ({total} bytes, {len(files)} files, {len(workspaces)} episodes)")
    return archive


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise BackupError(f"unsafe archive member path: {name!r}")
    if "\\" in name or ":" in name:
        raise BackupError(f"unsafe archive member path: {name!r}")
    return path


def verify_archive(archive_path: str | Path, *, require_sidecar: bool = True) -> dict:
    raw_archive = Path(archive_path).expanduser()
    if raw_archive.is_symlink():
        raise BackupError(f"backup ZIP may not be a symlink: {raw_archive}")
    archive = raw_archive.resolve(strict=True)
    if not zipfile.is_zipfile(archive):
        raise BackupError(f"not a regular workspace backup ZIP: {archive}")
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    if require_sidecar:
        if sidecar.is_symlink() or not sidecar.is_file():
            raise BackupError(f"backup SHA-256 sidecar is missing or unsafe: {sidecar}")
        try:
            expected, name = sidecar.read_text(encoding="ascii").strip().split(None, 1)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise BackupError(f"invalid backup SHA-256 sidecar: {exc}") from exc
        if name.strip() != archive.name or expected.lower() != sha256_file(archive):
            raise BackupError("backup archive SHA-256 does not match its sidecar")
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            if len(names) != len(set(names)):
                raise BackupError("backup ZIP contains duplicate member names")
            for name in names:
                _safe_member(name)
            try:
                inventory = json.loads(zf.read("backup-manifest.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupError(f"backup manifest is missing or invalid: {exc}") from exc
            if inventory.get("schema") != SCHEMA or not isinstance(inventory.get("files"), list):
                raise BackupError("unsupported backup manifest schema")
            prefix = inventory.get("archive_prefix")
            if (inventory.get("scope") not in ("episode", "device") or
                    prefix != ("workspace/" if inventory.get("scope") == "episode" else "device/")):
                raise BackupError("backup scope/prefix is invalid")
            device_name = str(inventory.get("device_name") or "")
            device_id = str(inventory.get("device_instance_id") or "").upper()
            if not SAFE_DEVICE_RE.fullmatch(device_name) or not SAFE_DEVICE_RE.fullmatch(device_id):
                raise BackupError("backup device identity is invalid")
            if inventory["scope"] == "episode" and not EPISODE_RE.fullmatch(str(inventory.get("episode", ""))):
                raise BackupError("backup episode identity is invalid")
            expected = {"backup-manifest.json"}
            seen_paths: set[str] = set()
            inventory_total = 0
            for row in inventory["files"]:
                relative_name = str(row.get("path", ""))
                rel = _safe_member(prefix + relative_name)
                name = rel.as_posix()
                if relative_name in seen_paths:
                    raise BackupError(f"backup manifest has duplicate file path: {relative_name}")
                seen_paths.add(relative_name)
                size = row.get("size_bytes")
                if type(size) is not int or size < 0:
                    raise BackupError(f"backup manifest has invalid file size: {relative_name}")
                inventory_total += size
                rel_parts = PurePosixPath(relative_name).parts
                if inventory["scope"] == "episode":
                    if not (relative_name == "workspace.json" or
                            (rel_parts and rel_parts[0] in ALLOWED_EPISODE_DIRS)):
                        raise BackupError(f"episode backup contains an unscoped path: {relative_name}")
                elif not (rel_parts and rel_parts[0] == "episodes" or
                          relative_name.startswith("local device config/") or
                          (len(rel_parts) == 1 and rel_parts[0].startswith("WORKFLOW"))):
                    raise BackupError(f"device backup contains an unscoped path: {relative_name}")
                expected.add(name)
                try:
                    content = zf.read(name)
                except KeyError as exc:
                    raise BackupError(f"backup member is missing: {name}") from exc
                if len(content) != size:
                    raise BackupError(f"backup member has incorrect size: {name}")
                if hashlib.sha256(content).hexdigest() != row.get("sha256"):
                    raise BackupError(f"backup member SHA-256 mismatch: {name}")
            if set(names) != expected:
                raise BackupError("backup ZIP contains unlisted or unexpected members")
            if inventory_total != inventory.get("total_bytes"):
                raise BackupError("backup manifest total byte count does not match its file inventory")
            if inventory["scope"] == "device":
                workspaces = inventory.get("workspaces")
                if not isinstance(workspaces, list) or not workspaces:
                    raise BackupError("device backup has no episode workspace inventory")
                episodes = [str(row.get("episode", "")).upper()
                            for row in workspaces if isinstance(row, dict)]
                if (len(episodes) != len(workspaces) or
                        any(not EPISODE_RE.fullmatch(code) for code in episodes) or
                        len(episodes) != len(set(episodes))):
                    raise BackupError("device backup episode inventory is malformed or duplicated")
            return inventory
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"cannot verify backup {archive}: {exc}") from exc


def restore_backup(archive_path: str | Path, devices_root: str | Path, *, apply: bool) -> Path:
    inventory = verify_archive(archive_path)
    raw_devices = Path(devices_root).expanduser()
    if raw_devices.is_symlink():
        raise BackupError("--devices-root may not be a symlink")
    devices = raw_devices.resolve(strict=True)
    if devices.is_symlink() or not devices.is_dir() or devices.name != "devices":
        raise BackupError("--devices-root must be the existing release devices/ directory")
    device_dir = devices / f"{inventory['device_name']}-{inventory['device_instance_id']}"
    if device_dir.is_symlink():
        raise BackupError(f"restore device directory may not be a symlink: {device_dir}")
    episodes_dir = device_dir / "episodes"
    if episodes_dir.is_symlink():
        raise BackupError(f"restore episodes directory may not be a symlink: {episodes_dir}")
    destination = (device_dir / "episodes" / inventory["episode"]
                   if inventory["scope"] == "episode" else device_dir)
    if destination.exists() or destination.is_symlink():
        raise BackupError(f"restore destination already exists; never merge/overwrite workspaces: {destination}")
    if not apply:
        print(f"DRY RUN: verified {len(inventory['files'])} files ({inventory['total_bytes']} bytes)")
        print(f"would restore to: {destination}")
        return destination
    if inventory["scope"] == "episode":
        device_dir.mkdir(parents=True, exist_ok=True)
    else:
        device_dir.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=False)
    archive = Path(archive_path).expanduser().resolve(strict=True)
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            for row in inventory["files"]:
                rel = _safe_member(str(row["path"]))
                target = destination.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    content = zf.read(inventory["archive_prefix"] + rel.as_posix())
                    stream.write(content)
                row_by_path = {item["path"]: item for item in inventory["files"]}
                expected = row_by_path[rel.as_posix()]
                if target.stat().st_size != expected["size_bytes"] or sha256_file(target) != expected["sha256"]:
                    raise BackupError(f"restored file failed size/SHA-256 verification: {target}")
        if inventory["scope"] == "episode":
            restored = _read_workspace(destination)
            if restored[1:] != (inventory["device_name"], inventory["device_instance_id"], inventory["episode"]):
                raise BackupError("restored workspace identity does not match backup manifest")
        else:
            for row in inventory.get("workspaces", []):
                episode_path = destination / "episodes" / str(row["episode"])
                restored = _read_workspace(episode_path)
                if restored[1:] != (inventory["device_name"], inventory["device_instance_id"], row["episode"]):
                    raise BackupError(f"restored workspace identity mismatch: {episode_path}")
        verify_archive(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"restore stopped part-way; preserve the partial destination for review: {exc}") from exc
    print(f"restored verified workspace to: {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    scope = backup.add_mutually_exclusive_group(required=True)
    scope.add_argument("--instance-root", help="back up one episodes/<episode> workspace")
    scope.add_argument("--device-root", help="back up every episode and device change record")
    backup.add_argument("--destination", required=True, help="backup folder outside the shared release")
    backup.add_argument("--release-root", required=True)
    backup.add_argument("--max-gib", type=float, default=DEFAULT_MAX_GIB)
    verify = sub.add_parser("verify")
    verify.add_argument("--archive", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--devices-root", required=True)
    restore.add_argument("--apply", action="store_true", help="write only to the exact identity's new empty workspace")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            if args.instance_root:
                create_backup(args.instance_root, args.destination, args.release_root, max_gib=args.max_gib)
            else:
                create_device_backup(args.device_root, args.destination, args.release_root,
                                     max_gib=args.max_gib)
        elif args.command == "verify":
            inventory = verify_archive(args.archive)
            print(f"PASS: {len(inventory['files'])} files, {inventory['total_bytes']} bytes; identity="
                  f"{inventory['device_name']}-{inventory['device_instance_id']}:{inventory['episode']}")
        else:
            restore_backup(args.archive, args.devices_root, apply=args.apply)
        return 0
    except (BackupError, OSError, ValueError) as exc:
        print(f"DEVICE WORKSPACE BACKUP ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
