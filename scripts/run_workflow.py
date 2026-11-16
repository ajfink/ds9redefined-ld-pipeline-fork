#!/usr/bin/env python3
"""Execute an ordered, allowlisted workflow plan with durable status and safe resume.

Plans contain stage names from ``run_stage.py``; this program never evaluates shell
text. Each step declares the files/directories that establish its inputs and outputs.
Resume fingerprints are full SHA-256 for files up to 64 MiB; larger files use size,
mtime, and first/last-block SHA-256 by default. Plans may request full hashing.

    python scripts/run_workflow.py --plan <job.json>                 # plan only
    python scripts/run_workflow.py --plan <job.json> --execute
    python scripts/run_workflow.py --plan <job.json> --execute --resume

Only the Archive download stage may be retried automatically after interruption,
because the bundled downloader is explicitly resumable. Other interrupted/failed
stages stop for inspection; their helpers retain their own overwrite protections.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import queue
import re
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
RUN_STAGE = SCRIPTS / "run_stage.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from dependency_lock import (LockError, redact_shared_text, shared_redactions)  # noqa: E402
from ldcommon import device_workspace_enabled  # noqa: E402
from run_stage import STAGES, load_profile  # noqa: E402
from shared_workspace import resolve_instance_workspace  # noqa: E402

PLAN_SCHEMA = "ld-pipeline-workflow-plan-v1"
STATE_SCHEMA = "ld-pipeline-workflow-state-v2"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STEP_ID_RE = JOB_ID_RE
CHUNK_BYTES = 4 * 1024 * 1024
FULL_HASH_LIMIT = 64 * 1024 * 1024
INPUT_REF_RE = re.compile(r"\$\{input:([A-Za-z0-9_]+)\}")


class WorkflowError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def file_fingerprints(values: list[str], *, must_exist: bool = True,
                      policy: str = "balanced") -> list[dict[str, Any]]:
    """Fingerprint paths; large media use stat + edge samples unless full mode is requested."""
    if policy not in ("balanced", "full"):
        raise WorkflowError(f"unsupported fingerprint policy: {policy!r}")

    def fingerprint(path: Path) -> dict[str, Any]:
        stat = path.stat()
        row: dict[str, Any] = {"path": str(path.resolve()), "kind": "file", "size": stat.st_size,
                               "mtime_ns": stat.st_mtime_ns}
        if policy == "full" or stat.st_size <= FULL_HASH_LIMIT:
            row["method"] = "sha256"
            row["sha256"] = sha256_file(path)
        else:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                first = stream.read(CHUNK_BYTES)
                stream.seek(max(0, stat.st_size - CHUNK_BYTES))
                last = stream.read(CHUNK_BYTES)
            digest.update(first)
            digest.update(last)
            row["method"] = "stat+edge-sha256"
            row["edge_sha256"] = digest.hexdigest()
        return row

    result = []
    for raw in values:
        unresolved = Path(os.path.expandvars(os.path.expanduser(raw)))
        if unresolved.is_symlink():
            raise WorkflowError(f"refusing to fingerprint a symbolic link: {unresolved}")
        path = unresolved.resolve()
        if path.is_file():
            result.append(fingerprint(path))
            continue
        if path.is_dir():
            result.append({"path": str(path), "kind": "directory"})
            for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
                base = Path(current)
                dirs.sort(key=str.casefold)
                files.sort(key=str.casefold)
                for name in dirs:
                    child = base / name
                    if child.is_symlink():
                        raise WorkflowError(f"refusing to fingerprint a directory containing a symbolic link: {child}")
                    result.append({"path": str(child.resolve()), "kind": "directory"})
                for name in files:
                    child = base / name
                    if child.is_symlink() or not child.is_file():
                        raise WorkflowError(f"refusing to fingerprint a non-regular directory entry: {child}")
                    result.append(fingerprint(child))
            continue
        if must_exist:
            raise WorkflowError(f"declared workflow path is missing or not a regular file/directory: {path}")
    return result


def _absolute_path_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise WorkflowError(f"{where} must be an array of non-empty absolute file or directory paths")
    paths = []
    for item in value:
        expanded = os.path.expandvars(os.path.expanduser(item))
        path = Path(expanded)
        if not path.is_absolute():
            raise WorkflowError(f"{where} paths must be absolute: {item!r}")
        if path.is_symlink():
            raise WorkflowError(f"{where} must not include symbolic links: {item!r}")
        resolved = str(path.resolve())
        if resolved in paths:
            raise WorkflowError(f"{where} contains a duplicate path: {resolved}")
        paths.append(resolved)
    return paths


def _path_id(path: str) -> str:
    normalized = os.path.normcase(str(Path(path).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _paths_overlap(left: str, right: str) -> bool:
    a, b = Path(left).resolve(), Path(right).resolve()
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def expand_operator_inputs(plan: dict) -> dict:
    """Resolve an example's clearly separated editable fields into its plan."""
    operator_inputs = plan.get("_operator_inputs_EDIT")
    if operator_inputs is None:
        return plan
    if not isinstance(operator_inputs, dict) or not operator_inputs:
        raise WorkflowError("_operator_inputs_EDIT must be a non-empty object")
    values: dict[str, str] = {}
    for key, setting in operator_inputs.items():
        if isinstance(setting, dict):
            if set(setting) != {"value", "_comment"} or not isinstance(setting.get("_comment"), str):
                raise WorkflowError(f"_operator_inputs_EDIT.{key} must contain only 'value' and explanatory '_comment' fields")
            value = setting["value"]
        else:
            # Keep simple strings accepted for hand-authored plans; the example
            # template uses value/comment objects to annotate each setting.
            value = setting
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"_operator_inputs_EDIT.{key} must be a non-empty string")
        if "REPLACE_" in value or "YOUR_ACCOUNT" in value:
            raise WorkflowError(f"replace the example value in _operator_inputs_EDIT.{key}")
        if INPUT_REF_RE.search(value):
            raise WorkflowError(f"_operator_inputs_EDIT.{key} contains an unresolved input reference")
        values[key] = value

    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            def replacement(match: re.Match[str]) -> str:
                key = match.group(1)
                if key not in values:
                    raise WorkflowError(f"plan references missing operator input: {key}")
                return values[key]
            resolved = INPUT_REF_RE.sub(replacement, value)
            if "${input:" in resolved:
                raise WorkflowError(f"plan contains an unresolved operator input reference: {resolved}")
            return resolved
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    return {key: (value if key in ("_instructions_DO_NOT_EDIT", "_operator_inputs_EDIT")
                  else substitute(value)) for key, value in plan.items()}


