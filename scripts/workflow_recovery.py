#!/usr/bin/env python3
"""Locate immutable plans for incomplete jobs in the current device workspace."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATE_SCHEMA = "ld-pipeline-workflow-state-v2"


def _episode_instances(device_root: Path) -> Iterator[Path]:
    """Inspect only direct DS9 or series/episode workspace levels, never media trees."""
    episodes = device_root / "episodes"
    if not episodes.is_dir() or episodes.is_symlink():
        return
    try:
        top_level = sorted(episodes.iterdir())
    except OSError:
        return
    for first in top_level:
        if first.is_symlink() or not first.is_dir():
            continue
        if ((first / "workspace.json").is_file() or (first / "manifest").is_dir() or
                ((first / "logs").is_dir() and (first / "plans").is_dir())):
            yield first
            continue
        try:
            nested = sorted(first.iterdir())
        except OSError:
            continue
        for second in nested:
            if (second.is_symlink() or not second.is_dir()):
                continue
            if ((second / "workspace.json").is_file() or (second / "manifest").is_dir() or
                    ((second / "logs").is_dir() and (second / "plans").is_dir())):
                yield second


def _saved_plans(device_root: Path) -> Iterator[Path]:
    seen: set[Path] = set()
    plan_directories = [device_root / "plans"]
    plan_directories.extend(instance / "plans" for instance in _episode_instances(device_root))
    for directory in plan_directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve(strict=False)
            if resolved not in seen:
                seen.add(resolved)
                yield path


def _workflow_states(device_root: Path) -> Iterator[Path]:
    for instance in _episode_instances(device_root):
        logs = instance / "logs"
        if not logs.is_dir() or logs.is_symlink():
            continue
        try:
            paths = sorted(logs.glob("workflow-*.json"))
        except OSError:
            continue
        yield from (path for path in paths if not path.is_symlink() and path.is_file())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _last_completed_step(state: dict[str, Any]) -> tuple[str, str]:
    steps = state.get("steps")
    if not isinstance(steps, list):
        return "No completed step", ""
    completed = [(index, row) for index, row in enumerate(steps)
                 if isinstance(row, dict) and row.get("status") == "PASS"]
    if not completed:
        return "No completed step", ""
    _index, row = completed[-1]
    step_name = str(row.get("stage") or row.get("id") or "Completed step")
    timestamp = str(row.get("finished_utc") or
                    (row.get("approval") or {}).get("approved_at_utc") or "")
    return step_name, timestamp


def _timestamp_key(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return 0.0


def _find_plan(plans: list[tuple[Path, bytes, dict[str, Any]]],
               state: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    digest = state.get("plan_sha256")
    job_id = state.get("job_id")
    episode = str(state.get("episode", "")).upper()
    series = str(state.get("series", "DS9")).upper()
    for path, raw, plan in plans:
        if hashlib.sha256(raw).hexdigest() != digest:
            continue
        if (str(plan.get("episode", "")).upper() != episode or
                str(plan.get("series", "DS9")).upper() != series):
            continue
        planned_job_id = plan.get("job_id")
        if planned_job_id and planned_job_id != job_id:
            continue
        return path.resolve(strict=False), plan
    return None


def _resume_block_reason(state: dict[str, Any], plan: dict[str, Any],
                         profile_matches: bool, profile_digest: str) -> str | None:
    """Verify the runner's resume gates before offering a job as resumable."""
    if not profile_matches:
        return "The machine profile changed; this job cannot resume with the current profile."
    saved_steps = state.get("steps")
    plan_steps = plan.get("steps")
    if not isinstance(saved_steps, list) or not isinstance(plan_steps, list):
        return "Saved workflow steps are incomplete; inspect the state and create a new plan."
    if len(saved_steps) != len(plan_steps):
        return "Saved workflow steps differ from the plan; inspect the state and create a new plan."
    try:
        from run_workflow import (WorkflowError, _downstream_completed_output_ids,
                                  _fingerprint_payload, _verify_passed_step,
                                  file_fingerprints, validate_resume_state)
    except ModuleNotFoundError as exc:
        if exc.name != "run_workflow":
            return f"Resume checks are unavailable: {exc}"
        try:
            from scripts.run_workflow import (WorkflowError, _downstream_completed_output_ids,
                                              _fingerprint_payload, _verify_passed_step,
                                              file_fingerprints, validate_resume_state)
        except ImportError as fallback_exc:
            return f"Resume checks are unavailable: {fallback_exc}"
    try:
        validation_plan = dict(plan)
        if not validation_plan.get("job_id"):
            validation_plan["job_id"] = state.get("job_id")
        validate_resume_state(state, validation_plan, str(state.get("plan_sha256", "")), profile_digest)
        policy = str(state.get("fingerprint_policy", plan.get("fingerprint_policy", "balanced")))
        for index, (saved, step) in enumerate(zip(saved_steps, plan_steps)):
            if not isinstance(saved, dict) or not isinstance(step, dict):
                return "Saved workflow steps are malformed; inspect the state and create a new plan."
            if (saved.get("id") != step.get("id") or
                    saved.get("kind", "stage") != step.get("kind", "stage") or
                    saved.get("stage") != step.get("stage")):
                return "Saved workflow steps differ from the plan; inspect the state and create a new plan."
            status = saved.get("status")
            if status == "PASS":
                _verify_passed_step(
                    saved, step, policy,
                    superseded_path_ids=_downstream_completed_output_ids(plan, state, index))
                continue
            if status == "PENDING":
                continue
            step_id = str(saved.get("id") or step.get("id") or f"step {index + 1}")
            if saved.get("kind") == "review" and status == "WAITING_APPROVAL":
                current_inputs = file_fingerprints(step.get("inputs", []), policy=policy)
                if _fingerprint_payload(current_inputs) != (saved.get("input_fingerprints") or []):
                    return f"Review evidence changed for {step_id}; create a new plan."
                prompt = step.get("prompt")
                if (saved.get("review_prompt_sha256") and isinstance(prompt, str) and
                        saved["review_prompt_sha256"] != hashlib.sha256(prompt.encode("utf-8")).hexdigest()):
                    return f"Review prompt changed for {step_id}; create a new plan."
                continue
            if status not in ("RUNNING", "INTERRUPTED", "FAILED"):
                return f"Step {step_id} has unsupported resume state {status!r}."
            if (step.get("stage") == "download" and
                    step.get("resume_policy") == "download"):
                current_inputs = file_fingerprints(step.get("inputs", []), policy=policy)
                if _fingerprint_payload(current_inputs) != (saved.get("input_fingerprints") or []):
                    return f"Resumable download {step_id} inputs changed; refusing retry."
                continue
            before_outputs = saved.get("output_fingerprints_before")
            if not isinstance(before_outputs, list):
                return (f"{step_id} ended {status} without pre-attempt output evidence; "
                        "inspect it and create a new plan after reconciliation.")
            current_inputs = file_fingerprints(step.get("inputs", []), policy=policy)
            if _fingerprint_payload(current_inputs) != (saved.get("input_fingerprints") or []):
                return f"{step_id} inputs changed after the failed attempt; inspect and create a new plan."
            current_outputs = file_fingerprints(step.get("outputs", []), must_exist=False,
                                                policy=policy)
            if _fingerprint_payload(current_outputs) != _fingerprint_payload(before_outputs):
                return (f"{step_id} outputs changed during the failed attempt; inspect and reconcile "
                        "before retrying.")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        return f"Resume evidence could not be verified: {exc}"
    return None


