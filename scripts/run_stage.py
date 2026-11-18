#!/usr/bin/env python3
"""Run one allowlisted pipeline stage with explicit gates and durable logs.

Planning is the default; ``--execute`` is required to launch a workflow command.
There is no arbitrary shell mode. The download stage uses downloader-specific
URL/integrity/space checks so it can start early. Other write-capable stages run
the unified doctor and declared manifest validators; each invoked helper retains
its own refuse-overwrite and quality gates.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
from dependency_lock import (LockError, profile_platform, redact_shared_text,
                             resolve_interpreter, shared_redactions)  # noqa: E402
from ldcommon import (Ld, PipelineError, device_workspace_enabled, parse_episode_identity,
                      load_episode, shared_instance_root, workspace_release_root)  # noqa: E402
from process_utils import process_group_options, safe_console_write, terminate_process_tree  # noqa: E402
from shared_workspace import resolve_instance_workspace  # noqa: E402

STAGES = {
    "download": {"kind": "download", "write": True},
    "init-episode": {"kind": "init", "write": True},
    "validate-manifest": {"kind": "validate", "manifest_stage": "manifest", "write": False},
    "validate-inputs": {"kind": "validate", "manifest_stage": "inputs", "write": False},
    "validate-stack": {"kind": "validate", "manifest_stage": "stack", "write": False},
    "validate-cadence": {"kind": "validate", "manifest_stage": "cadence", "write": False},
    "validate-audio": {"kind": "validate", "manifest_stage": "audio", "write": False},
    "validate-retime": {"kind": "validate", "manifest_stage": "retime", "write": False},
    "validate-encode": {"kind": "validate", "manifest_stage": "encode", "write": False},
    "validate-padfix": {"kind": "validate", "manifest_stage": "padfix", "write": False},
    "validate-deliverable": {"kind": "validate", "manifest_stage": "deliverable", "write": False},
    "stack-reference": {"kind": "python", "script": "select_stack_reference.py", "gate": "inputs", "write": True},
    "efm-report-table": {"kind": "python", "script": "efm_report_table.py", "gate": "inputs", "write": False},
    "verify-tfm-run": {"kind": "python", "script": "validate_tfm_run.py", "gate": "stack", "write": True},
    "build-map": {"kind": "map", "gate": "stack", "write": True},
    "motion-trace": {"kind": "python", "script": "build_motion_trace.py", "gate": "stack", "write": True},
    "master-descriptors": {"kind": "python", "script": "extract_video_descriptors.py", "fixed_args": ["--source", "master"], "gate": "stack", "write": True},
    "dvd-descriptors": {"kind": "python", "script": "extract_video_descriptors.py", "fixed_args": ["--source", "dvd"], "gate": "stack", "write": True},
    "measure-dvd-alignment": {"kind": "python", "script": "measure_dvd_alignment.py", "gate": "stack", "write": True},
    "stack-render": {"kind": "orc-render", "gate": "inputs", "write": True},
    "prepare-retime-breaks": {"kind": "python", "script": "prepare_retime_breaks.py", "gate": "stack", "write": True},
    "record-retime-breaks": {"kind": "python", "script": "record_retime_breaks.py", "gate": "inputs", "write": True},
    "measure-break-edges": {"kind": "python", "script": "measure_break_edges.py", "gate": "stack", "write": True},
    "measure-audio-rate": {"kind": "python", "script": "measure_audio_rate.py", "write": True},
    "measure-pad-audio": {"kind": "python", "script": "measure_pad_audio.py", "gate": "stack", "write": True},
    "measure-ld-black": {"kind": "python", "script": "measure_ld_black.py", "gate": "stack", "write": True},
    "build-ld-synced": {"kind": "python", "script": "build_ld_synced.py", "write": True},
    "retime-assets": {"kind": "python", "script": "retime_dvd_assets.py", "gate": "retime", "write": True},
    "audio-diagnostic": {"kind": "python", "script": "audio_timing_diagnostic.py", "gate": "stack", "write": True},
    "audio-promote-no-dvd": {"kind": "no-dvd", "gate": "stack", "write": True},
    "pad-audit": {"kind": "python", "script": "pad_audit.py", "gate": "cadence", "write": True},
    "fullres-check": {"kind": "python", "script": "fullres_check.py", "gate": "cadence", "write": True},
    "rife-replacements": {"kind": "python", "script": "rife_padframes.py", "gate": "cadence", "write": True},
    "build-padfix": {"kind": "padfix", "gate": "cadence", "write": True},
    "encode-validate": {"kind": "encode", "pass": "validate", "gate": "encode", "write": False},
    "encode-final": {"kind": "encode", "pass": "final", "gate": "encode", "write": True},
    "remux": {"kind": "remux", "gate": "audio", "write": True},
    "qc": {"kind": "python", "script": "qc_report.py", "write": True},
}


def load_profile(path: str | None) -> tuple[Path, dict]:
    chosen = path or os.environ.get("LDP_PROFILE")
    devices = ROOT / "devices"
    if not chosen and devices.is_dir():
        raise LockError("shared release detected; provide --profile to a local profile outside the release tree")
    chosen = chosen or str(ROOT / "machine.json")
    p = Path(os.path.expandvars(os.path.expanduser(chosen))).resolve()
    if not p.is_file():
        raise LockError(f"profile not found: {p}; copy the matching machine example to machine.json")
    try:
        profile = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read profile {p}: {exc}") from exc
    if devices.is_dir():
        try:
            p.relative_to(ROOT.resolve())
            raise LockError("device-workspace profiles must be outside the README/release folder")
        except ValueError:
            pass
    if device_workspace_enabled(profile) and workspace_release_root(profile) != ROOT.resolve():
        raise LockError(f"workspace/shared release root must match this README/code folder: {ROOT}")
    if devices.is_dir() and not device_workspace_enabled(profile):
        raise LockError("this README folder has devices/ data; use an external profile with shared.enabled=true")
    return p, profile


def py(profile: dict) -> str:
    return resolve_interpreter(profile)


def powershell(profile: dict) -> str:
    configured = ((profile.get("tools") or {}).get("pwsh") or "").strip()
    if configured:
        p = Path(os.path.expandvars(os.path.expanduser(configured)))
        if p.is_file():
            return str(p)
        raise LockError(f"profile tools.pwsh points at a missing executable: {p}")
    for name in ("pwsh", "powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise LockError("PowerShell not found; configure tools.pwsh or install a supported PowerShell")


def python_command(profile: dict, script: str, episode: str, extra: list[str]) -> list[str]:
    return [py(profile), str(SCRIPTS / script), "--episode", episode, *extra]


def make_action(stage: str, episode: str, profile: dict, extra: list[str],
                report_out: str | None, series: str = "DS9") -> list[str]:
    spec = STAGES[stage]
    os_name = profile_platform(profile)
    kind = spec["kind"]
    if kind == "validate":
        return [py(profile), str(SCRIPTS / "validate_episode.py"), "--episode", episode,
                "--stage", spec["manifest_stage"]]
    if kind == "python":
        fixed_args = spec.get("fixed_args", [])
        fixed_options = {arg.split("=", 1)[0] for arg in fixed_args if arg.startswith("-")}
        extra_options = {arg.split("=", 1)[0] for arg in extra if arg.startswith("-")}
        conflicts = sorted(fixed_options & extra_options)
        if conflicts:
            raise LockError(f"stage {stage!r} fixes argument(s) {', '.join(conflicts)}; do not override them")
        return python_command(profile, spec["script"], episode, [*extra, *fixed_args])
    if kind == "orc-render":
        if extra:
            raise LockError("stack-render takes no arbitrary Orc arguments; review/use the manifest project")
        return [str((profile.get("tools") or {}).get("orc") or "<tools.orc>"),
                "<manifest stack_project>", "--process"]
    if kind == "init":
        series_args = ["--series", series] if series != "DS9" else []
        return [py(profile), str(SCRIPTS / "init_episode.py"), *series_args, *extra]
    if kind == "download":
        return [powershell(profile), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(SCRIPTS / "download_archive.ps1"), *extra]
    if kind == "no-dvd":
        if not report_out:
            raise LockError("audio-promote-no-dvd requires --report-out <new-unique-report.json>")
        return python_command(profile, "audio_timing_diagnostic.py", episode,
                              ["--promote-dvd-free", "--report-out", report_out, *extra])
    if kind == "map":
        if os_name == "windows":
            return [powershell(profile), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(SCRIPTS / "build_map.ps1"), "-Episode", episode, *extra]
        if os_name == "macos":
            return python_command(profile, "build_map_vpy.py", episode, extra)
        raise LockError("Linux cadence/build-map execution is not enabled: the full Linux workflow is unvalidated")
    if kind == "padfix":
        if os_name == "windows":
            return python_command(profile, "build_padfix_avs.py", episode, extra)
        if os_name == "macos":
            return python_command(profile, "build_padfix_vpy.py", episode, extra)
        raise LockError("Linux PAD repair execution is not enabled; the full Linux workflow is unvalidated")
    if kind == "encode":
        if os_name == "windows":
            return [powershell(profile), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(SCRIPTS / "encode_episode.ps1"), "-Episode", episode,
                    "-Pass", spec["pass"], *extra]
        if os_name == "macos":
            return python_command(profile, "encode_episode_mac.py", episode,
                                  ["--pass", spec["pass"], *extra])
        raise LockError("Linux encoding is not enabled; the full Linux workflow is unvalidated")
    if kind == "remux":
        if os_name == "windows":
            return [powershell(profile), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(SCRIPTS / "remux_padfixed.ps1"), "-Episode", episode, *extra]
        if os_name == "macos":
            return python_command(profile, "remux_padfixed_mac.py", episode, extra)
        raise LockError("Linux delivery/remux is not enabled; the full Linux workflow is unvalidated")
    raise LockError(f"unknown stage implementation: {kind}")


def validate_command(episode: str, profile: dict, manifest_stage: str) -> list[str]:
    return [py(profile), str(SCRIPTS / "validate_episode.py"), "--episode", episode,
            "--stage", manifest_stage]


def requires_doctor(stage: str) -> bool:
    """Downloads have their own URL, tool-path, metadata, integrity and space gates.

    Keep them startable while the full toolchain inventory/readiness check runs in
    parallel; all other write stages retain the complete doctor gate.
    """
    return bool(STAGES[stage]["write"] and stage != "download")


def display(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def run_captured(command: list[str], env: dict[str, str], log_path: Path,
                 redactions: list[tuple[str, str]] | None = None,
                 cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=cwd)
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("COMMAND: " + redact_shared_text(display(command), redactions or []) + "\n")
        log.write(redact_shared_text(proc.stdout or "", redactions or []))
        if proc.stderr:
            log.write("\n[stderr]\n" + redact_shared_text(proc.stderr, redactions or []))
        log.write(f"\nEXIT CODE: {proc.returncode}\n\n")
    if proc.stdout:
        safe_console_write(proc.stdout)
    if proc.stderr:
        safe_console_write(proc.stderr, sys.stderr)
    return proc


def run_logged(command: list[str], env: dict[str, str], log_path: Path,
               redactions: list[tuple[str, str]] | None = None, *,
               cwd: Path | None = None, progress_path: Path | None = None,
               stall_seconds: float | None = None,
               heartbeat_seconds: float = 30.0) -> int:
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("COMMAND: " + redact_shared_text(display(command), redactions or []) + "\n\n")
        log.flush()
        options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                   "text": True, "encoding": "utf-8", "errors": "replace",
                   "bufsize": 1, "env": env, "cwd": cwd}
        options.update(process_group_options())
        proc = subprocess.Popen(command, **options)
        assert proc.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue()

        def pump_output() -> None:
            try:
                for item in proc.stdout:
                    lines.put(item)
            finally:
                lines.put(None)

        reader = threading.Thread(target=pump_output, name="stage-output-reader", daemon=True)
        reader.start()
        started = last_progress_at = time.monotonic()
        progress_stamp = None
        if progress_path and progress_path.exists():
            try:
                progress_stamp = progress_path.stat().st_mtime_ns
            except OSError:
                pass
        stale_reported = False
        last_line = "(no output yet)"
        rc = None
        try:
            while True:
                try:
                    line = lines.get(timeout=max(0.05, heartbeat_seconds))
                except queue.Empty:
                    now = time.monotonic()
                    heartbeat = (f"[stage heartbeat] elapsed={now-started:.0f}s; "
                                 f"pid={proc.pid}; last-output={last_line[-160:]}")
                    if progress_path:
                        try:
                            current_stamp = progress_path.stat().st_mtime_ns
                            if progress_stamp is None or current_stamp != progress_stamp:
                                progress_stamp = current_stamp
                                last_progress_at = now
                            quiet = now - last_progress_at
                            heartbeat += f"; progress-log-quiet={quiet:.0f}s"
                            if stall_seconds and quiet >= stall_seconds and not stale_reported:
                                stale_reported = True
                                warning = (f"[watchdog] no progress-log update for {quiet:.0f}s; "
                                           f"stopping the scoped stage process tree; log={progress_path}\n")
                                log.write(redact_shared_text(warning, redactions or []))
                                log.flush()
                                safe_console_write(warning)
                                if not terminate_process_tree(proc):
                                    note = ("[watchdog] termination not confirmed; stage may remain "
                                            "blocked in an OS-level wait; inspect PID/process and do not relaunch over outputs.\n")
                                    log.write(note)
                                    log.flush()
                                rc = 124
                                break
                        except OSError:
                            heartbeat += "; progress-log-unavailable"
                    heartbeat += "\n"
                    log.write(redact_shared_text(heartbeat, redactions or []))
                    log.flush()
                    safe_console_write(heartbeat)
                    continue
                if line is None:
                    break
                last_line = line.rstrip("\r\n") or "(blank line)"
                safe_line = redact_shared_text(line, redactions or [])
                log.write(safe_line)
                log.flush()
                safe_console_write(safe_line)
            if rc is None:
                rc = proc.wait()
            proc.stdout.close()
            reader.join(timeout=2)
        except KeyboardInterrupt:
            terminated = terminate_process_tree(proc)
            log.write("\nINTERRUPTED by operator; process-tree termination confirmed="
                      + str(terminated) + "\n")
            log.flush()
            raise
        log.write(f"\nEXIT CODE: {rc}\n")
        return int(rc)


def stack_render_inputs(profile_path: Path, episode: str, series: str,
                        instance: Path | None, env: dict[str, str]) -> tuple[list[str], Path, Path]:
    """Resolve the measured Orc project and its manifest-declared sink without writing."""
    keys = ("LDP_PROFILE", "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR",
            "LDP_INSTANCE_LOGS", "LDP_CUT_PAD_IMAGES", "LDP_INSTANCE_MODE",
            "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            if key in env:
                os.environ[key] = env[key]
            else:
                os.environ.pop(key, None)
        ld = Ld(str(profile_path))
        ep = load_episode(episode, ld, series=series)
        if not ep.data.get("reference"):
            raise LockError(f"{ep.identity}: Orc reference selection is not committed")
        project = ep.path("stack_project")
        master = ep.path("stack_master")
        return [str(ld.tool("orc")), str(project), "--process"], project, master
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def safe_working_directory(profile: dict, shared: bool,
                           requested: str | None) -> Path:
    """Keep shared-mode helper processes off an SMB-mounted source checkout."""
    if requested:
        cwd = Path(os.path.expandvars(os.path.expanduser(requested))).resolve()
    elif shared:
        # Heavy work roots may themselves be on an SMB mount. The user's home is
        # the safe neutral cwd; all bundle scripts and manifest paths are absolute.
        cwd = Path.home().resolve()
    else:
        cwd = Path.cwd().resolve()
    if not cwd.is_dir():
        raise LockError(f"working directory must exist: {cwd}")
    if shared:
        release = workspace_release_root(profile)
        try:
            cwd.relative_to(release)
        except ValueError:
            return cwd
        raise LockError("shared-mode subprocess cwd must be outside the README/release tree; "
                        "pass --working-directory to a device-local directory")
    return cwd


def write_stage_report(path: Path, profile_path: Path, episode: str, stage: str,
                       command: list[str], started: str, status: str,
                       exit_code: int | None, log_path: Path,
                       gates: list[dict], timing: dict, series: str = "DS9",
                       redactions: list[tuple[str, str]] | None = None) -> None:
    report = {"schema": "ld-pipeline-stage-run-v1",
              "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
              "episode": episode, "series": series,
              "identity": f"{series}:{episode}", "stage": stage, "profile": str(profile_path),
              "command": command, "started_utc": started,
              "finished_utc": datetime.now(timezone.utc).isoformat(),
              "exit_code": exit_code, "status": status, "gates": gates,
              "timing": timing,
              "log": str(log_path)}
    report_path = path
    if redactions:
        report = json.loads(redact_shared_text(json.dumps(report), redactions))
    with report_path.open("x", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"stage report: {report_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", help="episode code (not needed with --list)")
    ap.add_argument("--series", choices=("DS9", "VOY"), help="episode series; omitted/bare legacy codes mean DS9")
    ap.add_argument("--stage", choices=sorted(STAGES))
    ap.add_argument("--profile", help="machine profile; defaults to LDP_PROFILE or bundle machine.json")
    ap.add_argument("--instance-root", help="required per-instance episode workspace in shared mode")
    ap.add_argument("--execute", action="store_true", help="execute; default only prints the bounded plan")
    ap.add_argument("--report-out", help="required new report path for audio-promote-no-dvd")
    ap.add_argument("--working-directory",
                    help="subprocess cwd; shared mode defaults to the device-local roots.work")
    ap.add_argument("--planned-gib", type=float,
                    help="required, episode-measured peak space estimate for stack-render")
    ap.add_argument("--space-root", default="prores",
                    help="profile root receiving stack-render output (default: prores)")
    ap.add_argument("--orc-stall-timeout-seconds", type=float, default=1200.0,
                    help="stack-render no-progress watchdog (default 1200s; set from observed progress cadence)")
    ap.add_argument("--publish-reference", action="append", default=[],
                    help="after a successful stage, copy this useful report into shared references/ (shared mode only)")
    ap.add_argument("--script-arg", action="append", default=[],
                    help="one argument passed to the allowlisted stage script; repeat as needed")
    ap.add_argument("--list", action="store_true", help="list allowlisted stages")
    args = ap.parse_args()
    if args.list:
        for name, spec in STAGES.items():
            doctor_label = "required" if requires_doctor(name) else (
                "deferred" if spec["write"] else "not-needed")
            print(f"{name:24} write={str(spec['write']).lower()} gate={spec.get('gate', 'none')} doctor={doctor_label}")
        return 0
    if not args.stage or not args.episode:
        ap.error("--stage and --episode are required unless --list is used")
    if args.stage == "stack-render":
        if args.planned_gib is None or not math.isfinite(args.planned_gib) or args.planned_gib <= 0:
            ap.error("stack-render requires a finite positive --planned-gib estimate")
        if not math.isfinite(args.orc_stall_timeout_seconds) or args.orc_stall_timeout_seconds <= 0:
            ap.error("--orc-stall-timeout-seconds must be finite and positive")
    elif args.planned_gib is not None:
        ap.error("--planned-gib is only used by stack-render")
    try:
        series, episode_code, identity = parse_episode_identity(args.episode, args.series)
        profile_path, profile = load_profile(args.profile)
        os_name = profile_platform(profile)
        if os_name not in ("windows", "macos", "linux"):
            raise LockError(f"unsupported profile os: {profile.get('os')!r}")
        if os_name == "linux" and STAGES[args.stage]["write"]:
            raise LockError("Linux production stages are blocked by design; only dependency and doctor checks are available until Linux end-to-end validation")
        spec = STAGES[args.stage]
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["LDP_PROFILE"] = str(profile_path)
        env["LDP_SERIES"] = series
        env["LDP_EPISODE"] = episode_code
        env["LDP_IDENTITY"] = identity
        shared = device_workspace_enabled(profile)
        instance = None
        instance_mode = None
        workspace_warning = None
        redactions = shared_redactions(profile_path, profile) if shared else []
        if shared:
            workspace, workspace_warning = resolve_instance_workspace(
                profile_path, profile, episode_code, args.instance_root,
                create=args.execute, series=series)
            if workspace:
                instance = Path(workspace["instance_root"]).resolve()
                instance_mode = workspace["storage_mode"]
                env.update(workspace["shell_exports"])
                env["LDP_EPISODE"] = episode_code
            if args.publish_reference and not spec.get("write"):
                raise LockError("--publish-reference requires an executing write-capable stage")
        elif args.instance_root or args.publish_reference:
            raise LockError("--instance-root/--publish-reference require shared.enabled=true in the selected profile")
        working_directory = safe_working_directory(
            profile, shared, args.working_directory)
        if args.stage == "stack-render":
            if args.script_arg:
                raise LockError("stack-render takes no arbitrary Orc arguments; review/use the manifest project")
            if instance is None and shared:
                action = [str((profile.get("tools") or {}).get("orc") or "<tools.orc>"),
                          "<existing manifest stack_project>", "--process"]
                stack_project = stack_master = None
            else:
                action, stack_project, stack_master = stack_render_inputs(
                    profile_path, episode_code, series, instance, env)
        else:
            action = make_action(args.stage, episode_code, profile, args.script_arg,
                                 args.report_out, series)
        doctor = None
        if requires_doctor(args.stage):
            doctor = [py(profile), str(SCRIPTS / "doctor.py"), "--profile", str(profile_path), "--json"]
            if args.stage != "init-episode":
                doctor += ["--episode", episode_code]
            if instance:
                doctor += ["--instance-root", str(instance)]
        gates = ([validate_command(episode_code, profile, spec["gate"])] if spec.get("gate") else [])
        if args.stage == "remux":
            gates.append(validate_command(episode_code, profile, "encode"))
            manifest_dir = Path(env["LDP_MANIFEST_DIR"]) if env.get("LDP_MANIFEST_DIR") else ROOT / "episodes"
            if not env.get("LDP_MANIFEST_DIR") and series != "DS9":
                manifest_dir = manifest_dir / series
            manifest = manifest_dir / f"{episode_code}.json"
            if manifest.is_file():
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
                tracks = manifest_data.get("mux_tracks") or []
            else:
                tracks = []
            if "51" in tracks or "srt" in tracks:
                gates.append(validate_command(episode_code, profile, "retime"))
        if not args.execute:
            print(f"PLAN V{(ROOT / 'WORKFLOW_VERSION').read_text(encoding='ascii').strip()} stage={args.stage} episode={identity}")
            print("read-only profile: " + str(profile_path))
            if shared:
                if instance:
                    print(f"workspace: {instance} ({instance_mode})")
                else:
                    print("workspace: will reuse/create under README-folder devices/; if unavailable, continue in isolated device-local roots")
                if workspace_warning:
                    print("workspace warning: " + workspace_warning)
            print("subprocess cwd: " + str(working_directory))
            if args.stage == "stack-render":
                print(f"space gate: --stage stack-render --planned-gib {args.planned_gib:g} "
                      f"--root {args.space_root}")
                print(f"watchdog: no Orc log progress for {args.orc_stall_timeout_seconds:g}s "
                      "stops only this stage process tree")
            if args.stage == "download":
                print("readiness: full doctor deferred; downloader-specific profile/curl, Archive integrity, and space gates still run")
            if requires_doctor(args.stage):
                assert doctor is not None
                if shared and not instance:
                    print("doctor gate: runs with the workspace auto-resolved at execution time")
                else:
                    print("doctor gate: " + display(doctor))
            for gate in gates:
                print("manifest gate: " + display(gate))
            print("action: " + display(action))
            print("No commands were run. Add --execute after reviewing this plan.")
            return 0

        logs_raw = (profile.get("roots") or {}).get("logs")
        logs = (instance / "logs") if instance else (
            Path(os.path.expandvars(os.path.expanduser(str(logs_raw)))) / series
            if logs_raw and series != "DS9" else
            Path(os.path.expandvars(os.path.expanduser(str(logs_raw)))) if logs_raw else None)
        if series != "DS9" and logs is not None and not instance and logs.parent.is_dir():
            logs.mkdir(exist_ok=True)
        if logs is None or not logs.is_dir():
            raise LockError(f"roots.logs must exist before executing a stage: {logs_raw!r}")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        log_label = episode_code if series == "DS9" else f"{series}-{episode_code}"
        log_path = logs / f"stage-{log_label}-{args.stage}-{stamp}.log"
        report_path = logs / f"stage-{log_label}-{args.stage}-{stamp}.json"
        orc_log_path = logs / f"orc-render-{log_label}-{stamp}.log" if args.stage == "stack-render" else None
        if args.stage == "stack-render":
            if not stack_project or not stack_master:
                raise LockError("stack-render needs an existing episode workspace and manifest")
            if not stack_project.is_file():
                raise LockError(f"manifest Orc project is missing: {stack_project}")
            if stack_master.is_symlink() or stack_master.exists():
                raise LockError(f"refusing to overwrite existing stack master: {stack_master}; inspect it first")
            if orc_log_path and (orc_log_path.is_symlink() or orc_log_path.exists()):
                raise LockError(f"refusing to overwrite Orc log: {orc_log_path}")
            action = [*action, "--log-file", str(orc_log_path), "--log-out", "file"]
        start = datetime.now(timezone.utc).isoformat()
        with log_path.open("x", encoding="utf-8") as log:
            log.write(f"workflow_version={(ROOT / 'WORKFLOW_VERSION').read_text(encoding='ascii').strip()}\n")
            profile_label = "<device-local-profile>" if shared else str(profile_path)
            log.write(f"episode={episode_code} series={series} identity={identity} stage={args.stage} profile={profile_label}\n\n")
            if args.stage == "download":
                log.write("NOTE: full doctor deferred; downloader enforces profile/curl, Archive integrity, and destination-space gates.\n\n")
            if shared and instance:
                safe_instance = redact_shared_text(str(instance), redactions)
                log.write(f"workspace_mode={instance_mode} workspace={safe_instance}\n")
                if workspace_warning:
                    log.write(f"workspace_warning={redact_shared_text(workspace_warning, redactions)}\n")
        gate_results = ([{"kind": "workspace", "mode": instance_mode,
                         "instance_root": str(instance), "warning": workspace_warning}]
                        if shared and instance else [])
        stage_clock_started = time.perf_counter()
        gate_elapsed = 0.0
        action_elapsed = 0.0
        post_action_elapsed = 0.0

        def timing_snapshot() -> dict:
            total = max(0.0, time.perf_counter() - stage_clock_started)
            return {
                "active_elapsed_seconds": round(total, 3),
                "gate_elapsed_seconds": round(gate_elapsed, 3),
                "action_elapsed_seconds": round(action_elapsed, 3),
                "post_action_elapsed_seconds": round(post_action_elapsed, 3),
                "human_wait_seconds": 0.0,
                "wait_policy": "Time between stage invocations is excluded; pause manual stage clocks while waiting for human gate input.",
            }

        def log_timing(log_status: str) -> None:
            timing = timing_snapshot()
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write("TIMING " + json.dumps({"status": log_status, **timing}) + "\n")

        if workspace_warning:
            print("WARN: " + workspace_warning, file=sys.stderr)
        if requires_doctor(args.stage):
            assert doctor is not None
            gate_start = time.perf_counter()
            check = run_captured(doctor, env, log_path, redactions, cwd=working_directory)
            gate_elapsed += time.perf_counter() - gate_start
            doctor_row = {"kind": "doctor", "command": doctor, "exit_code": check.returncode}
            gate_results.append(doctor_row)
            if check.returncode:
                log_timing("BLOCKED_DOCTOR")
                write_stage_report(report_path, profile_path, episode_code, args.stage,
                                   action, start, "BLOCKED_DOCTOR", check.returncode,
                                   log_path, gate_results, timing_snapshot(), series, redactions)
                return check.returncode or 1
        for gate in gates:
            gate_start = time.perf_counter()
            gate_proc = run_captured(gate, env, log_path, redactions, cwd=working_directory)
            gate_elapsed += time.perf_counter() - gate_start
            gate_results.append({"kind": "manifest", "command": gate,
                                 "exit_code": gate_proc.returncode})
            if gate_proc.returncode:
                log_timing("BLOCKED_MANIFEST_GATE")
                write_stage_report(report_path, profile_path, episode_code, args.stage,
                                   action, start, "BLOCKED_MANIFEST_GATE", gate_proc.returncode,
                                   log_path, gate_results, timing_snapshot(), series, redactions)
                return gate_proc.returncode or 1
        if args.stage == "stack-render":
            budget_command = [py(profile), str(SCRIPTS / "check_space_budget.py"),
                              "--stage", "stack-render", "--planned-gib",
                              f"{args.planned_gib:g}", "--root", args.space_root,
                              "--profile", str(profile_path)]
            gate_start = time.perf_counter()
            budget_result = run_captured(budget_command, env, log_path, redactions,
                                         cwd=working_directory)
            gate_elapsed += time.perf_counter() - gate_start
            gate_results.append({"kind": "space-budget", "command": budget_command,
                                 "exit_code": budget_result.returncode})
            if budget_result.returncode:
                log_timing("BLOCKED_SPACE_BUDGET")
                write_stage_report(report_path, profile_path, episode_code, args.stage,
                                   action, start, "BLOCKED_SPACE_BUDGET",
                                   budget_result.returncode, log_path, gate_results,
                                   timing_snapshot(), series, redactions)
                return budget_result.returncode or 1
        print("COMMAND: " + display(action), flush=True)
        action_start = time.perf_counter()
        rc = run_logged(action, env, log_path, redactions, cwd=working_directory,
                        progress_path=orc_log_path,
                        stall_seconds=(args.orc_stall_timeout_seconds
                                       if args.stage == "stack-render" else None))
        action_elapsed += time.perf_counter() - action_start
        if rc == 0 and args.publish_reference:
            for reference in args.publish_reference:
                publish = [py(profile), str(SCRIPTS / "shared_workspace.py"),
                           "publish-reference", "--profile", str(profile_path),
                           "--instance-root", str(instance), "--source", reference]
                publish_start = time.perf_counter()
                published = run_captured(publish, env, log_path, redactions,
                                         cwd=working_directory)
                post_action_elapsed += time.perf_counter() - publish_start
                gate_results.append({"kind": "publish-reference", "command": publish,
                                     "exit_code": published.returncode})
                if published.returncode:
                    rc = published.returncode
                    break
        log_timing("PASS" if rc == 0 else "FAIL")
        write_stage_report(report_path, profile_path, episode_code, args.stage,
                           action, start, "PASS" if rc == 0 else "FAIL", rc,
                           log_path, gate_results, timing_snapshot(), series, redactions)
        return 0 if rc == 0 else rc
    except (OSError, json.JSONDecodeError, LockError, PipelineError) as exc:
        print(f"STAGE RUNNER ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
