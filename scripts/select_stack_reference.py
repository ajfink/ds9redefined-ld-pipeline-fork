#!/usr/bin/env python3
"""Use Orc's own refresh recommendation to select a weighted-stack reference.

The first declared capture is only a scratch sample-grid seed when the manifest
reference is blank. Orc refreshes that scratch project, reports its suggested
reference, and the helper repeats with the suggested capture first until Orc's
recommendation is stable (bounded to three refreshes). It then saves the selected
reference and refreshed project. It never renders the stack master or invents a
quality score.

  python scripts\\select_stack_reference.py --episode S01E10
  python scripts\\select_stack_reference.py --episode S01E10 --dry-run
  python scripts\\select_stack_reference.py --episode S01E05 --reselect
  python scripts\\select_stack_reference.py --episode S01E09 --resume <logs/checkpoint.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import (BUNDLE, Ld, PipelineError, assert_writable_path,
                      episode_manifest_path, load_episode,
                      redact_shared_text)  # noqa: E402
from process_utils import process_group_options, safe_console_write, terminate_process_tree  # noqa: E402

MIN_CAPTURES = 2
MAX_CAPTURES = 6
MAX_REFERENCE_PASSES = 3
ORC_HEARTBEAT_SECONDS = 30.0
CHECKPOINT_SCHEMA = "ld-pipeline-stack-reference-checkpoint-v1"


def parse_refresh_report(texts: list[str]) -> dict:
    """Find Orc's JSON refresh object in output that may also contain log text."""
    decoder = JSONDecoder()
    for text in texts:
        for match in re.finditer(r"\{", text):
            try:
                report, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if (isinstance(report, dict)
                    and {"nodes", "completed", "saved"}.issubset(report)):
                return report
    raise PipelineError("Orc refresh output did not contain a parseable JSON report")