def load_plan(path: Path) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
        # PowerShell's UTF-8 Set-Content commonly writes a BOM; accept both that
        # form and plain UTF-8 JSON plans.
        plan = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read workflow plan {path}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise WorkflowError(f"plan schema must be {PLAN_SCHEMA!r}")
    plan = expand_operator_inputs(plan)
    job_id = plan.get("job_id")
    if job_id is not None and (not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id)):
        raise WorkflowError("job_id, when supplied, must use 1-64 letters, digits, dots, underscores, or hyphens")
    episode = plan.get("episode")
    if not isinstance(episode, str) or not re.fullmatch(r"S\d{2}E\d{2}", episode, re.IGNORECASE):
        raise WorkflowError("episode must be an SxxExx code")
    if job_id is None and (not isinstance(plan.get("episode_title"), str) or not plan["episode_title"].strip()):
        raise WorkflowError("episode_title is required when job_id is omitted; the runner generates an ID from it")
    profile = plan.get("profile")
    if not isinstance(profile, str) or not Path(os.path.expandvars(os.path.expanduser(profile))).is_absolute():
        raise WorkflowError("profile must be an absolute path to the device-local machine profile")
    if plan.get("fingerprint_policy", "balanced") not in ("balanced", "full"):
        raise WorkflowError("fingerprint_policy must be 'balanced' or 'full'")
    if plan.get("instance_root") is not None:
        instance = plan["instance_root"]
        if not isinstance(instance, str) or not Path(os.path.expandvars(os.path.expanduser(instance))).is_absolute():
            raise WorkflowError("instance_root, when present, must be an absolute path")
    decisions = plan.get("decisions", [])
    if not isinstance(decisions, list) or any(not isinstance(row, dict) for row in decisions):
        raise WorkflowError("decisions must be an array of JSON objects recording explicit user choices")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("steps must be a non-empty ordered array")
    seen_ids: set[str] = set()
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if not isinstance(step, dict):
            raise WorkflowError(f"{where} must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not STEP_ID_RE.fullmatch(step_id) or step_id in seen_ids:
            raise WorkflowError(f"{where}.id must be unique and use 1-64 letters, digits, dots, underscores, or hyphens")
        seen_ids.add(step_id)
        kind = step.get("kind", "stage")
        if kind not in ("stage", "review"):
            raise WorkflowError(f"{where}.kind must be 'stage' or 'review'")
        stage = step.get("stage")
        if kind == "stage" and stage not in STAGES:
            raise WorkflowError(f"{where}.stage is not an allowlisted run_stage name: {stage!r}")
        if kind == "review":
            prompt = step.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise WorkflowError(f"{where}.prompt is required for a review step")
        args = step.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise WorkflowError(f"{where}.args must be an array of strings (one argument per entry)")
        inputs = _absolute_path_list(step.get("inputs", []), f"{where}.inputs")
        outputs = _absolute_path_list(step.get("outputs", []), f"{where}.outputs")
        if kind == "review" and (not inputs or outputs):
            raise WorkflowError(f"{where} review steps need evidence inputs and must not declare outputs")
        if any(_paths_overlap(source, target) for source in inputs for target in outputs):
            raise WorkflowError(f"{where} input/output paths must not overlap; list an in-place-mutated artifact as an output only")
        if not inputs and not outputs:
            raise WorkflowError(f"{where} must declare at least one input or output file for resume verification")
        retry = step.get("resume_policy", "inspect")
        if retry not in ("inspect", "download"):
            raise WorkflowError(f"{where}.resume_policy must be 'inspect' or 'download'")
        if retry == "download" and (kind != "stage" or stage != "download"):
            raise WorkflowError(f"{where}: only the allowlisted download stage may use resume_policy='download'")
        if stage == "download" and retry == "inspect":
            # Conservative default. The author must opt into the downloader's
            # documented resumability in the immutable plan.
            pass
        for key in ("report_out",):
            if key in step and (not isinstance(step[key], str) or not step[key]):
                raise WorkflowError(f"{where}.{key} must be a non-empty path string")
        publish = step.get("publish_references", [])
        if not isinstance(publish, list) or any(not isinstance(item, str) or not item for item in publish):
            raise WorkflowError(f"{where}.publish_references must be an array of non-empty paths")
        if kind == "review":
            if not isinstance(step.get("for_stage"), str):
                raise WorkflowError(f"{where}.for_stage must name the stage whose evidence is reviewed")
            if step.get("args") or step.get("report_out") or publish:
                raise WorkflowError(f"{where} is a review gate, not an executable stage")
    for index, step in enumerate(steps):
        if step.get("kind", "stage") == "stage" and STAGES[step["stage"]].get("review_gate"):
            if index + 1 >= len(steps):
                raise WorkflowError(f"{step['stage']} must be followed by an explicit human review step")
            following = steps[index + 1]
            if (following.get("kind") != "review" or
                    following.get("for_stage") != step["stage"]):
                raise WorkflowError(f"{step['stage']} must be followed immediately by a review step with for_stage={step['stage']!r}")
        if step.get("kind") == "review":
            if index == 0:
                raise WorkflowError("review step must follow the stage that produced its evidence")
            previous = steps[index - 1]
            if (previous.get("kind", "stage") != "stage" or
                    previous.get("stage") != step.get("for_stage") or
                    not STAGES[previous["stage"]].get("review_gate")):
                raise WorkflowError(f"review step {step['id']} must follow its declared review-gated stage")
    return plan, raw


def workflow_logs(profile_path: Path, profile: dict, plan: dict,
                  *, create_workspace: bool) -> tuple[Path, str | None, dict[str, str], str | None]:
    env_exports: dict[str, str] = {}
    warning = None
    instance_root = plan.get("instance_root")
    if device_workspace_enabled(profile):
        workspace, warning = resolve_instance_workspace(
            profile_path, profile, plan["episode"], instance_root,
            create=create_workspace)
        if workspace:
            instance_root = str(Path(workspace["instance_root"]).resolve())
            env_exports = {str(k): str(v) for k, v in workspace.get("shell_exports", {}).items()}
            return Path(instance_root) / "logs", instance_root, env_exports, warning
    elif instance_root:
        raise WorkflowError("instance_root requires shared.enabled=true in the selected profile")
    raw_logs = (profile.get("roots") or {}).get("logs")
    if not raw_logs:
        raise WorkflowError("profile roots.logs must be set for durable workflow status")
    logs = Path(os.path.expandvars(os.path.expanduser(str(raw_logs)))).resolve()
    if create_workspace:
        if not logs.is_dir():
            raise WorkflowError(f"profile roots.logs must already exist for workflow state: {logs}")
    return logs, None, env_exports, warning


def _state_for_disk(state: dict, redactions: list[tuple[str, str]]) -> str:
    # Paths live in the immutable plan, not the shared status file. Fingerprint
    # order is stable because the plan hash is checked on resume.
    safe_state = json.loads(json.dumps(state))
    for step in safe_state.get("steps", []):
        for key in ("input_fingerprints", "output_fingerprints"):
            for row in step.get(key, []) or []:
                raw_path = row.pop("path", None)
                if raw_path:
                    row["path_id"] = _path_id(raw_path)
    encoded = json.dumps(safe_state, ensure_ascii=False, indent=2) + "\n"
    return redact_shared_text(encoded, redactions) if redactions else encoded


def save_state(path: Path, state: dict, redactions: list[tuple[str, str]]) -> None:
    state["updated_utc"] = utc_now()
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_state_for_disk(state, redactions))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextmanager
def workflow_lock(path: Path) -> Iterator[None]:
    """OS advisory lock; the lock is released automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkflowError(f"another process holds workflow lock {path}") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkflowError(f"another process holds workflow lock {path}") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _plan_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def generate_job_id(episode: str, title: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-") or "episode"
    slug = slug[:24].strip("-") or "episode"
    return f"{episode.upper()}-{slug}-{stamp}"


def find_resume_state(logs: Path, plan_digest: str, profile_digest: str,
                      episode: str) -> Path:
    matches = []
    for path in logs.glob("workflow-*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (state.get("schema") == STATE_SCHEMA and
                state.get("plan_sha256") == plan_digest and
                state.get("profile_sha256") == profile_digest and
                state.get("episode") == episode):
            matches.append((path, state))
    if not matches:
        raise WorkflowError("--resume found no saved job matching this plan, profile, and episode")
    unfinished = [(path, state) for path, state in matches if state.get("status") != "PASS"]
    if len(unfinished) > 1:
        ids = ", ".join(state.get("job_id", path.stem) for path, state in unfinished)
        raise WorkflowError(f"--resume is ambiguous; multiple unfinished jobs match: {ids}")
    if unfinished:
        return unfinished[0][0]
    # If all matching jobs completed, `--resume` is a harmless verification/no-op
    # of the most recently created one. A new execution gets a fresh generated ID.
    return max(matches, key=lambda row: str(row[1].get("created_utc", "")))[0]


def _profile_digest(path: Path) -> str:
    return sha256_file(path)


def _fresh_state(plan: dict, plan_digest: str, profile_digest: str) -> dict:
    created = utc_now()
    local_user = getpass.getuser()
    decisions = []
    for decision in plan.get("decisions", []):
        row = dict(decision)
        # Approval identity/time are execution metadata, never operator inputs.
        row["approved_by"] = local_user
        row["approved_at_utc"] = created
        decisions.append(row)
    return {
        "schema": STATE_SCHEMA,
        "job_id": plan["job_id"],
        "episode": plan["episode"],
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "plan_sha256": plan_digest,
        "profile_sha256": profile_digest,
        "fingerprint_policy": plan.get("fingerprint_policy", "balanced"),
        "created_utc": created,
        "updated_utc": created,
        "decisions": decisions,
        "status": "PENDING",
        "active_step": None,
        "steps": [{"id": step["id"], "kind": step.get("kind", "stage"),
                   "stage": step.get("stage"), "status": "PENDING"}
                  for step in plan["steps"]],
    }


def validate_resume_state(state: dict, plan: dict, plan_digest: str,
                          profile_digest: str) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise WorkflowError("workflow state schema is unsupported")
    expected = {
        "job_id": plan["job_id"],
        "episode": plan["episode"],
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "plan_sha256": plan_digest,
        "profile_sha256": profile_digest,
        "fingerprint_policy": plan.get("fingerprint_policy", "balanced"),
    }
    changed = [key for key, value in expected.items() if state.get(key) != value]
    if changed:
        raise WorkflowError("cannot resume: plan/profile/workflow identity changed: " + ", ".join(changed))
    recorded = [(row.get("id"), row.get("kind", "stage"), row.get("stage"))
                for row in state.get("steps", [])]
    current = [(row["id"], row.get("kind", "stage"), row.get("stage"))
               for row in plan["steps"]]
    if recorded != current:
        raise WorkflowError("cannot resume: ordered workflow steps differ from the saved state")


def _step_file_lists(step: dict) -> tuple[list[str], list[str]]:
    return step.get("inputs", []), step.get("outputs", [])


def _fingerprint_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "path"} | {
        "path_id": _path_id(row["path"])} for row in rows]


def _verify_passed_step(saved: dict, step: dict, fingerprint_policy: str) -> None:
    inputs, outputs = _step_file_lists(step)
    current_inputs = file_fingerprints(inputs, policy=fingerprint_policy)
    current_outputs = file_fingerprints(outputs, policy=fingerprint_policy)
    expected_inputs = saved.get("input_fingerprints") or []
    if _fingerprint_payload(current_inputs) != expected_inputs:
        raise WorkflowError(f"completed step {step['id']} input changed; refusing to skip or rerun it")
    expected_outputs = saved.get("output_fingerprints") or []
    if _fingerprint_payload(current_outputs) != expected_outputs:
        raise WorkflowError(f"completed step {step['id']} output changed or is missing; inspect it before continuing")


def _command(plan: dict, step: dict, profile_path: Path,
             instance_root: str | None) -> list[str]:
    command = [sys.executable, str(RUN_STAGE), "--stage", step["stage"],
               "--episode", plan["episode"], "--profile", str(profile_path), "--execute"]
    if instance_root:
        command += ["--instance-root", instance_root]
    if step.get("report_out"):
        command += ["--report-out", step["report_out"]]
    for reference in step.get("publish_references", []):
        command += ["--publish-reference", reference]
    for value in step.get("args", []):
        command.append("--script-arg=" + value)
    return command


def _display(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


STANDARD_OUTPUT_RE = re.compile(
    r"(?:\b(?:warn(?:ing)?|error|failed|failure|fail|limit|blocked|critical|fatal|exception|traceback)\b"
    r"|\bworkflow\b|\breview(?: required| approved)?\b|\bstage runner error\b"
    r"|\bstage report:|\bprogress\b|\b\d+\s*/\s*\d+\b|(?<!\w)\d{1,3}%(?!\w))",
    re.IGNORECASE)


def standard_output_line(line: str) -> bool:
    """Keep notable diagnostics and gate/status lines in the concise console view."""
    return bool(STANDARD_OUTPUT_RE.search(line))


def configure_console() -> None:
    """Keep diagnostic streaming safe on Windows code pages without Unicode glyphs."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def _stop_child_tree(proc: subprocess.Popen) -> None:
    """Stop the launched stage process tree, not only its Python wrapper."""
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            # taskkill /T is scoped to the PID we launched and includes its
            # descendants (PowerShell/FFmpeg/VapourSynth workers).
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True, check=False)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _run_child(command: list[str], env: dict[str, str], log,
               redactions: list[tuple[str, str]], state: dict,
               state_path: Path, *, verbose: bool = True) -> int:
    safe_command = redact_shared_text(_display(command), redactions)
    log.write(f"\n[{utc_now()}] COMMAND: {safe_command}\n")
    log.flush()
    print("COMMAND: " + safe_command, flush=True)
    try:
        options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                   "text": True, "encoding": "utf-8", "errors": "replace",
                   "bufsize": 1, "env": env}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        proc = subprocess.Popen(command, **options)
    except OSError as exc:
        raise WorkflowError(f"could not launch allowlisted stage runner: {exc}") from exc
    assert proc.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    thread = threading.Thread(target=reader, name="workflow-stage-output", daemon=True)
    thread.start()
    line_count = 0
    last_line: str | None = None
    recent_lines: list[str] = []
    eof = False
    try:
        while not eof:
            try:
                line = lines.get(timeout=3.0)
            except queue.Empty:
                state["heartbeat_utc"] = utc_now()
                if last_line:
                    state["steps"][state["active_index"]]["last_output"] = redact_shared_text(last_line[-500:], redactions)
                save_state(state_path, state, redactions)
                continue
            if line is None:
                eof = True
                continue
            safe_line = redact_shared_text(line, redactions)
            log.write(safe_line)
            log.flush()
            recent_lines.append(safe_line.rstrip("\r\n"))
            if len(recent_lines) > 40:
                del recent_lines[0]
            if verbose or standard_output_line(safe_line):
                sys.stdout.write(safe_line)
                sys.stdout.flush()
            last_line = line.rstrip("\r\n")
            line_count += 1
            if line_count % 25 == 0:
                state["heartbeat_utc"] = utc_now()
                state["steps"][state["active_index"]]["last_output"] = redact_shared_text(last_line[-500:], redactions)
                save_state(state_path, state, redactions)
        rc = proc.wait()
        if rc and not verbose:
            print("Recent stage output (full details are retained in the workflow log):", flush=True)
            for detail in recent_lines:
                print(detail, flush=True)
        return rc
    except KeyboardInterrupt:
        _stop_child_tree(proc)
        raise
    finally:
        proc.stdout.close()
        thread.join(timeout=1)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read saved workflow state {path}: {exc}") from exc


