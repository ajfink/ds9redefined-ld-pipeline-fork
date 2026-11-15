#!/usr/bin/env python3
"""Safe, testable intake-plan generation for the local workflow GUI."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

PLAN_SCHEMA = "ld-pipeline-workflow-plan-v1"
EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)


class WorkflowAppError(ValueError):
    """Invalid GUI input or an unsupported intake template."""


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
