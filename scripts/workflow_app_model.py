#!/usr/bin/env python3
"""Safe, testable intake-plan generation for the local workflow GUI."""
from __future__ import annotations

import copy
import json
import os
import platform
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from typing import Any

PLAN_SCHEMA = "ld-pipeline-workflow-plan-v1"
EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)
EPISODE_SEARCH_RE = re.compile(r"S\d{2}E\d{2}", re.IGNORECASE)


class WorkflowAppError(ValueError):
    """Invalid GUI input or an unsupported intake template."""


def default_profile_path(*, system: str | None = None,
                         env: dict[str, str] | None = None,
                         home: Path | None = None) -> Path:
    """Portable per-user profile location shared by the GUI and CLI workflows."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    system = (system or platform.system()).casefold()
    if system in ("windows", "win32"):
        root = Path(env.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    elif system in ("darwin", "macos"):
        root = Path(env.get("HOME") or home) / "Library" / "Application Support"
    else:
        root = Path(env.get("XDG_CONFIG_HOME") or (Path(env.get("HOME") or home) / ".config"))
    return (root / "ld-pipeline" / "profiles" / "machine.json").resolve(strict=False)


def choose_existing_profile(default_path: Path, template: dict[str, Any], *,
                            hostname: str | None = None) -> Path:
    """Use the default profile, or a unique/host-matching legacy profile in its folder."""
    default_path = default_path.expanduser().resolve(strict=False)
    if default_path.is_file():
        return default_path
    operator = template.get("_operator_inputs_EDIT") or {}
    hint = operator.get("profile")
    hint_value = hint.get("value") if isinstance(hint, dict) else hint
    if isinstance(hint_value, str) and hint_value and "REPLACE_" not in hint_value:
        hinted = Path(os.path.expandvars(os.path.expanduser(hint_value)))
        if hinted.is_absolute() and hinted.is_file() and hinted.parent.resolve() == default_path.parent:
            return hinted.resolve()

    candidates = []
    for candidate in default_path.parent.glob("machine*.json"):
        if ".backup-" in candidate.name or not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        candidates.append((candidate.resolve(), str(data.get("machine", ""))))
    if len(candidates) == 1:
        return candidates[0][0]
    host = (hostname or socket.gethostname()).casefold()
    matching = [path for path, machine in candidates if machine.casefold() == host]
    if len(matching) == 1:
        return matching[0]
    if candidates:
        raise WorkflowAppError(
            f"multiple machine profiles exist in the default profile folder {default_path.parent}; "
            "rename the intended one to machine.json or correct its machine name")
    return default_path


def create_default_profile(default_path: Path, bundle_root: Path, *,
                           system: str | None = None,
                           hostname: str | None = None) -> tuple[Path, bool]:
    """Ensure a per-user profile exists at the selected default path, without overwriting."""
    path = default_path.expanduser().resolve(strict=False)
    if path.is_file():
        try:
            profile = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"existing machine profile is invalid and was not changed: {path}: {exc}") from exc
        if not isinstance(profile, dict):
            raise WorkflowAppError(f"existing machine profile must be a JSON object: {path}")
        return path, False
    return create_profile_from_template(path, bundle_root, system=system,
                                        hostname=hostname), True


def create_profile_from_template(path: Path, bundle_root: Path, *,
                                 system: str | None = None,
                                 hostname: str | None = None) -> Path:
    """Create a new local profile once; never replace an existing profile."""
    path = path.expanduser().resolve(strict=False)
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"existing machine profile is invalid and was not changed: {path}: {exc}") from exc
        return path
    system = (system or platform.system()).casefold()
    target_os = "windows" if system in ("windows", "win32") else "darwin" if system in ("darwin", "macos") else "linux"
    template_name = {
        "windows": "machine.example.json",
        "darwin": "machine-macos.example.json",
        "linux": "machine-linux-x86_64.example.json",
    }[target_os]
    template_path = Path(bundle_root) / template_name
    try:
        profile = json.loads(template_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cannot read portable machine profile template {template_path}: {exc}") from exc
    host = hostname or socket.gethostname()
    profile["machine"] = host
    profile["os"] = target_os
    if (Path(bundle_root) / "devices").is_dir():
        profile["shared"] = {"enabled": True, "release_root": str(Path(bundle_root).resolve()),
                             "device_name": host}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        return path
    except OSError as exc:
        raise WorkflowAppError(f"could not create default machine profile {path}: {exc}") from exc
    return path


def archive_item_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in ("archive.org", "www.archive.org"):
        raise WorkflowAppError("Archive URL must use HTTPS on archive.org")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        raise WorkflowAppError("Archive URL must identify an item under /details/ or /download/")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", parts[1]):
        raise WorkflowAppError(f"unexpected Archive item identifier: {parts[1]!r}")
    return parts[1]


def episode_code_from_url_identifier(url: str) -> str | None:
    """Fast path: infer the episode from an Archive item ID without network access."""
    item = archive_item_id(url)
    found = {match.upper() for match in EPISODE_SEARCH_RE.findall(item)}
    if len(found) > 1:
        raise WorkflowAppError(f"Archive item identifier contains multiple episode codes: {sorted(found)}")
    return next(iter(found), None)


def episode_code_in_archive_url(url: str) -> str | None:
    """Return an unambiguous SxxExx code embedded in the Archive item identifier."""
    return episode_code_from_url_identifier(url)


def resolve_episode_code(url: str, metadata_reader=None) -> tuple[str, str]:
    """Resolve SxxExx from the item identifier, then Archive metadata/file names."""
    item = archive_item_id(url)
    direct = episode_code_in_archive_url(url)
    if direct:
        return direct, "Archive item identifier"
    if metadata_reader is None:
        def metadata_reader(item_id: str) -> dict[str, Any]:
            request = Request(f"https://archive.org/metadata/{item_id}",
                              headers={"User-Agent": "FromStunToKill-LaserDisc-Workflow"})
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
    try:
        metadata = metadata_reader(item)
    except Exception as exc:
        raise WorkflowAppError(f"could not resolve episode code from Archive metadata: {exc}") from exc
    info = metadata.get("metadata") if isinstance(metadata, dict) else None
    if not isinstance(info, dict):
        raise WorkflowAppError("Archive metadata response has no metadata object")
    texts = [str(info.get(key, "")) for key in ("identifier", "title", "description")]
    files = metadata.get("files") or []
    if isinstance(files, list):
        texts.extend(str(row.get("name", "")) for row in files if isinstance(row, dict))
    found = {match.upper() for text in texts for match in EPISODE_SEARCH_RE.findall(text)}
    if len(found) != 1:
        detail = "no episode code was found" if not found else f"multiple episode codes were found: {sorted(found)}"
        raise WorkflowAppError(f"cannot uniquely determine episode code from Archive item {item!r}: {detail}")
    return next(iter(found)), "Archive metadata"


def read_intake_template(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cannot read intake plan/template {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != PLAN_SCHEMA:
        raise WorkflowAppError(f"template schema must be {PLAN_SCHEMA!r}")
    if not isinstance(data.get("_operator_inputs_EDIT"), dict):
        raise WorkflowAppError("intake template needs an _operator_inputs_EDIT object")
    return data


def _absolute(value: str, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowAppError(f"{field} is required")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise WorkflowAppError(f"{field} must be an absolute path")
    if path.is_symlink():
        raise WorkflowAppError(f"{field} must not be a symbolic link")
    return path.resolve(strict=False)


def _set_decision(decisions: list[dict[str, Any]], decision_id: str, value: str) -> None:
    for decision in decisions:
        if decision.get("id") == decision_id:
            decision["value"] = value
            return
    decisions.append({"id": decision_id, "value": value})


def build_intake_plan(template: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Build a new no-overwrite intake plan; never mutate the loaded template."""
    plan = copy.deepcopy(template)
    operator_inputs = plan.get("_operator_inputs_EDIT")
    if not isinstance(operator_inputs, dict):
        raise WorkflowAppError("intake template needs an _operator_inputs_EDIT object")

    required = ("episode", "profile", "archive_url", "downloads_dir", "confirmed_title",
                "dvd_mode", "cut_pad_images", "manifest_path", "dvd_path")
    missing = [key for key in required if not str(values.get(key, "")).strip()]
    if missing:
        raise WorkflowAppError("complete these fields: " + ", ".join(missing))

    episode = values["episode"].strip().upper()
    if not EPISODE_RE.fullmatch(episode):
        raise WorkflowAppError("episode must use the SxxExx format")
    title = values["confirmed_title"].strip()
    archive_url = values["archive_url"].strip()
    parsed_url = urlparse(archive_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in ("archive.org", "www.archive.org") or not parsed_url.path.startswith("/details/"):
        raise WorkflowAppError("archive URL must be an HTTPS archive.org/details/<item> URL")

    _absolute(values["profile"], "profile")
    downloads = _absolute(values["downloads_dir"], "downloads_dir")
    manifest = _absolute(values["manifest_path"], "manifest_path")
    if manifest.name.casefold() != f"{episode}.json".casefold():
        raise WorkflowAppError(f"manifest_path filename must be {episode}.json")
    try:
        manifest.relative_to(downloads)
    except ValueError:
        pass
    else:
        raise WorkflowAppError("manifest_path must not be inside downloads_dir")
    if manifest.exists():
        raise WorkflowAppError(f"refusing to target an existing manifest: {manifest}")

    dvd_mode = values["dvd_mode"].strip().casefold()
    if dvd_mode not in ("dvd-backed", "no-dvd"):
        raise WorkflowAppError("dvd_mode must be 'dvd-backed' or 'no-dvd'")
    cut_pad_choice = values["cut_pad_images"].strip().casefold()
    dvd_path_value = values["dvd_path"].strip()
    if dvd_mode == "dvd-backed":
        dvd_path = _absolute(dvd_path_value, "dvd_path")
        if not dvd_path.is_file():
            raise WorkflowAppError(f"selected DVD file does not exist: {dvd_path}")
        cut_pad_choice = "not-applicable"
        dvd_path_value = str(dvd_path)
    else:
        if cut_pad_choice not in ("use", "skip"):
            raise WorkflowAppError("no-DVD mode requires cut_pad_images to be 'use' or 'skip'")
        dvd_path_value = "not-applicable"

    policy = values.get("fingerprint_policy", plan.get("fingerprint_policy", "balanced"))
    if policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint_policy must be 'balanced' or 'full'")

    editable_values = {
        "episode": episode,
        "profile": str(_absolute(values["profile"], "profile")),
        "archive_url": archive_url,
        "downloads_dir": str(downloads),
        "confirmed_title": title,
        "dvd_mode": dvd_mode,
        "cut_pad_images": cut_pad_choice,
        "manifest_path": str(manifest),
        "dvd_path": dvd_path_value,
    }
    for key, value in editable_values.items():
        previous = operator_inputs.get(key)
        comment = previous.get("_comment", "Set in the local workflow app.") if isinstance(previous, dict) else "Set in the local workflow app."
        operator_inputs[key] = {"value": value, "_comment": comment}

    plan["fingerprint_policy"] = policy
    instance_root_value = str(values.get("instance_root", "")).strip()
    if instance_root_value:
        plan["instance_root"] = str(_absolute(instance_root_value, "instance_root"))
    else:
        plan.pop("instance_root", None)
    decisions = plan.get("decisions")
    if not isinstance(decisions, list):
        raise WorkflowAppError("template decisions must be an array")
    _set_decision(decisions, "dvd-mode", dvd_mode)
    _set_decision(decisions, "cut-pad-images", cut_pad_choice)
    if dvd_mode == "dvd-backed":
        _set_decision(decisions, "dvd-reference", "${input:dvd_path}")
    else:
        decisions[:] = [row for row in decisions if row.get("id") != "dvd-reference"]

    steps = plan.get("steps")
    if not isinstance(steps, list) or [step.get("stage") for step in steps] != [
            "download", "init-episode", "validate-inputs"]:
        raise WorkflowAppError("GUI intake currently supports only download, init-episode, validate-inputs plans")
    download_step, init_step, validate_step = steps
    init_args = ["--url", "${input:archive_url}", "--downloads", "${input:downloads_dir}"]
    init_inputs = ["${input:downloads_dir}"]
    validate_inputs = ["${input:downloads_dir}", "${input:manifest_path}"]
    if dvd_mode == "dvd-backed":
        init_args.extend(("--dvd", "${input:dvd_path}"))
        init_inputs.append("${input:dvd_path}")
        validate_inputs.append("${input:dvd_path}")
    else:
        init_args.extend(("--no-dvd", "--cut-pad-images", "${input:cut_pad_images}"))
    init_args.extend(("--manifest-out", "${input:manifest_path}"))

    download_step["args"] = ["-Url", "${input:archive_url}", "-Destination", "${input:downloads_dir}"]
    download_step["inputs"] = []
    download_step["outputs"] = ["${input:downloads_dir}"]
    init_step["args"] = init_args
    init_step["inputs"] = init_inputs
    init_step["outputs"] = ["${input:manifest_path}"]
    validate_step["inputs"] = validate_inputs
    validate_step["outputs"] = []
    for instruction_index, instruction in enumerate(plan.get("_instructions_DO_NOT_EDIT", [])):
        if isinstance(instruction, str) and "initialize command is hardcoded to --no-dvd" in instruction:
            plan["_instructions_DO_NOT_EDIT"][instruction_index] = (
                "The workflow app generates an initialize command that matches the selected DVD mode; "
                "review the generated plan before execution.")
    return plan


def write_new_plan(path: Path, plan: dict[str, Any]) -> Path:
    target = path.expanduser().resolve(strict=False)
    if target.exists():
        raise WorkflowAppError(f"refusing to overwrite existing plan: {target}")
    if not target.parent.is_dir():
        raise WorkflowAppError(f"plan destination folder does not exist: {target.parent}")
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise WorkflowAppError(f"cannot save workflow plan {target}: {exc}") from exc
    return target