def run_plan(plan_path: Path, *, execute: bool, resume: bool,
             approve_steps: set[str] | None = None, verbose: bool = True) -> int:
    plan, raw = load_plan(plan_path)
    approve_steps = approve_steps or set()
    review_ids = {step["id"] for step in plan["steps"] if step.get("kind") == "review"}
    unknown_approvals = approve_steps - review_ids
    if unknown_approvals:
        raise WorkflowError("--approve names an unknown review step: " + ", ".join(sorted(unknown_approvals)))
    if approve_steps and not resume:
        raise WorkflowError("review steps can only be approved while resuming a waiting workflow")
    plan_path = plan_path.resolve()
    profile_path, profile = load_profile(plan["profile"])
    profile_digest = _profile_digest(profile_path)
    logs, instance_root, shell_exports, workspace_warning = workflow_logs(
        profile_path, profile, plan, create_workspace=execute)
    shared = device_workspace_enabled(profile)
    redactions = shared_redactions(profile_path, profile) if shared else []
    if workspace_warning:
        print("WARN: " + workspace_warning, file=sys.stderr)

    if not execute:
        job_label = plan.get("job_id") or f"auto-generated from {plan['episode_title']!r} at execution time"
        print(f"PLAN {job_label} V{(ROOT / 'WORKFLOW_VERSION').read_text(encoding='ascii').strip()} episode={plan['episode']}")
        print(f"profile: {redact_shared_text(str(profile_path), redactions)}")
        if shared and not instance_root:
            print("status/log destination: resolved device workspace on execution (or profile roots.logs fallback); dry-run creates neither")
        else:
            print(f"status/log destination: {redact_shared_text(str(logs), redactions)}")
        print(f"fingerprint policy: {plan.get('fingerprint_policy', 'balanced')}")
        for index, step in enumerate(plan["steps"], 1):
            if step.get("kind") == "review":
                print(f"{index}. {step['id']} [REVIEW GATE] {step['prompt']}")
                print(f"   evidence inputs={len(step.get('inputs', []))}")
                continue
            command = _command(plan, step, profile_path, instance_root)
            print(f"{index}. {step['id']} [{step['stage']}] -> {_display(command)}")
            print(f"   verify inputs={len(step.get('inputs', []))} outputs={len(step.get('outputs', []))} resume={step.get('resume_policy', 'inspect')}")
        print("No commands or state files were created. Add --execute to run this plan.")
        return 0

    if not logs.is_dir():
        raise WorkflowError(f"workflow logs directory does not exist: {logs}")

    digest = _plan_digest(raw)
    if resume:
        if plan.get("job_id"):
            job_id = plan["job_id"]
            state_path = logs / f"workflow-{job_id}.json"
            if not state_path.is_file():
                raise WorkflowError(f"--resume requested but state does not exist: {state_path}")
        else:
            state_path = find_resume_state(logs, digest, profile_digest, plan["episode"])
            job_id = _load_state(state_path).get("job_id")
            if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
                raise WorkflowError(f"saved workflow state has an invalid generated job ID: {state_path}")
        plan["job_id"] = job_id
    else:
        job_id = plan.get("job_id")
        if job_id is None:
            job_id = generate_job_id(plan["episode"], plan["episode_title"])
            while ((logs / f"workflow-{job_id}.json").exists() or
                   (logs / f"workflow-{job_id}.log").exists()):
                job_id = generate_job_id(plan["episode"], plan["episode_title"])
            plan["job_id"] = job_id
    state_path = logs / f"workflow-{job_id}.json"
    log_path = logs / f"workflow-{job_id}.log"
    lock_path = logs / f"workflow-{job_id}.lock"
    if not resume and (state_path.exists() or log_path.exists()):
        raise WorkflowError(f"workflow job already exists; choose a fresh generated ID or use --resume: {state_path}")

    with workflow_lock(lock_path):
        fingerprint_policy = plan.get("fingerprint_policy", "balanced")
        if resume:
            state = _load_state(state_path)
            validate_resume_state(state, plan, digest, profile_digest)
            if not log_path.is_file():
                raise WorkflowError(f"workflow log is missing; refusing to resume without evidence: {log_path}")
        else:
            state = _fresh_state(plan, digest, profile_digest)
            # Exclusive creation prevents accidentally replacing another job's evidence.
            with state_path.open("x", encoding="utf-8"):
                pass
            with log_path.open("x", encoding="utf-8") as log:
                log.write(f"workflow={plan['job_id']} episode={plan['episode']} version={state['workflow_version']}\n")
                log.write(f"plan_sha256={digest}\nprofile_sha256={profile_digest}\n")
                decisions = redact_shared_text(json.dumps(state.get("decisions", []), ensure_ascii=False), redactions)
                log.write("explicit_decisions=" + decisions + "\n")
                log.write("NOTE: retain the immutable plan file; state stores only its digest.\n")
                log.flush()
            save_state(state_path, state, redactions)

        env = os.environ.copy()
        env["LDP_PROFILE"] = str(profile_path)
        env.setdefault("PYTHONIOENCODING", "utf-8:replace")
        if verbose:
            env["PYTHONUNBUFFERED"] = "1"
        env.update(shell_exports)
        if instance_root:
            env["LDP_INSTANCE_ROOT"] = instance_root
            env["LDP_EPISODE"] = plan["episode"]
        state["status"] = "RUNNING"
        save_state(state_path, state, redactions)

        try:
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"\n[{utc_now()}] output_mode={'verbose' if verbose else 'standard'}\n")
                log.flush()
                for index, step in enumerate(plan["steps"]):
                    saved = state["steps"][index]
                    inputs = step.get("inputs", [])
                    outputs = step.get("outputs", [])
                    if step.get("kind") == "review":
                        if saved["status"] == "PASS":
                            _verify_passed_step(saved, step, fingerprint_policy)
                            print(f"SKIP verified approved review: {step['id']}")
                            continue
                        if saved["status"] not in ("PENDING", "WAITING_APPROVAL"):
                            raise WorkflowError(f"review step {step['id']} has unexpected state {saved['status']}")
                        current_inputs = file_fingerprints(inputs, policy=fingerprint_policy)
                        if saved["status"] == "PENDING":
                            saved["input_fingerprints"] = current_inputs
                            saved["prompt"] = step["prompt"]
                            saved["status"] = "WAITING_APPROVAL"
                            saved["started_utc"] = utc_now()
                        elif _fingerprint_payload(current_inputs) != (saved.get("input_fingerprints") or []):
                            raise WorkflowError(f"review evidence changed for {step['id']}; rerun the preceding stage and review again")
                        if step["id"] not in approve_steps:
                            state["status"] = "WAITING_APPROVAL"
                            state["active_step"] = step["id"]
                            state["active_index"] = index
                            save_state(state_path, state, redactions)
                            prompt = redact_shared_text(step["prompt"], redactions)
                            print(f"REVIEW REQUIRED [{step['id']}]: {prompt}")
                            command = [sys.executable, str(Path(__file__).resolve()),
                                       "--plan", str(plan_path), "--execute", "--resume",
                                       "--approve", step["id"]]
                            print("After reviewing the evidence, approve with: " +
                                  redact_shared_text(_display(command), redactions))
                            return 3
                        saved["status"] = "PASS"
                        saved["finished_utc"] = utc_now()
                        saved["approval"] = {"approved_by": getpass.getuser(),
                                              "approved_at_utc": utc_now()}
                        saved["output_fingerprints"] = []
                        saved.pop("prompt", None)
                        state["status"] = "RUNNING"
                        state["active_step"] = None
                        save_state(state_path, state, redactions)
                        with log_path.open("a", encoding="utf-8", newline="\n") as approval_log:
                            approval_log.write("\nREVIEW APPROVED " + json.dumps({
                                "step": step["id"], **saved["approval"]}, ensure_ascii=False) + "\n")
                        print(f"REVIEW APPROVED [{step['id']}] by {saved['approval']['approved_by']}")
                        continue
                    if saved["status"] == "PASS":
                        _verify_passed_step(saved, step, fingerprint_policy)
                        print(f"SKIP verified completed step: {step['id']}")
                        continue
                    if saved["status"] in ("RUNNING", "INTERRUPTED", "FAILED"):
                        if step.get("resume_policy") != "download":
                            raise WorkflowError(
                                f"step {step['id']} ended as {saved['status']}; inspect its outputs/logs, "
                                "then create a new job plan after reconciliation")
                        current_inputs = file_fingerprints(inputs, policy=fingerprint_policy)
                        if _fingerprint_payload(current_inputs) != (saved.get("input_fingerprints") or []):
                            raise WorkflowError(f"resumable download step {step['id']} inputs changed; refusing retry")
                        print(f"RESUME downloader step: {step['id']}")
                    else:
                        saved["input_fingerprints"] = file_fingerprints(inputs, policy=fingerprint_policy)

                    state["active_step"] = step["id"]
                    state["active_index"] = index
                    saved["status"] = "RUNNING"
                    saved["started_utc"] = saved.get("started_utc") or utc_now()
                    saved["attempts"] = int(saved.get("attempts", 0)) + 1
                    saved["input_fingerprints"] = saved.get("input_fingerprints") or file_fingerprints(
                        inputs, policy=fingerprint_policy)
                    save_state(state_path, state, redactions)
                    started = time.monotonic()
                    command = _command(plan, step, profile_path, instance_root)
                    try:
                        rc = _run_child(command, env, log, redactions, state, state_path,
                                        verbose=verbose)
                    except KeyboardInterrupt:
                        saved["status"] = "INTERRUPTED"
                        saved["finished_utc"] = utc_now()
                        saved["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        state["status"] = "INTERRUPTED"
                        state["active_step"] = step["id"]
                        save_state(state_path, state, redactions)
                        raise
                    except WorkflowError as exc:
                        rc = 2
                        saved["error"] = str(exc)
                    saved["exit_code"] = rc
                    saved["finished_utc"] = utc_now()
                    saved["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    if rc != 0:
                        saved["status"] = "FAILED"
                        state["status"] = "FAILED"
                        state["active_step"] = None
                        save_state(state_path, state, redactions)
                        print(f"WORKFLOW STOP: step {step['id']} failed (exit {rc}); later steps were not run", file=sys.stderr)
                        return rc or 1
                    saved["output_fingerprints"] = file_fingerprints(outputs, policy=fingerprint_policy)
                    if not inputs and not outputs:
                        raise WorkflowError(f"step {step['id']} has no file evidence to checkpoint")
                    saved["status"] = "PASS"
                    saved.pop("last_output", None)
                    saved.pop("error", None)
                    state["active_step"] = None
                    save_state(state_path, state, redactions)

                state["status"] = "PASS"
                state["finished_utc"] = utc_now()
                state["active_step"] = None
                save_state(state_path, state, redactions)
                print(f"WORKFLOW PASS: {plan['job_id']} state={redact_shared_text(str(state_path), redactions)}")
                return 0
        except KeyboardInterrupt:
            print("WORKFLOW INTERRUPTED; durable state was saved. Use --resume only for a plan with safe retry policy.", file=sys.stderr)
            return 130
        except WorkflowError:
            if state.get("status") == "RUNNING":
                state["status"] = "BLOCKED"
                state["active_step"] = state.get("active_step")
                save_state(state_path, state, redactions)
            raise


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", required=True, help="immutable JSON workflow plan")
    parser.add_argument("--execute", action="store_true", help="execute; default is a read-only plan")
    parser.add_argument("--resume", action="store_true", help="resume this exact saved job after verifying plan/profile/files")
    parser.add_argument("--approve", action="append", default=[], metavar="STEP_ID",
                        help="approve a named review step while resuming after personally reviewing its evidence")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--verbose", action="store_true",
                              help="stream all child-stage output to the console (the default; full logs are always saved)")
    output_group.add_argument("--standard-output", action="store_true",
                              help="show warnings, errors, gates, and summaries; retain complete output in the workflow log")
    args = parser.parse_args()
    if (args.resume or args.approve) and not args.execute:
        parser.error("--resume/--approve require --execute")
    if args.approve and not args.resume:
        parser.error("--approve requires --resume")
    try:
        return run_plan(Path(args.plan), execute=args.execute, resume=args.resume,
                        approve_steps=set(args.approve), verbose=not args.standard_output)
    except (OSError, json.JSONDecodeError, LockError, WorkflowError) as exc:
        print(f"WORKFLOW ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
