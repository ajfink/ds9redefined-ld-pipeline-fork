#!/usr/bin/env python3
"""Run the allowlisted Orc weighted-stack render and verify its reported artifact.

The wrapper checks the measured space budget, refuses an existing project/master
or output, streams Orc's log, and compares the documented render evidence with the
manifest and FFprobe. It does not approve stack quality; include a workflow review
step after this stage before cadence work.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import queue
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode, redact_shared_text  # noqa: E402
from streamutil import configure_console, start_line_reader  # noqa: E402

LOADED_RE = re.compile(r"\btbc_source:\s*Loaded\b", re.IGNORECASE)
WRITTEN_RE = re.compile(r"Successfully\s+wrote\s+([\d,]+)\s+frames", re.IGNORECASE)
UNRECOVERABLE_RE = re.compile(r"unrecoverable(?:\s+samples?)?\D{0,20}([\d,]+)", re.IGNORECASE)
PAIR_NUMBER = r"\d+(?:,\d{3})*"
PAIR_RE = re.compile(rf"(?:\bpairs?\s*[:=]\s*({PAIR_NUMBER})|({PAIR_NUMBER})\s+pairs?\b)", re.IGNORECASE)
ORC_HEARTBEAT_SECONDS = 60
ORC_STALL_WARNING_SECONDS = 20 * 60


def expected_pair_count(input_count: int) -> int:
    """Orc reports all source pairs; pressing groups are checked separately."""
    return input_count * (input_count - 1) // 2


def expected_pressing_groups(labels: list[str], groups) -> list[tuple[int, ...]] | None:
    """Map explicit manifest capture groups to 1-based Orc input indices."""
    if not isinstance(groups, list) or not groups:
        return None
    index = {label: position + 1 for position, label in enumerate(labels)}
    expected = []
    seen = []
    for group in groups:
        if isinstance(group, dict):
            members = group.get("value") or group.get("captures") or group.get("labels")
        else:
            members = group
        if not isinstance(members, list) or not all(isinstance(x, str) for x in members):
            return None
        if any(member not in index for member in members):
            return None
        seen.extend(members)
        expected.append(tuple(sorted(index[member] for member in members)))
    if len(seen) != len(set(seen)) or set(seen) != set(labels):
        return None
    return sorted(expected)


def project_source_labels(project: Path) -> list[str]:
    text = project.read_text(encoding="utf-8", errors="replace")
    labels = []
    for block in re.split(r"(?m)^    - id: ", text)[1:]:
        stage = re.search(r"(?m)^      stage: (\S+)\s*$", block)
        label = re.search(r"(?m)^      user_label: TBC Source (.+?)\s*$", block)
        if stage and stage.group(1) == "tbc_source" and label:
            labels.append(label.group(1))
    return labels


def stacker_reports(lines: list[str]) -> list[dict]:
    decoder = json.JSONDecoder()
    reports = []
    for line in lines:
        if "StackerStage" not in line:
            continue
        for match in re.finditer(r"\{", line):
            try:
                value, _ = decoder.raw_decode(line, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("inputs"), list):
                reports.append(value)
                break
    return reports


def reported_pressing_groups(text: str) -> list[tuple[int, ...]] | None:
    pattern = re.compile(r"inputs grouped by pressing[^:]*:\s*((?:\{\s*\d+(?:\s*,\s*\d+)*\s*\}\s*)+)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    groups = [tuple(sorted(int(value) for value in re.findall(r"\d+", item)))
              for item in re.findall(r"\{[^}]+\}", match.group(1))]
    return sorted(groups)


def validate_render_evidence(text: str, labels: list[str], expected_frames: int,
                             actual_frames: int, groups) -> dict:
    loaded_lines = [line for line in text.splitlines() if LOADED_RE.search(line)]
    loaded_counts = {label: sum(label.casefold() in line.casefold() for line in loaded_lines)
                     for label in labels}
    written = [int(value.replace(",", "")) for value in WRITTEN_RE.findall(text)]
    unrecoverable = [int(value.replace(",", "")) for value in UNRECOVERABLE_RE.findall(text)]
    lines = text.splitlines()
    stacker_lines = [line for line in lines if "StackerStage" in line]
    reports = stacker_reports(lines)
    input_counts = [len(report["inputs"]) for report in reports]
    pair_counts = [int(report["pairs"]) for report in reports
                   if isinstance(report.get("pairs"), (int, float))]
    if not pair_counts:
        pair_counts = [int((left or right).replace(",", "")) for line in stacker_lines
                       for left, right in PAIR_RE.findall(line)]
    wanted_pairs = expected_pair_count(len(labels))
    expected_groups = expected_pressing_groups(labels, groups)
    reported_groups = reported_pressing_groups(text)
    issues = []
    review_flags = []
    if len(loaded_lines) != len(labels) or any(count != 1 for count in loaded_counts.values()):
        issues.append(f"expected one Loaded line per capture; got {len(loaded_lines)} lines, labels={loaded_counts}")
    if not stacker_lines:
        issues.append("no StackerStage multi-input evidence was found")
    if not reports:
        issues.append("no structured StackerStage input-count report was found")
    elif any(count != len(labels) for count in input_counts):
        issues.append(f"StackerStage input counts {input_counts} differ from {len(labels)} captures")
    if not unrecoverable:
        review_flags.append("render log did not report an unrecoverable-sample count; verify in Orc output")
    elif any(count != 0 for count in unrecoverable):
        issues.append(f"unrecoverable samples were reported: {unrecoverable}")
    if not written:
        issues.append("render log did not contain 'Successfully wrote N frames'")
    elif any(count != expected_frames for count in written):
        issues.append(f"rendered frame count(s) {written} differ from measured stack.frames={expected_frames}")
    if actual_frames != expected_frames:
        issues.append(f"FFprobe frame count {actual_frames} differs from measured stack.frames={expected_frames}")
    if written and any(count != actual_frames for count in written):
        issues.append(f"Orc written-frame count {written} differs from FFprobe frame count {actual_frames}")
    pair_check = "not-reported"
    if pair_counts:
        if any(count != wanted_pairs for count in pair_counts):
            pair_check = f"fail: reported={pair_counts}, expected={wanted_pairs}"
            issues.append(f"Orc pair count(s) {pair_counts} differ from {len(labels)}-capture all-pairs expectation {wanted_pairs}")
        else:
            pair_check = f"pass: {wanted_pairs} all-pairs"
    else:
        review_flags.append("StackerStage pair count was not reported; verify actual capture-pair evidence")
    if expected_groups is None:
        review_flags.append("manifest pressing-group membership is unavailable or incomplete")
        group_check = "review-required"
    elif reported_groups is None:
        review_flags.append("Orc log did not report pressing groups; verify against manifest")
        group_check = "not-reported"
    elif reported_groups != expected_groups:
        issues.append(f"Orc pressing groups {reported_groups} differ from manifest groups {expected_groups}")
        group_check = "fail"
    else:
        group_check = "pass"
    return {
        "loaded_capture_lines": len(loaded_lines),
        "loaded_per_capture": loaded_counts,
        "stacker_stage_lines": len(stacker_lines),
        "stacker_input_counts": input_counts,
        "orc_written_frames": written,
        "ffprobe_frames": actual_frames,
        "expected_frames": expected_frames,
        "unrecoverable_counts": unrecoverable,
        "pair_count_check": pair_check,
        "pressing_group_check": group_check,
        "issues": issues,
        "review_flags": review_flags,
        "ok": not issues,
    }


def ffprobe_frames(ld: Ld, master: Path) -> int:
    proc = subprocess.run(
        [ld.ffprobe(), "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(master)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise PipelineError(f"ffprobe could not inspect rendered stack master: {proc.stderr[-600:]}")
    values = [line.strip() for line in proc.stdout.splitlines() if line.strip().isdigit()]
    if len(values) != 1:
        raise PipelineError(f"ffprobe did not return one frame count for {master}: {proc.stdout!r}")
    return int(values[0])


def check_space(ld: Ld, planned_gib: float) -> None:
    if planned_gib <= 0:
        raise PipelineError("--planned-gib must be a positive measured estimate")
    check = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("check_space_budget.py")),
         "--stage", "stack-render", "--planned-gib", str(planned_gib),
         "--root", "prores", "--profile", str(ld.profile_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((check.stdout or "").strip(), flush=True)
    if check.returncode != 0:
        raise PipelineError("stack-render space gate did not pass")


def write_report(path: Path, report: dict, ld: Ld) -> None:
    assert_writable_path(path, ld)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = redact_shared_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", ld)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(safe)


def progress_heartbeat(elapsed_s: float, last_progress_s: float, pid: int,
                       process_alive: bool, master_bytes: int) -> tuple[str, bool]:
    """Return a visible heartbeat and whether the run has crossed the advisory stall threshold."""
    idle_s = max(0.0, elapsed_s - last_progress_s)
    message = (f"ORC PROGRESS WATCHDOG: pid={pid}; elapsed={elapsed_s:.0f}s; "
               f"no log/output progress={idle_s:.0f}s; process_alive={process_alive}; "
               f"master_bytes={master_bytes}; no automatic kill/retry")
    return message, idle_s >= ORC_STALL_WARNING_SECONDS


def move_existing_master_to_backup(master: Path, run_id: str) -> Path:
    """Keep a restart target recoverable, then clear its original path for Orc."""
    if master.is_symlink() or not master.is_file():
        raise PipelineError(f"existing stack master is missing or unsafe: {master}")
    original_stat = master.stat()
    backup = master.with_name(f"{master.name}.bak-restart-{run_id}")
    if backup.exists() or backup.is_symlink():
        raise PipelineError(f"refusing to reuse existing stack-master backup: {backup}")
    current_stat = master.stat()
    if (current_stat.st_size, current_stat.st_mtime_ns) != (
            original_stat.st_size, original_stat.st_mtime_ns):
        raise PipelineError(f"existing stack master changed before restart: {master}")
    master.replace(backup)
    return backup


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--planned-gib", required=True, type=float,
                        help="defensible stack master + scratch peak estimate; preserves configured reserve")
    parser.add_argument("--report-out", help="new JSON report path; defaults to a unique profile log")
    parser.add_argument("--overwrite-stack", action="store_true",
                        help="after explicit operator restart, move the existing master to a backup before rendering")
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("generic Orc stack render handles one-part episodes only")
    labels = ep.capture_set
    if not 2 <= len(labels) <= 6:
        raise PipelineError(f"weighted stack requires 2..6 captures; found {len(labels)}")
    reference = ep.reference
    expected_order = [reference] + [label for label in labels if label != reference]
    alignment_map = (ep.data.get("alignment_map") or "").strip()
    if not alignment_map:
        raise PipelineError("measured alignment_map is required before rendering the refreshed Orc project")
    project = ep.path("stack_project")
    master = ep.path("stack_master")
    if not project.is_file():
        raise PipelineError(f"refreshed Orc project is missing: {project}")
    project_order = project_source_labels(project)
    if project_order != expected_order:
        raise PipelineError(f"Orc project input order {project_order} does not match measured reference order {expected_order}")
    existing_master_stat = None
    if master.exists():
        if not args.overwrite_stack:
            raise PipelineError(f"refusing to overwrite existing stack master: {master}")
        if master.is_symlink() or not master.is_file():
            raise PipelineError(f"existing stack master is not a safe regular file: {master}")
        existing_master_stat = (master.stat().st_size, master.stat().st_mtime_ns)
    expected_frames = ep.stack_frames
    if expected_frames <= 0:
        raise PipelineError("measured stack.frames must be positive before rendering")
    assert_writable_path(master, ld)
    report_path = (Path(args.report_out).expanduser().resolve() if args.report_out else
                   ld.root("logs") / f"{ep.code}-stack-render-{uuid.uuid4().hex}.json")
    assert_writable_path(report_path, ld)
    if report_path.exists():
        raise PipelineError(f"refusing to overwrite stack render report: {report_path}")

    check_space(ld, args.planned_gib)
    overwritten_master_backup = None
    if existing_master_stat is not None:
        current_stat = master.stat()
        if (current_stat.st_size, current_stat.st_mtime_ns) != existing_master_stat:
            raise PipelineError(f"existing stack master changed before restart: {master}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        overwritten_master_backup = move_existing_master_to_backup(master, stamp)
        print(f"moved existing stack master to backup: {overwritten_master_backup}", flush=True)
    orc = ld.tool("orc")
    command = [str(orc), str(project), "--process"]
    started = datetime.now(timezone.utc).isoformat()
    print("ORC COMMAND: " + subprocess.list2cmdline(command), flush=True)
    lines: list[str] = []
    master.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        raise PipelineError(f"could not start Orc render: {exc}") from exc
    assert proc.stdout is not None
    output_lines, reader = start_line_reader(proc.stdout, name="orc-stack-render-output")
    started_monotonic = time.monotonic()
    last_progress = started_monotonic
    last_master = (master.stat().st_size, master.stat().st_mtime_ns) if master.exists() else (0, 0)
    next_stall_notice = ORC_STALL_WARNING_SECONDS
    while True:
        try:
            line = output_lines.get(timeout=ORC_HEARTBEAT_SECONDS)
        except queue.Empty:
            now = time.monotonic()
            if master.exists():
                stat = master.stat()
                current_master = (stat.st_size, stat.st_mtime_ns)
                if current_master != last_master:
                    last_master = current_master
                    last_progress = now
            elapsed = now - started_monotonic
            heartbeat, stalled = progress_heartbeat(
                elapsed, last_progress - started_monotonic, proc.pid,
                proc.poll() is None, last_master[0])
            print(heartbeat, flush=True)
            if stalled and elapsed >= next_stall_notice:
                print("ORC STALL WARNING: preserve the process/log/output evidence and inspect manually; "
                      "this wrapper will not kill or restart a potentially locked Orc process.", flush=True)
                next_stall_notice = elapsed + ORC_STALL_WARNING_SECONDS
            continue
        if line is None:
            break
        lines.append(line)
        print(line, end="", flush=True)
        if "progress:" in line.casefold() or "decoding frames:" in line.casefold():
            last_progress = time.monotonic()
    proc.stdout.close()
    reader.join(timeout=5)
    orc_exit = proc.wait()
    text = "".join(lines)
    report = {
        "schema": "ld-pipeline-stack-render-v1",
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code,
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "orc_exit_code": orc_exit,
        "project": str(project),
        "master": str(master),
        "overwritten_master_backup": (str(overwritten_master_backup)
                                      if overwritten_master_backup else None),
        "reference_capture": reference,
        "alignment_map": alignment_map,
        "expected_captures": expected_order,
        "expected_frames": expected_frames,
    }
    if not master.is_file() or master.stat().st_size <= 0:
        report.update({"status": "FAIL", "issues": ["Orc did not produce a non-empty stack master"]})
        write_report(report_path, report, ld)
        raise PipelineError(f"Orc render produced no stack master; report: {report_path}")
    actual_frames = ffprobe_frames(ld, master)
    evidence = validate_render_evidence(
        text, expected_order, expected_frames, actual_frames,
        (ep.data.get("stack") or {}).get("pressing_groups"))
    report.update({"status": "PASS" if evidence["ok"] else "REVIEW_REQUIRED",
                   "artifact_bytes": master.stat().st_size,
                   "evidence": evidence})
    write_report(report_path, report, ld)
    if not evidence["ok"]:
        raise PipelineError("stack-render evidence is incomplete or inconsistent; inspect the Orc log and report before continuing: "
                            + str(report_path) + " | " + "; ".join(evidence["issues"]))
    if orc_exit != 0:
        print(f"WARN: Orc exited {orc_exit}, but artifact/evidence checks passed; verify its full stage log.", flush=True)
    print(f"STACK RENDER EVIDENCE PASS: report={report_path}; HUMAN QUALITY REVIEW IS STILL REQUIRED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"STACK RENDER ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