def discover_incomplete_workflows(profile_path: str | Path,
                                  bundle_root: str | Path) -> list[dict[str, Any]]:
    """Find nonterminal jobs for one app-managed device and pair them with saved plans."""
    profile = Path(profile_path).expanduser().resolve(strict=True)
    bundle = Path(bundle_root).resolve(strict=True)
    devices = bundle / "devices"
    try:
        relative_profile = profile.relative_to(devices.resolve(strict=True))
    except (OSError, ValueError):
        return []
    if (len(relative_profile.parts) != 3 or
            relative_profile.parts[1:] != ("local device config", "machine.json")):
        return []
    device_root = devices / relative_profile.parts[0]
    if device_root.is_symlink() or not device_root.is_dir():
        return []
    try:
        current_profile_digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    except OSError:
        return []

    plans: list[tuple[Path, bytes, dict[str, Any]]] = []
    for path in _saved_plans(device_root):
        try:
            raw = path.read_bytes()
            plan = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(plan, dict) and plan.get("schema") == "ld-pipeline-workflow-plan-v1":
            plans.append((path, raw, plan))

    candidates: list[dict[str, Any]] = []
    for state_path in _workflow_states(device_root):
        state = _read_json(state_path)
        if (not state or state.get("schema") != STATE_SCHEMA or
                state.get("status") in ("PASS", "REJECTED")):
            continue
        match = _find_plan(plans, state)
        if match is None:
            continue
        plan_path, plan = match
        try:
            plan_profile = Path(str(plan.get("profile", ""))).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            plan_profile = Path()
        last_step, last_completed_utc = _last_completed_step(state)
        profile_matches = (plan_profile == profile and
                           state.get("profile_sha256") == current_profile_digest)
        resume_block_reason = _resume_block_reason(
            state, plan, profile_matches, current_profile_digest)
        candidate = {
            "state_path": state_path.resolve(strict=False),
            "plan_path": plan_path,
            "state": state,
            "plan": plan,
            "job_id": str(state.get("job_id", state_path.stem)),
            "episode": str(state.get("episode", plan.get("episode", ""))),
            "series": str(state.get("series", plan.get("series", "DS9"))).upper(),
            "status": str(state.get("status", "unknown")),
            "last_completed_step": last_step,
            "last_completed_utc": last_completed_utc,
            "profile_matches": profile_matches,
            "resumable": resume_block_reason is None,
            "resume_block_reason": resume_block_reason or "",
            "sort_timestamp": _timestamp_key(last_completed_utc),
        }
        if candidate["resumable"]:
            candidates.append(candidate)
    candidates.sort(key=lambda row: (row["sort_timestamp"], row["job_id"]), reverse=True)
    return candidates
