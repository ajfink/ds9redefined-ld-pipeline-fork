#!/usr/bin/env python3
"""Create and populate isolated per-instance data folders under a shared release.

The release root is never an output target. The only mutation performed here is
under its pre-provisioned ``devices/`` subtree: unique instance folders,
logs/references, and reviewed cut-PAD stills. The OS ACL remains an administrator
responsibility; this helper does not edit permissions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)
IMAGE_RE = re.compile(r"^DVD-(\d{2})-(\d{2})-(\d{2})\.(\d{3})-f(\d{8})\.png$", re.IGNORECASE)
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
REFERENCE_EXTENSIONS = {".md", ".txt", ".json", ".csv", ".tsv", ".log"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dependency_lock import LockError, redact_shared_text, shared_redactions  # noqa: E402


def load_profile(path: str | None) -> tuple[Path, dict]:
    if not path:
        path = os.environ.get("LDP_PROFILE")
    if not path:
        raise LockError("shared workspace requires --profile or LDP_PROFILE pointing to a local machine.json")
    profile_path = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    if not profile_path.is_file():
        raise LockError(f"local profile not found: {profile_path}")
    if profile_path.is_relative_to(ROOT.resolve()):
        raise LockError("machine.json must be outside the shared release folder; profiles are device-local")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read local profile {profile_path}: {exc}") from exc
    shared = profile.get("shared") or {}
    if shared.get("enabled") is not True:
        raise LockError("set shared.enabled=true in the local profile after the shared root/ACL is configured")
    release_value = shared.get("release_root")
    if not release_value:
        raise LockError("shared.release_root must be set to the shared ld-pipeline directory")
    release = Path(os.path.expandvars(os.path.expanduser(str(release_value)))).resolve()
    if release != ROOT.resolve():
        raise LockError(f"shared.release_root ({release}) must match this code bundle ({ROOT})")
    devices = release / "devices"
    if not devices.is_dir():
        raise LockError(f"shared devices/ directory must be provisioned by the release administrator: {devices}")
    return profile_path, profile


def configure_shared_profile(source_value: str, output_value: str,
                             device_name: str | None) -> Path:
    """Create a new device-local profile that points at this read-only release."""
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
    profile["shared"] = {"enabled": True, "release_root": str(ROOT.resolve()),
                         "device_name": device_name or profile.get("machine") or "device"}
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
        "Shared-release profile: source files are read-only. Initialize a unique per-episode "
        "workspace under shared devices/ and pass --instance-root to run_stage.py.")
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


def create_workspace(profile_path: Path, profile: dict, episode: str) -> dict:
    if not EPISODE_RE.fullmatch(episode):
        raise LockError(f"episode must be SxxExx, got {episode!r}")
    shared = profile["shared"]
    devices = Path(shared["release_root"]).resolve() / "devices"
    device = safe_component(str(shared.get("device_name") or profile.get("machine") or "device"))
    instance_id = secrets.token_hex(4).upper()
    instance_name = f"{device}-{instance_id}"
    device_instance = devices / instance_name
    try:
        # mkdir without parents/exist_ok is the collision-safe reservation. Do
        # not create the shared-root devices/ parent from an ordinary run.
        device_instance.mkdir()
    except FileExistsError:
        return create_workspace(profile_path, profile, episode)
    episode_root = device_instance / "episodes" / episode.upper()
    (device_instance / "episodes").mkdir()
    episode_root.mkdir()
    dirs = {
        "manifest_dir": episode_root / "manifest",
        "logs": episode_root / "logs",
        "references": episode_root / "references",
        "cut_pad_images": episode_root / "cut-pad-images",
    }
    for path in dirs.values():
        path.mkdir()
    metadata = {
        "schema": "ld-pipeline-shared-instance-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": episode.upper(),
        "machine": profile.get("machine"),
        "instance_id": instance_id,
        "profile_name": profile_path.name,
        "release_root": ".",
        "instance_relative_path": episode_root.resolve().relative_to(ROOT.resolve()).as_posix(),
        "write_scope": "this unique per-instance episode folder only",
        "environment": {
            "LDP_INSTANCE_ROOT": ".",
            "LDP_MANIFEST_DIR": "manifest",
            "LDP_INSTANCE_LOGS": "logs",
            "LDP_CUT_PAD_IMAGES": "cut-pad-images",
        },
    }
    with (episode_root / "workspace.json").open("x", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
    # Show the caller's device-local profile only in this command's output; do
    # not put that private path into the shared workspace metadata.
    metadata["instance_root"] = str(episode_root.resolve())
    metadata["shell_exports"] = {
        "LDP_PROFILE": str(profile_path),
        "LDP_INSTANCE_ROOT": str(episode_root.resolve()),
        "LDP_MANIFEST_DIR": str(dirs["manifest_dir"].resolve()),
        "LDP_INSTANCE_LOGS": str(dirs["logs"].resolve()),
        "LDP_CUT_PAD_IMAGES": str(dirs["cut_pad_images"].resolve()),
    }
    return metadata


def validate_instance(root_value: str, profile_path: Path, profile: dict) -> Path:
    root = Path(os.path.expandvars(os.path.expanduser(root_value))).resolve()
    devices = Path(profile["shared"]["release_root"]).resolve() / "devices"
    try:
        relative = root.relative_to(devices.resolve())
    except ValueError as exc:
        raise LockError(f"instance root must be under {devices}: {root}") from exc
    if len(relative.parts) != 3 or relative.parts[1] != "episodes":
        raise LockError("instance root must have form devices/<device-instance>/episodes/<episode>")
    device = safe_component(str(profile.get("shared", {}).get("device_name") or
                                profile.get("machine") or "device"))
    if not relative.parts[0].startswith(device + "-"):
        raise LockError("instance root belongs to a different device label")
    try:
        metadata = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"invalid/missing workspace.json at {root}: {exc}") from exc
    expected_relative = root.relative_to(ROOT.resolve()).as_posix()
    if (metadata.get("release_root") != "." or
            metadata.get("instance_relative_path") != expected_relative or
            metadata.get("machine") != profile.get("machine") or
            str(metadata.get("episode", "")).casefold() != root.name.casefold() or
            relative.parts[0].rsplit("-", 1)[-1] != metadata.get("instance_id")):
        raise LockError("workspace metadata does not match this release bundle")
    if not root.is_dir():
        raise LockError(f"instance directory is missing: {root}")
    return root


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


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
                    mapping_verified: bool) -> tuple[Path, Path]:
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
    record = {
        "schema": "ld-pipeline-cut-pad-provenance-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": instance.name,
        "output_pad_frame": output_frame,
        "dvd_timestamp": f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}",
        "dvd_frame": int(match.group(5)),
        "source_dvd": source_dvd or "user-supplied verified DVD",
        "source_dvd_sha256": dvd_sha256,
        "png_sha256": sha256(image),
        "reviewed_episode_scene_and_pad_mapping": True,
        "source_filename": source.name,
    }
    with provenance.open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    return image, provenance


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create one unique device-instance/episode workspace")
    init.add_argument("--profile", required=True)
    init.add_argument("--episode", required=True)
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
        if args.command == "init":
            result = create_workspace(profile_path, profile, args.episode)
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
                    args.source_dvd, args.source_dvd_sha256, args.confirm_mapping)
                print(f"published cut-PAD image: {image}\nprovenance: {provenance}")
        return 0
    except (LockError, OSError, json.JSONDecodeError) as exc:
        print(f"SHARED WORKSPACE ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