def source_alignment_decision(report: dict, ordered_labels: list[str]) -> dict:
    """Validate Orc's recommendation and map its 1-based input index to a label."""
    if report.get("completed") is not True or report.get("saved") is not True:
        raise PipelineError(
            "Orc refresh did not report completed=true and saved=true; "
            "reference selection is not committed"
        )
    nodes = report.get("nodes")
    if not isinstance(nodes, list):
        raise PipelineError("Orc refresh JSON has no nodes array")
    matches = [node for node in nodes
               if node.get("stage") == "source_align"
               and node.get("tool") == "source_alignment"]
    if len(matches) != 1:
        raise PipelineError(
            f"expected one source_alignment result, found {len(matches)}"
        )
    node = matches[0]
    if node.get("status") != "success":
        raise PipelineError(
            f"Orc source alignment status is {node.get('status')!r}, not success"
        )
    stats = node.get("statistics") or {}
    try:
        source_count = int(stats["sourceCount"])
        suggested_index = int(stats["suggestedReference"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(
            "Orc source-alignment report lacks sourceCount/suggestedReference"
        ) from exc
    if source_count != len(ordered_labels):
        raise PipelineError(
            f"Orc analyzed {source_count} sources, expected {len(ordered_labels)}"
        )
    if not 1 <= suggested_index <= source_count:
        raise PipelineError(
            f"Orc suggestedReference={suggested_index} is outside 1..{source_count}"
        )

    # The JSON report carries the source labels as an independent check that the
    # recommendation index is being interpreted in the generated project's order.
    source_rows = node.get("sources")
    if isinstance(source_rows, list) and source_rows:
        reported_labels = []
        for row in source_rows:
            label = str(row.get("label", ""))
            prefix = "TBC Source "
            reported_labels.append(label[len(prefix):] if label.startswith(prefix) else label)
        if len(reported_labels) == len(ordered_labels) and reported_labels != ordered_labels:
            raise PipelineError(
                f"Orc source order {reported_labels} does not match project order "
                f"{ordered_labels}"
            )

    runner_up = stats.get("referenceRunnerUp")
    try:
        runner_up_index = int(runner_up) if runner_up is not None else None
    except (TypeError, ValueError):
        runner_up_index = None
    runner_up_label = None
    if runner_up_index is not None and 1 <= runner_up_index <= source_count:
        runner_up_label = ordered_labels[runner_up_index - 1]

    return {
        "status": node["status"],
        "source_count": source_count,
        "suggested_index": suggested_index,
        "suggested_label": ordered_labels[suggested_index - 1],
        "runner_up_index": runner_up_index,
        "runner_up_label": runner_up_label,
        "stack_sigma": stats.get("stackSigma"),
        "equal_sigma": stats.get("equalSigma"),
        "best_single_sigma": stats.get("bestSingleSigma"),
        "fit_trusted": stats.get("fitTrusted"),
        "fell_back_to_mean": stats.get("fellBackToMean"),
        "pressing_groups": stats.get("pressingGroups"),
        "worst_misfit": stats.get("worstMisfit"),
        "frames_with_1_source": stats.get("framesWith1Source"),
        "frames_with_2_sources": stats.get("framesWith2Sources"),
        "frames_with_3plus_sources": stats.get("framesWith3PlusSources"),
        "alignment_map": (node.get("parameter_changes") or {}).get("alignmentMap"),
    }


def _tail(text: str, count: int = 32) -> str:
    return "\n".join(text.splitlines()[-count:])


def _run_and_log(command: list[str], log_path: Path) -> subprocess.CompletedProcess:
    """Stream child output to disk/console and emit heartbeats during quiet work."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ld = Ld()
    output: list[str] = []
    lines: queue.Queue[str | None] = queue.Queue()
    safe_command = redact_shared_text(subprocess.list2cmdline(command), ld)
    try:
        log = log_path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as exc:
        raise PipelineError(f"refusing to overwrite reference-selection log: {log_path}") from exc
    with log:
        log.write(f"Command: {safe_command}\n--- merged stdout/stderr ---\n")
        log.flush()
        try:
            options = {
                "cwd": str(Path.home().resolve()),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            options.update(process_group_options())
            child = subprocess.Popen(command, **options)
        except OSError as exc:
            log.write(f"Could not run command: {exc}\nExit code: 127\n")
            log.flush()
            raise PipelineError(f"could not run {command[0]}: {exc}") from exc
        assert child.stdout is not None

        def pump() -> None:
            try:
                for line in child.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=pump, name="orc-output-reader", daemon=True)
        reader.start()
        started = time.monotonic()
        last_output = "(no output yet)"
        try:
            while True:
                try:
                    line = lines.get(timeout=ORC_HEARTBEAT_SECONDS)
                except queue.Empty:
                    heartbeat = (f"[heartbeat] elapsed={time.monotonic()-started:.0f}s; "
                                 f"last output: {last_output}\n")
                    log.write(redact_shared_text(heartbeat, ld))
                    log.flush()
                    safe_console_write(heartbeat)
                    continue
                if line is None:
                    break
                output.append(line)
                last_output = line.strip()[-240:] or "(blank line)"
                safe_line = redact_shared_text(line, ld)
                log.write(safe_line)
                log.flush()
                safe_console_write(safe_line)
            returncode = child.wait()
            reader.join()
        except KeyboardInterrupt:
            terminated = terminate_process_tree(child)
            log.write("\nINTERRUPTED by operator; partial Orc output is preserved above.\n"
                      f"process_tree_termination_confirmed={terminated}\n")
            log.flush()
            reader.join(timeout=10)
            raise
        log.write(f"\nExit code: {returncode}\n")
        log.flush()
        return subprocess.CompletedProcess(command, returncode, "".join(output), "")


def capture_input_fingerprint(ep) -> list[dict]:
    """Lightweight per-capture identity used to reject stale Orc-pass checkpoints."""
    downloads = ep.downloads()
    result = []
    for capture in ep.data.get("captures") or []:
        name = capture.get("tbc_file")
        if not name:
            raise PipelineError(f"capture {capture.get('label')!r} has no TBC filename")
        path = downloads / name
        if not path.is_file():
            raise PipelineError(f"reference selection input missing: {path}")
        stat = path.stat()
        result.append({"label": capture.get("label"), "name": name,
                       "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                       "manifest_md5": capture.get("tbc_md5")})
    return result


def file_fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest()}


def manifest_non_reference_digest(data: dict) -> str:
    content = dict(data)
    content.pop("reference", None)
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_bytes_with_reference(original_bytes: bytes, reference: str) -> bytes:
    data = json.loads(original_bytes.decode("utf-8"))
    data["reference"] = reference
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_checkpoint(path: Path, state: dict, ld: Ld, *, create: bool = False) -> None:
    """Atomically save resume state; checkpoint values contain no local absolute paths."""
    path = assert_writable_path(path, ld)
    encoded = redact_shared_text(json.dumps(state, ensure_ascii=False, indent=2), ld)
    try:
        json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"checkpoint redaction produced invalid JSON: {exc}") from exc
    if create:
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")
        except FileExistsError as exc:
            raise PipelineError(f"refusing to overwrite reference checkpoint: {path}") from exc
        return
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_checkpoint(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"reference checkpoint is missing or is a symlink: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid reference checkpoint {path}: {exc}") from exc
    if state.get("schema") != CHECKPOINT_SCHEMA:
        raise PipelineError(f"unsupported reference checkpoint schema in {path}")
    return state


def validate_checkpoint(state: dict, *, episode: str, workflow_version: str,
                        manifest_sha256: str, manifest_non_reference_sha256: str,
                        current_reference: str | None, original_reference: str | None,
                        captures: list[str], input_fingerprint: list[dict],
                        orc_fingerprint: dict, profile_name: str, machine: str | None,
                        profile_sha256: str, reselect: bool, dry_run: bool) -> bool:
    expected = {
        "episode": episode,
        "workflow_version": workflow_version,
        "original_reference": original_reference,
        "manifest_non_reference_sha256": manifest_non_reference_sha256,
        "captures": captures,
        "input_fingerprint": input_fingerprint,
        "orc_fingerprint": orc_fingerprint,
        "profile_name": profile_name,
        "machine": machine,
        "profile_sha256": profile_sha256,
        "reselect": reselect,
        "dry_run": dry_run,
    }
    changed = [key for key, value in expected.items() if state.get(key) != value]
    if changed:
        raise PipelineError("cannot resume reference selection; inputs/identity changed: "
                            + ", ".join(changed))
    original_sha = state.get("manifest_sha256")
    if manifest_sha256 == original_sha and current_reference == original_reference:
        manifest_already_committed = False
    elif (state.get("selected") is not None and
          manifest_sha256 == state.get("selected_manifest_sha256") and
          current_reference == state.get("selected")):
        manifest_already_committed = True
    else:
        raise PipelineError("cannot resume reference selection; manifest changed outside the saved selection")
    decisions = state.get("decisions")
    if not isinstance(decisions, list):
        raise PipelineError("reference checkpoint has no completed-pass list")
    if not isinstance(state.get("next_pass"), int) or not 1 <= state["next_pass"] <= MAX_REFERENCE_PASSES + 1:
        raise PipelineError("reference checkpoint has an invalid next_pass")
    if len(decisions) != state["next_pass"] - 1:
        raise PipelineError("reference checkpoint pass count and next_pass disagree")
    if not re.fullmatch(r"\d{8}T\d{12}Z", str(state.get("selection_id", ""))):
        raise PipelineError("reference checkpoint has an invalid selection_id")
    for index, item in enumerate(decisions, 1):
        if not isinstance(item, dict) or item.get("pass") != index:
            raise PipelineError("reference checkpoint pass sequence is invalid")
        seed = item.get("seed")
        if seed not in captures:
            raise PipelineError(f"reference checkpoint has an unknown pass seed: {seed!r}")
        expected_order = [seed] + [label for label in captures if label != seed]
        if item.get("order") != expected_order:
            raise PipelineError(f"reference checkpoint input order is invalid for pass {index}")
        decision = item.get("decision") or {}
        if decision.get("suggested_label") not in captures:
            raise PipelineError(f"reference checkpoint has an invalid recommendation in pass {index}")
    seed = state.get("next_seed")
    selected = state.get("selected")
    if seed is not None and seed not in captures:
        raise PipelineError(f"reference checkpoint has an unknown next seed: {seed!r}")
    if selected is not None and selected not in captures:
        raise PipelineError(f"reference checkpoint has an unknown selected capture: {selected!r}")
    if decisions:
        last = decisions[-1]
        recommendation = last["decision"]["suggested_label"]
        if selected is not None:
            if selected != last["seed"] or recommendation != selected or seed is not None:
                raise PipelineError("stable selection checkpoint state is inconsistent")
        elif seed != recommendation:
            raise PipelineError("checkpoint next seed does not match the last Orc recommendation")
    return manifest_already_committed


def restore_checkpoint_decisions(state: dict, probe_root: Path, log_root: Path) -> tuple[list[dict], Path | None]:
    """Rehydrate completed-pass summaries only after verifying their saved projects/logs."""
    decisions = []
    stable_project = None
    for item in state["decisions"]:
        project_name = item.get("project_name")
        log_name = item.get("refresh_log_name")
        builder_name = item.get("builder_log_name")
        if not all(isinstance(name, str) and Path(name).name == name
                   for name in (project_name, log_name, builder_name)):
            raise PipelineError("checkpoint contains an unsafe artifact filename")
        project = probe_root / project_name
        refresh_log = log_root / log_name
        builder_log = log_root / builder_name
        if (not project.is_file() or project.is_symlink() or
                hashlib.sha256(project.read_bytes()).hexdigest() != item.get("project_sha256")):
            raise PipelineError(f"checkpointed Orc project is missing or changed: {project_name}")
        if not refresh_log.is_file() or not builder_log.is_file():
            raise PipelineError(f"checkpointed Orc/build logs are missing for pass {item.get('pass')}")
        if (hashlib.sha256(refresh_log.read_bytes()).hexdigest() != item.get("refresh_log_sha256") or
                hashlib.sha256(builder_log.read_bytes()).hexdigest() != item.get("builder_log_sha256")):
            raise PipelineError(f"checkpointed Orc/build log changed for pass {item.get('pass')}")
        decision = dict(item.get("decision") or {})
        decision.update({
            "pass": item["pass"],
            "order": item["order"],
            "log_path": str(refresh_log),
            "completed": True,
            "saved": True,
            "exit_code": item.get("exit_code", 0),
        })
        decisions.append(decision)
        if state.get("selected") and item["pass"] == len(state["decisions"]):
            stable_project = project
    return decisions, stable_project


def _build_scratch_project(
    episode: str,
    reference: str,
    project_path: Path,
    sink_path: Path,
    builder_log: Path,
) -> None:
    command = [
        sys.executable,
        str(BUNDLE / "scripts" / "build_stack_project.py"),
        "--episode", episode,
        "--reference-override", reference,
        "--project-out", str(project_path),
        "--master-out", str(sink_path),
    ]
    result = _run_and_log(command, builder_log)
    if result.returncode != 0 or not project_path.is_file():
        raise PipelineError(
            "could not build Orc reference-probe project; see "
            f"{builder_log}\n{_tail(result.stdout + chr(10) + result.stderr)}"
        )


def _run_orc_refresh(orc: Path, project_path: Path, log_path: Path) -> tuple[dict, int]:
    # Intentionally omit --process: this refreshes analysis/maps, never renders video.
    command = [str(orc), "tools", "refresh", str(project_path), "--json"]
    result = _run_and_log(command, log_path)
    report = parse_refresh_report([
        result.stdout,
        result.stderr,
        result.stdout + "\n" + result.stderr,
        result.stderr + "\n" + result.stdout,
    ])
    # Orc can return non-zero even when the saved report says the refresh completed.
    return report, result.returncode


def _report_summary(
    episode: str,
    run_id: str,
    original_reference: str | None,
    decisions: list[dict],
    final_project: Path | None,
    manifest_path: Path,
    status: str,
    error: str | None = None,
) -> str:
    lines = [
        f"# Automatic Orc reference selection — {episode}",
        "",
        f"- Run ID: `{run_id}`",
        f"- Status: **{status}**",
        f"- Manifest reference before selection: `{original_reference or '(unset)'}`",
        f"- Manifest: `{manifest_path}`",
    ]
    if final_project:
        lines.append(f"- Refreshed project: `{final_project}`")
    if error:
        lines.extend(["", "## Stop reason", "", error])
    if decisions:
        lines.extend([
            "",
            "## Orc refresh decisions",
            "",
            "| Pass | Sources | Input order | Suggested reference | Runner-up | Stack sigma | Coverage 1/2/3+ | Fit trusted | Saved/completed |",
            "|---:|---:|---|---|---|---:|---|---|---|",
        ])
        for decision in decisions:
            lines.append(
                "| {pass_no} | {source_count} | `{order}` | `{suggested}` (input {index}) | `{runner}` | "
                "{sigma} | {coverage} | {fit} | {saved}/{completed} |".format(
                    pass_no=decision["pass"],
                    source_count=decision["source_count"],
                    order=", ".join(decision["order"]),
                    suggested=decision["suggested_label"],
                    index=decision["suggested_index"],
                    runner=decision["runner_up_label"] or "not reported",
                    sigma=(decision["stack_sigma"] if decision["stack_sigma"] is not None
                           else "not reported"),
                    coverage="{}/{}/{}".format(
                        decision["frames_with_1_source"] if decision["frames_with_1_source"] is not None else "?",
                        decision["frames_with_2_sources"] if decision["frames_with_2_sources"] is not None else "?",
                        decision["frames_with_3plus_sources"] if decision["frames_with_3plus_sources"] is not None else "?",
                    ),
                    fit=(decision["fit_trusted"] if decision["fit_trusted"] is not None
                         else "not reported"),
                    saved=decision["saved"],
                    completed=decision["completed"],
                )
            )
            if decision.get("alignment_map"):
                lines.append(f"\nMeasured alignment map: `{decision['alignment_map']}`")
            lines.append(f"\nRaw Orc refresh log: `{decision['log_path']}`")
    lines.extend([
        "",
        "Orc's `suggestedReference` is the selector; no noise-only custom score is used.",
        "This helper does not render the master. Continue through the existing stack-quality gate.",
    ])
    return "\n".join(lines) + "\n"


def _commit_manifest_reference(manifest_path: Path, original_sha: str,
                               reference: str, run_id: str) -> None:
    current_bytes = manifest_path.read_bytes()
    desired_bytes = manifest_bytes_with_reference(current_bytes, reference)
    desired_sha = hashlib.sha256(desired_bytes).hexdigest()
    current_sha = hashlib.sha256(current_bytes).hexdigest()
    if current_sha == desired_sha:
        return
    if current_sha != original_sha:
        raise PipelineError(
            f"episode manifest changed during Orc refresh; not overwriting {manifest_path}"
        )
    temp_path = manifest_path.with_name(f".{manifest_path.name}.autoref-{run_id}.tmp")
    if temp_path.exists():
        raise PipelineError(f"refusing to overwrite temporary manifest {temp_path}")
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(desired_bytes.decode("utf-8"))
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != original_sha:
            raise PipelineError(
                f"episode manifest changed before commit; not overwriting {manifest_path}"
            )
        os.replace(temp_path, manifest_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_selection_report(path: Path, body: str, ld: Ld) -> None:
    safe = redact_shared_text(body, ld)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(safe)
    except FileExistsError as exc:
        raise PipelineError(f"refusing to overwrite selection report: {path}") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--reselect", action="store_true",
                    help="allow Orc to replace an already declared manifest reference")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the refresh loop and report the choice without committing project/manifest")
    ap.add_argument("--resume", metavar="CHECKPOINT",
                    help="resume after verifying the saved manifest, captures, Orc tool, and completed passes")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("two-part Emissary needs one project per side; this selector handles one-part episodes only")
    captures = ep.capture_set
    if not MIN_CAPTURES <= len(captures) <= MAX_CAPTURES:
        raise PipelineError(f"weighted stack needs {MIN_CAPTURES}..{MAX_CAPTURES} captures; found {len(captures)}")
    if len(captures) != len(set(captures)):
        raise PipelineError(f"capture labels must be unique: {captures}")

    manifest_path = episode_manifest_path(ep.code, ld, series=getattr(ep, "series", "DS9"))
    current_manifest_bytes = manifest_path.read_bytes()
    current_manifest_sha = hashlib.sha256(current_manifest_bytes).hexdigest()
    current_manifest_data = json.loads(current_manifest_bytes.decode("utf-8"))
    current_reference = current_manifest_data.get("reference") or None
    original_reference = current_reference
    if current_reference and current_reference not in captures:
        raise PipelineError(f"manifest reference {current_reference!r} is not in captures {captures}")
    final_project = ep.path("stack_project")
    if not args.dry_run and final_project.exists() and not args.resume:
        raise PipelineError(
            f"refusing to overwrite existing Orc project {final_project}; "
            "finish or archive that run before selecting a new reference"
        )

    # Do not spend time mapping/choosing references until the downloaded inputs pass.
    validator = BUNDLE / "scripts" / "validate_episode.py"
    validation = subprocess.run(
        [sys.executable, str(validator), "--episode", ep.code, "--stage", "inputs"],
        cwd=str(BUNDLE), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if validation.returncode != 0:
        raise PipelineError(
            "input validation must pass before Orc reference selection:\n"
            + _tail(validation.stdout + "\n" + validation.stderr)
        )

    probe_root = ld.root("probe")
    log_root = ld.root("logs")
    probe_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    orc = Path(ld.tool("orc"))
    workflow_version = (BUNDLE / "WORKFLOW_VERSION").read_text(encoding="ascii").strip()
    inputs = capture_input_fingerprint(ep)
    orc_fingerprint = file_fingerprint(orc)
    profile_sha256 = hashlib.sha256(ld.profile_path.read_bytes()).hexdigest()
    invocation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    original_seed = original_reference or captures[0]
    manifest_already_committed = False

    if args.resume:
        supplied_checkpoint = Path(args.resume).expanduser()
        if supplied_checkpoint.is_symlink():
            raise PipelineError(f"reference checkpoint may not be a symlink: {supplied_checkpoint}")
        checkpoint_path = supplied_checkpoint.resolve()
        try:
            checkpoint_path.relative_to(log_root.resolve())
        except ValueError as exc:
            raise PipelineError("resume checkpoint must be in this episode/device logs folder") from exc
        state = load_checkpoint(checkpoint_path)
        original_reference = state.get("original_reference")
        if original_reference and original_reference not in captures:
            raise PipelineError(f"checkpoint reference {original_reference!r} is not in captures {captures}")
        original_seed = original_reference or captures[0]
        manifest_already_committed = validate_checkpoint(
            state, episode=ep.code, workflow_version=workflow_version,
            manifest_sha256=current_manifest_sha,
            manifest_non_reference_sha256=manifest_non_reference_digest(current_manifest_data),
            current_reference=current_reference, original_reference=original_reference,
            captures=list(captures), input_fingerprint=inputs,
            orc_fingerprint=orc_fingerprint,
            profile_name=ld.profile_path.name, machine=ld.profile.get("machine"),
            profile_sha256=profile_sha256,
            reselect=args.reselect, dry_run=args.dry_run)
        original_sha = state["manifest_sha256"]
        if state.get("finalized"):
            raise PipelineError("reference checkpoint is already finalized; start a new selection only with operator review")
        selection_id = state["selection_id"]
        decisions, stable_project = restore_checkpoint_decisions(state, probe_root, log_root)
        seen = [item["seed"] for item in state["decisions"]]
        current_seed = state.get("next_seed")
        selected = state.get("selected")
        next_pass = state["next_pass"]
        if selected and stable_project is None:
            raise PipelineError("checkpoint marks a stable selection but its verified project is missing")
        if final_project.exists():
            if (not selected or not stable_project or
                    hashlib.sha256(final_project.read_bytes()).digest() !=
                    hashlib.sha256(stable_project.read_bytes()).digest()):
                raise PipelineError(f"existing final project does not match the resumed checkpoint: {final_project}")
        elif manifest_already_committed:
            raise PipelineError("manifest already records the selected reference but the final Orc project is missing")
        if not selected and current_seed not in captures:
            raise PipelineError(f"checkpoint has no valid next reference seed: {current_seed!r}")
        artifact_run_id = invocation_id  # don't overwrite a partial pass from the interrupted attempt
        final_report_path = log_root / f"{ep.code}-reference-selection-resume-{invocation_id}.md"
    else:
        selection_id = invocation_id
        original_sha = current_manifest_sha
        checkpoint_path = log_root / f"{ep.code}-reference-selection-{selection_id}.checkpoint.json"
        state = {
            "schema": CHECKPOINT_SCHEMA,
            "selection_id": selection_id,
            "episode": ep.code,
            "workflow_version": workflow_version,
            "manifest_sha256": original_sha,
            "manifest_non_reference_sha256": manifest_non_reference_digest(current_manifest_data),
            "selected_manifest_sha256": None,
            "original_reference": original_reference,
            "captures": list(captures),
            "input_fingerprint": inputs,
            "orc_fingerprint": orc_fingerprint,
            "profile_name": ld.profile_path.name,
            "machine": ld.profile.get("machine"),
            "profile_sha256": profile_sha256,
            "reselect": bool(args.reselect),
            "dry_run": bool(args.dry_run),
            "decisions": [],
            "next_seed": original_seed,
            "next_pass": 1,
            "selected": None,
            "stable_project_name": None,
            "finalized": False,
        }
        write_checkpoint(checkpoint_path, state, ld, create=True)
        decisions = []
        seen = []
        current_seed = original_seed
        selected = None
        stable_project = None
        next_pass = 1
        artifact_run_id = selection_id
        final_report_path = log_root / f"{ep.code}-reference-selection-{selection_id}.md"

    print(f"reference-selection checkpoint: {redact_shared_text(str(checkpoint_path), ld)}", flush=True)
    try:
        if selected is None:
            for pass_no in range(next_pass, MAX_REFERENCE_PASSES + 1):
                if current_seed in seen:
                    raise PipelineError(
                        "Orc recommendation cycled through " + ", ".join(seen + [current_seed])
                    )
                seen.append(current_seed)
                order = [current_seed] + [label for label in captures if label != current_seed]
                project = probe_root / f"{ep.code}-reference-{artifact_run_id}-pass{pass_no}.orcprj"
                sink = ep.path("stack_master")
                builder_log = log_root / f"{ep.code}-reference-{artifact_run_id}-build{pass_no}.log"
                _build_scratch_project(ep.code, current_seed, project, sink, builder_log)

                refresh_log = log_root / f"{ep.code}-reference-{artifact_run_id}-orc{pass_no}.log"
                report, exit_code = _run_orc_refresh(orc, project, refresh_log)
                decision = source_alignment_decision(report, order)
                decision.update({
                    "pass": pass_no,
                    "order": order,
                    "log_path": str(refresh_log),
                    "completed": report.get("completed"),
                    "saved": report.get("saved"),
                    "exit_code": exit_code,
                })
                if exit_code != 0:
                    print("WARN: Orc returned a nonzero exit, but completed/saved artifacts were verified")
                if not project.is_file() or project.stat().st_size == 0:
                    raise PipelineError(f"Orc did not save the scratch project {project}")
                project_hash = hashlib.sha256(project.read_bytes()).hexdigest()

                recommended = decision["suggested_label"]
                if original_reference and not args.reselect and recommended != original_reference:
                    raise PipelineError(
                        f"Orc recommends {recommended!r}, but the manifest already declares "
                        f"{original_reference!r}; rerun with --reselect only if you intend to replace it"
                    )
                is_stable = recommended == current_seed
                checkpoint_decision = {key: value for key, value in decision.items()
                                       if key not in ("pass", "order", "log_path", "completed", "saved", "exit_code")}
                state["decisions"].append({
                    "pass": pass_no,
                    "seed": current_seed,
                    "order": order,
                    "decision": checkpoint_decision,
                    "project_name": project.name,
                    "project_sha256": project_hash,
                    "builder_log_name": builder_log.name,
                    "refresh_log_name": refresh_log.name,
                    "builder_log_sha256": hashlib.sha256(builder_log.read_bytes()).hexdigest(),
                    "refresh_log_sha256": hashlib.sha256(refresh_log.read_bytes()).hexdigest(),
                    "exit_code": exit_code,
                })
                state["next_pass"] = pass_no + 1
                state["next_seed"] = None if is_stable else recommended
                state["selected"] = current_seed if is_stable else None
                state["stable_project_name"] = project.name if is_stable else None
                if is_stable:
                    state["selected_manifest_sha256"] = (
                        original_sha if current_seed == original_reference else
                        hashlib.sha256(manifest_bytes_with_reference(
                            current_manifest_bytes, current_seed)).hexdigest())
                else:
                    state["selected_manifest_sha256"] = None
                state["updated_utc"] = datetime.now(timezone.utc).isoformat()
                write_checkpoint(checkpoint_path, state, ld)
                decisions.append(decision)
                print(f"saved completed Orc pass {pass_no}; safe resume point recorded", flush=True)
                if is_stable:
                    selected, stable_project = current_seed, project
                    break
                current_seed = recommended

        if selected is None or stable_project is None:
            raise PipelineError(
                f"Orc's recommendation did not stabilize within {MAX_REFERENCE_PASSES} full refreshes; "
                f"resume from {checkpoint_path} only after checking the saved artifacts"
            )

        status = "DRY RUN - not committed" if args.dry_run else "SELECTED"
        if not args.dry_run:
            if final_project.exists():
                if (not args.resume or
                        hashlib.sha256(final_project.read_bytes()).digest() !=
                        hashlib.sha256(stable_project.read_bytes()).digest()):
                    raise PipelineError(f"refusing to overwrite existing Orc project {final_project}")
            else:
                final_project.parent.mkdir(parents=True, exist_ok=True)
                with final_project.open("xb") as destination, stable_project.open("rb") as source:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                if hashlib.sha256(final_project.read_bytes()).digest() != hashlib.sha256(stable_project.read_bytes()).digest():
                    raise PipelineError(f"promoted Orc project copy failed hash verification: {final_project}")
            if selected != original_reference and not manifest_already_committed:
                _commit_manifest_reference(manifest_path, original_sha, selected, selection_id)

        summary = _report_summary(ep.code, selection_id, original_reference, decisions,
                                  stable_project if args.dry_run else final_project,
                                  manifest_path, status)
        _write_selection_report(final_report_path, summary, ld)
        state["finalized"] = True
        state["finalized_utc"] = datetime.now(timezone.utc).isoformat()
        write_checkpoint(checkpoint_path, state, ld)
        final_decision = decisions[-1]
        print(f"Orc-selected reference: {selected}")
        print(f"stable input order: {', '.join(final_decision['order'])}")
        print(f"refreshed project: {stable_project if args.dry_run else final_project}")
        print(f"selection report: {redact_shared_text(str(final_report_path), ld)}")
        print(f"resume checkpoint: {redact_shared_text(str(checkpoint_path), ld)}")
        print("No master render was run; continue with the existing stack-quality gate.")
        return 0
    except KeyboardInterrupt:
        summary = _report_summary(ep.code, selection_id, original_reference, decisions,
                                  stable_project, manifest_path, "INTERRUPTED",
                                  "operator interrupted the refresh; partial logs are preserved")
        _write_selection_report(final_report_path, summary, ld)
        print(f"Interrupted; resume with --resume {redact_shared_text(str(checkpoint_path), ld)}",
              file=sys.stderr)
        return 130
    except PipelineError as exc:
        summary = _report_summary(ep.code, selection_id, original_reference, decisions,
                                  stable_project, manifest_path, "STOP", str(exc))
        _write_selection_report(final_report_path, summary, ld)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"selection report: {redact_shared_text(str(final_report_path), ld)}", file=sys.stderr)
        print(f"resume checkpoint: {redact_shared_text(str(checkpoint_path), ld)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
