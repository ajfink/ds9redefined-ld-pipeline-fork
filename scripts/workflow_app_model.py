#!/usr/bin/env python3
"""Safe, testable intake-plan generation for the local workflow GUI."""
from __future__ import annotations

import copy
import csv
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


def resolve_local_source(path_value: str | Path) -> dict[str, Any]:
    """Inspect a local rip without requiring an Archive download manifest."""
    from local_source import LocalSourceError, inspect_local_source
    try:
        return inspect_local_source(path_value)
    except LocalSourceError as exc:
        raise WorkflowAppError(str(exc)) from exc


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

    source_mode = str(values.get("source_mode", "archive-url")).strip().casefold()
    if source_mode not in ("archive-url", "local"):
        raise WorkflowAppError("source_mode must be 'archive-url' or 'local'")
    required = ["profile", "confirmed_title", "dvd_mode", "cut_pad_images",
                "manifest_path", "dvd_path"]
    if source_mode == "archive-url":
        required.extend(("episode", "archive_url", "downloads_dir"))
    else:
        required.append("local_source_path")
    missing = [key for key in required if not str(values.get(key, "")).strip()]
    if missing:
        raise WorkflowAppError("complete these fields: " + ", ".join(missing))

    local_info = resolve_local_source(values["local_source_path"]) if source_mode == "local" else None
    episode = (local_info["episode"] if local_info else values["episode"].strip().upper())
    if not EPISODE_RE.fullmatch(episode):
        raise WorkflowAppError("episode must use the SxxExx format")
    if local_info and values.get("episode") and values["episode"].strip().upper() != episode:
        raise WorkflowAppError("local source ArchiveId episode does not match the detected episode")
    title = values["confirmed_title"].strip()
    archive_url = values["archive_url"].strip() if not local_info else "not-applicable"
    if not local_info:
        parsed_url = urlparse(archive_url)
        if (parsed_url.scheme != "https" or parsed_url.hostname not in ("archive.org", "www.archive.org") or
                not parsed_url.path.startswith(("/details/", "/download/"))):
            raise WorkflowAppError("archive URL must be an HTTPS archive.org/details/<item> URL")

    profile_path = _absolute(values["profile"], "profile")
    downloads = (Path(local_info["source_dir"]).resolve() if local_info else
                 _absolute(values["downloads_dir"], "downloads_dir"))
    if local_info:
        try:
            profile_data = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"cannot read selected machine profile: {exc}") from exc
        if (profile_data.get("shared") or {}).get("enabled") is True:
            roots = profile_data.get("roots") or {}
            download_root = roots.get("downloads_root")
            download_template = roots.get("downloads_template") or "DS9 {episode} LaserDisc"
            if not download_root:
                raise WorkflowAppError("shared-profile local intake requires roots.downloads_root in the machine profile")
            try:
                expected_downloads = (Path(download_root).expanduser() /
                                      download_template.format(episode=episode)).resolve()
            except (ValueError, KeyError) as exc:
                raise WorkflowAppError(f"invalid profile download folder template: {exc}") from exc
            if downloads.resolve() != expected_downloads:
                raise WorkflowAppError(
                    f"shared-profile local source must be in its configured downloads folder: {expected_downloads}")
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
        "profile": str(profile_path),
        "archive_url": archive_url,
        "downloads_dir": str(downloads),
        "source_mode": source_mode,
        "local_source_path": local_info["selected_path"] if local_info else "not-applicable",
        "fingerprint_policy": policy,
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
    _set_decision(decisions, "source-mode", source_mode)
    if local_info:
        _set_decision(decisions, "archive-item-confirmed", "not supplied; local inventory provenance")
        _set_decision(decisions, "local-source", local_info["selected_path"])
    else:
        _set_decision(decisions, "archive-item-confirmed", archive_url)
        decisions[:] = [row for row in decisions if row.get("id") != "local-source"]
    if dvd_mode == "dvd-backed":
        _set_decision(decisions, "dvd-reference", "${input:dvd_path}")
    else:
        decisions[:] = [row for row in decisions if row.get("id") != "dvd-reference"]

    steps = plan.get("steps")
    if not isinstance(steps, list) or [step.get("stage") for step in steps] != [
            "download", "init-episode", "validate-inputs"]:
        raise WorkflowAppError("GUI intake currently supports download/init/validate templates")
    download_step, init_step, validate_step = steps
    init_args = (["--local-source", "${input:local_source_path}",
                  "--fingerprint-policy", "${input:fingerprint_policy}",
                  "--confirmed-title", "${input:confirmed_title}"] if local_info else
                 ["--url", "${input:archive_url}", "--downloads", "${input:downloads_dir}"])
    init_inputs = ["${input:downloads_dir}"]
    validate_inputs = ["${input:downloads_dir}", "${input:manifest_path}"]
    if local_info:
        for evidence in (local_info["source_dir"], local_info["selected_path"]):
            if Path(evidence).resolve() != downloads.resolve():
                init_inputs.append(evidence)
                validate_inputs.append(evidence)
    if dvd_mode == "dvd-backed":
        init_args.extend(("--dvd", "${input:dvd_path}"))
        init_inputs.append("${input:dvd_path}")
        validate_inputs.append("${input:dvd_path}")
    else:
        init_args.extend(("--no-dvd", "--cut-pad-images", "${input:cut_pad_images}"))
    init_args.extend(("--manifest-out", "${input:manifest_path}"))

    if local_info:
        init_step["id"] = "initialize-local-episode"
        init_step["stage"] = "init-local-episode"
        init_step["outputs"] = ["${input:manifest_path}"]
        plan["steps"] = [init_step, validate_step]
    else:
        download_step["args"] = ["-Url", "${input:archive_url}", "-Destination", "${input:downloads_dir}"]
        download_step["inputs"] = []
        download_step["outputs"] = ["${input:downloads_dir}"]
    init_step["args"] = init_args
    init_step["inputs"] = init_inputs
    if not local_info:
        init_step["outputs"] = ["${input:manifest_path}"]
    validate_step["inputs"] = validate_inputs
    validate_step["outputs"] = []
    if local_info:
        plan.setdefault("_instructions_DO_NOT_EDIT", []).append(
            "Local intake skips download and creates a clearly labeled local inventory. Archive origin/authenticity "
            "is not asserted; inspect the selected capture set and the inventory evidence before proceeding.")
        for instruction_index, instruction in enumerate(plan.get("_instructions_DO_NOT_EDIT", [])):
            if isinstance(instruction, str) and "This is an S01E07, no-DVD example." in instruction:
                plan["_instructions_DO_NOT_EDIT"][instruction_index] = (
                    "This plan was generated from an existing local source selection; confirm its inferred episode identity "
                    "and local-only provenance before processing.")
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
