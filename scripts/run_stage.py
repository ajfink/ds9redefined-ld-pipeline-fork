#!/usr/bin/env python3
"""Run one allowlisted pipeline stage with explicit gates and durable logs.

Planning is the default; ``--execute`` is required to launch a workflow command.
There is no arbitrary shell mode. Write-capable stages first run the unified
doctor and declared manifest validators; each invoked helper retains its own
refuse-overwrite and quality gates.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from dependency_lock import LockError, profile_platform, resolve_interpreter  # noqa: E402

STAGES = {
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
    "build-map": {"kind": "map", "gate": "stack", "write": True},
    "audio-diagnostic": {"kind": "python", "script": "audio_timing_diagnostic.py", "gate": "stack", "write": True},
    "audio-promote-no-dvd": {"kind": "no-dvd", "gate": "stack", "write": True},
    "pad-audit": {"kind": "python", "script": "pad_audit.py", "gate": "cadence", "write": True},
    "fullres-check": {"kind": "python", "script": "fullres_check.py", "gate": "padfix", "write": True},
    "rife-replacements": {"kind": "python", "script": "rife_padframes.py", "gate": "padfix", "write": True},
    "build-padfix": {"kind": "padfix", "gate": "cadence", "write": True},
    "encode-validate": {"kind": "encode", "pass": "validate", "gate": "encode", "write": False},
    "encode-final": {"kind": "encode", "pass": "final", "gate": "encode", "write": True},
    "remux": {"kind": "remux", "gate": "audio", "write": True},
    "qc": {"kind": "python", "script": "qc_report.py", "write": True},
}


def load_profile(path: str | None) -> tuple[Path, dict]:
    chosen = path or os.environ.get("LDP_PROFILE") or str(ROOT / "machine.json")
    p = Path(os.path.expandvars(os.path.expanduser(chosen))).resolve()
    if not p.is_file():
        raise LockError(f"profile not found: {p}; copy the matching machine example to machine.json")
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read profile {p}: {exc}") from exc


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


def make_action(stage: str, episode: str, profile: dict, extra: list[str], report_out: str | None) -> list[str]:
    spec = STAGES[stage]
    os_name = profile_platform(profile)
    kind = spec["kind"]
    if kind == "validate":
        return [py(profile), str(SCRIPTS / "validate_episode.py"), "--episode", episode,
                "--stage", spec["manifest_stage"]]
    if kind == "python":
        return python_command(profile, spec["script"], episode, extra)
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


def display(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def run_captured(command: list[str], env: dict[str, str], log_path: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("COMMAND: " + display(command) + "\n")
        log.write(proc.stdout or "")
        if proc.stderr:
            log.write("\n[stderr]\n" + proc.stderr)
        log.write(f"\nEXIT CODE: {proc.returncode}\n\n")
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc


def run_logged(command: list[str], env: dict[str, str], log_path: Path) -> int:
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("COMMAND: " + display(command) + "\n\n")
        log.flush()
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, env=env)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        proc.stdout.close()
        rc = proc.wait()
        log.write(f"\nEXIT CODE: {rc}\n")
        return rc


def write_stage_report(path: Path, profile_path: Path, episode: str, stage: str,
                       command: list[str], started: str, status: str,
                       exit_code: int | None, log_path: Path,
                       gates: list[dict]) -> None:
    report = {"schema": "ld-pipeline-stage-run-v1",
              "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
              "episode": episode, "stage": stage, "profile": str(profile_path),
              "command": command, "started_utc": started,
              "finished_utc": datetime.now(timezone.utc).isoformat(),
              "exit_code": exit_code, "status": status, "gates": gates,
              "log": str(log_path)}
    report_path = path
    with report_path.open("x", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"stage report: {report_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", help="episode code (not needed with --list)")
    ap.add_argument("--stage", choices=sorted(STAGES))
    ap.add_argument("--profile", help="machine profile; defaults to LDP_PROFILE or bundle machine.json")
    ap.add_argument("--execute", action="store_true", help="execute; default only prints the bounded plan")
    ap.add_argument("--report-out", help="required new report path for audio-promote-no-dvd")
    ap.add_argument("--script-arg", action="append", default=[],
                    help="one argument passed to the allowlisted stage script; repeat as needed")
    ap.add_argument("--list", action="store_true", help="list allowlisted stages")
    args = ap.parse_args()
    if args.list:
        for name, spec in STAGES.items():
            print(f"{name:24} write={str(spec['write']).lower()} gate={spec.get('gate', 'none')}")
        return 0
    if not args.stage or not args.episode:
        ap.error("--stage and --episode are required unless --list is used")
    try:
        profile_path, profile = load_profile(args.profile)
        os_name = profile_platform(profile)
        if os_name not in ("windows", "macos", "linux"):
            raise LockError(f"unsupported profile os: {profile.get('os')!r}")
        if os_name == "linux" and STAGES[args.stage]["write"]:
            raise LockError("Linux production stages are blocked by design; only dependency and doctor checks are available until Linux end-to-end validation")
        action = make_action(args.stage, args.episode, profile, args.script_arg, args.report_out)
        spec = STAGES[args.stage]
        env = os.environ.copy()
        env["LDP_PROFILE"] = str(profile_path)
        doctor = [py(profile), str(SCRIPTS / "doctor.py"), "--profile", str(profile_path), "--episode", args.episode, "--json"]
        gates = ([validate_command(args.episode, profile, spec["gate"])] if spec.get("gate") else [])
        if args.stage == "remux":
            gates.append(validate_command(args.episode, profile, "encode"))
            manifest = ROOT / "episodes" / f"{args.episode}.json"
            if manifest.is_file():
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
                tracks = manifest_data.get("mux_tracks") or []
            else:
                tracks = []
            if "51" in tracks or "srt" in tracks:
                gates.append(validate_command(args.episode, profile, "retime"))
        if not args.execute:
            print(f"PLAN V{(ROOT / 'WORKFLOW_VERSION').read_text(encoding='ascii').strip()} stage={args.stage} episode={args.episode}")
            print("read-only profile: " + str(profile_path))
            if spec["write"]:
                print("doctor gate: " + display(doctor))
            for gate in gates:
                print("manifest gate: " + display(gate))
            print("action: " + display(action))
            print("No commands were run. Add --execute after reviewing this plan.")
            return 0

        logs_raw = (profile.get("roots") or {}).get("logs")
        logs = Path(os.path.expandvars(os.path.expanduser(str(logs_raw)))) if logs_raw else None
        if logs is None or not logs.is_dir():
            raise LockError(f"roots.logs must exist before executing a stage: {logs_raw!r}")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        log_path = logs / f"stage-{args.episode}-{args.stage}-{stamp}.log"
        report_path = logs / f"stage-{args.episode}-{args.stage}-{stamp}.json"
        start = datetime.now(timezone.utc).isoformat()
        with log_path.open("x", encoding="utf-8") as log:
            log.write(f"workflow_version={(ROOT / 'WORKFLOW_VERSION').read_text(encoding='ascii').strip()}\n")
            log.write(f"episode={args.episode} stage={args.stage} profile={profile_path}\n\n")
        gate_results = []
        if spec["write"]:
            check = run_captured(doctor, env, log_path)
            doctor_row = {"kind": "doctor", "command": doctor, "exit_code": check.returncode}
            gate_results.append(doctor_row)
            if check.returncode:
                write_stage_report(report_path, profile_path, args.episode, args.stage,
                                   action, start, "BLOCKED_DOCTOR", check.returncode,
                                   log_path, gate_results)
                return check.returncode or 1
        for gate in gates:
            gate_proc = run_captured(gate, env, log_path)
            gate_results.append({"kind": "manifest", "command": gate,
                                 "exit_code": gate_proc.returncode})
            if gate_proc.returncode:
                write_stage_report(report_path, profile_path, args.episode, args.stage,
                                   action, start, "BLOCKED_MANIFEST_GATE", gate_proc.returncode,
                                   log_path, gate_results)
                return gate_proc.returncode or 1
        print("COMMAND: " + display(action), flush=True)
        rc = run_logged(action, env, log_path)
        write_stage_report(report_path, profile_path, args.episode, args.stage,
                           action, start, "PASS" if rc == 0 else "FAIL", rc,
                           log_path, gate_results)
        return 0 if rc == 0 else rc
    except (OSError, json.JSONDecodeError, LockError) as exc:
        print(f"STAGE RUNNER ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
