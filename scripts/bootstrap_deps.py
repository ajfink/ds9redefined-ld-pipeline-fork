#!/usr/bin/env python3
"""Check or explicitly install the V7 direct Python-package lock.

Read-only by default. ``--install`` applies only the selected target's pinned
pip requirements, with dependency resolution disabled; it never installs native
tools, changes Python, edits machine.json, or updates plugins/models.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dependency_lock import (LockError, compare_lock, profile_platform,
                             resolve_interpreter, target_for)


def load_profile(path: str | None) -> tuple[Path | None, dict]:
    chosen = path or os.environ.get("LDP_PROFILE")
    if chosen:
        p = Path(os.path.expandvars(os.path.expanduser(chosen))).resolve()
    else:
        p = Path(__file__).resolve().parent.parent / "machine.json"
        if not p.is_file():
            return None, {}
    if not p.is_file():
        raise LockError(f"profile not found: {p}")
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read profile {p}: {exc}") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="machine.json; defaults to LDP_PROFILE or bundle machine.json")
    ap.add_argument("--install", action="store_true",
                    help="explicitly install/update only the direct packages pinned for this host")
    ap.add_argument("--print", dest="print_command", action="store_true",
                    help="print the exact installation command without running it")
    ap.add_argument("--json", action="store_true", help="emit machine-readable result")
    args = ap.parse_args()
    try:
        target = target_for()
        profile_path, profile = load_profile(args.profile)
        if profile:
            expected_os = profile_platform(profile)
            host_os = target.split("-", 1)[0]
            if expected_os and expected_os != host_os:
                raise LockError(f"profile os={profile.get('os')!r} does not match host target {target}")
        interpreter = resolve_interpreter(profile) if profile else sys.executable
        from dependency_lock import DEPENDENCIES, read_lock
        lock, _, _ = read_lock(target)
        command = [interpreter, "-m", "pip", "install", "--no-input", "--upgrade",
                   "--no-deps", "--requirement", str(lock)]
        result = compare_lock(target, interpreter)
        result["profile"] = str(profile_path) if profile_path else None
        result["install_command"] = command
        result["install_requested"] = args.install
        result["read_only"] = not args.install
        if args.print_command:
            result["command_text"] = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        python_mismatch = any(item.get("package") == "Python" for item in result.get("mismatches", []))
        if args.install and python_mismatch:
            result["install_skipped"] = "selected interpreter Python version does not match the target lock"
        elif args.install and result.get("ok"):
            result["install_skipped"] = "all locked direct packages are already satisfied"
        elif args.install:
            proc = subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            result["install_exit_code"] = proc.returncode
            result["install_stdout"] = proc.stdout
            result["install_stderr"] = proc.stderr
            if proc.returncode:
                result["ok"] = False
                result["error"] = "pip install failed; inspect output and rerun the doctor"
            else:
                result = compare_lock(target, interpreter) | {
                    "profile": str(profile_path) if profile_path else None,
                    "install_command": command,
                    "install_requested": True,
                    "read_only": False,
                    "install_exit_code": 0,
                    "install_stdout": proc.stdout,
                    "install_stderr": proc.stderr,
                }
                result["ok"] = result["ok"]
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"target: {target}\npython: {result['python']} ({result['interpreter']})")
            print(f"lock: {result['lock']}")
            for name, version in result["expected_packages"].items():
                actual = result["packages"].get(name)
                if name == "opencv-python":
                    actual = actual or result["packages"].get("opencv-python-headless")
                if name == "opencv-python-headless":
                    actual = actual or result["packages"].get("opencv-python")
                print(f"  {'PASS' if actual == version else 'FAIL'} {name}: expected={version}, installed={actual}")
            if args.print_command:
                print("install command (not run): " + result["command_text"])
            elif args.install and result.get("install_exit_code") == 0:
                print("pip install exit: " + str(result.get("install_exit_code")))
            elif args.install and result.get("install_skipped"):
                print("install skipped: " + result["install_skipped"])
            else:
                print("read-only check; run with --install only after the tool-update gate and user approval")
            print("LOCK CHECK PASS" if result.get("ok") else "LOCK CHECK FAIL")
        return 0 if result.get("ok") else 1
    except LockError as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
