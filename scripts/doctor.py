#!/usr/bin/env python3
"""Unified, read-only V7 machine doctor for Windows, macOS and Linux x86_64.

Windows and macOS delegate their platform-specific functional checks to the
existing preflight scripts. Linux receives common tool/profile/dependency checks
but is deliberately never marked episode-ready: its end-to-end workflow remains
unvalidated. This command installs nothing and writes only when --report-out is
explicitly supplied.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
from dependency_lock import (LockError, compare_lock, profile_platform,
                             redact_shared_text, resolve_interpreter,
                             shared_redactions, target_for)
from ldcommon import shared_instance_root


def profile_path(value: str | None) -> Path:
    chosen = value or os.environ.get("LDP_PROFILE")
    devices = ROOT / "devices"
    if not chosen and devices.is_dir():
        raise LockError("shared release detected; pass --profile pointing to a local machine.json outside the release tree")
    if chosen:
        path = Path(os.path.expandvars(os.path.expanduser(chosen))).resolve()
    else:
        path = ROOT / "machine.json"
    if not path.is_file():
        raise LockError(f"machine profile not found: {path}; copy the matching machine-*.example.json to machine.json")
    if devices.is_dir():
        try:
            path.relative_to(ROOT.resolve())
            raise LockError("machine profile must be outside the shared release tree")
        except ValueError:
            pass
    return path


def invoke(argv: list[str], env: dict[str, str] | None = None) -> dict:
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return {"command": argv, "exit_code": p.returncode,
            "stdout": p.stdout, "stderr": p.stderr}


def parse_windows_preflight(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        match = re.match(r"\s*\[(PASS|WARN|FAIL)\]\s+(.*?)\s{2,}(.*)$", line)
        if match:
            rows.append({"status": match.group(1), "check": match.group(2).strip(),
                         "detail": match.group(3).strip()})
    return rows


def windows_preflight(profile: Path, episode: str | None, env: dict[str, str]) -> dict:
    shell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not shell:
        return {"exit_code": 2, "error": "PowerShell was not found; use preflight.ps1 from a supported shell"}
    argv = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(ROOT / "preflight.ps1"), "-Profile", str(profile)]
    if episode:
        argv += ["-Episode", episode]
    result = invoke(argv, env)
    result["checks"] = parse_windows_preflight(result.get("stdout", "") + "\n" + result.get("stderr", ""))
    result["ok_marker"] = "PREFLIGHT OK." in result.get("stdout", "")
    result["ok"] = result["exit_code"] == 0 and result["ok_marker"]
    return result


def mac_preflight(profile: Path, episode: str | None, interpreter: str, env: dict[str, str]) -> dict:
    argv = [interpreter, str(ROOT / "scripts" / "preflight_mac.py"),
            "--profile", str(profile), "--json"]
    if episode:
        argv += ["--episode", episode]
    result = invoke(argv, env)
    try:
        decoded = json.loads(result.get("stdout", ""))
    except json.JSONDecodeError:
        decoded = None
    result["summary"] = decoded
    result["checks"] = (decoded or {}).get("results", [])
    result["ok"] = bool(decoded and decoded.get("ok") and result["exit_code"] == 0)
    if decoded is None:
        result["error"] = "preflight_mac.py did not return valid JSON"
    return result


def resolve_tool(value: str | None) -> str | None:
    if not value:
        return None
    expanded = os.path.expandvars(os.path.expanduser(value))
    p = Path(expanded)
    if p.is_file():
        return str(p.resolve())
    return shutil.which(expanded)


def linux_preflight(profile: dict, profile_file: Path, interpreter: str,
                    lock_report: dict, episode: str | None) -> dict:
    checks: list[dict] = []

    def add(name: str, status: str, detail: str):
        checks.append({"check": name, "status": status, "detail": detail})

    actual_arch = platform.machine()
    add("host:architecture", "PASS" if actual_arch.lower() in ("x86_64", "amd64") else "FAIL",
        f"{actual_arch}; V7 dependency candidate is x86_64 only")
    add("profile", "PASS" if profile_file.is_file() else "FAIL", str(profile_file))
    add("workflow:linux-support", "WARN",
        "Linux x86_64 dependency/capability checks only; full workflow is not validated or supported")

    roots = profile.get("roots") or {}
    for name in ("work", "probe", "avs", "audio", "subs", "out", "prores", "stack", "logs"):
        raw = roots.get(name)
        path = Path(os.path.expandvars(os.path.expanduser(str(raw)))) if raw else None
        add(f"root:{name}", "PASS" if path and path.is_dir() else "FAIL",
            str(path) if path else "unset")
    work_raw = roots.get("work")
    min_free = float((profile.get("limits") or {}).get("min_free_gb", 80))
    if work_raw:
        work = Path(os.path.expandvars(os.path.expanduser(str(work_raw))))
        if work.is_dir():
            free_gib = shutil.disk_usage(work).free / (1024 ** 3)
            add("space:work", "PASS" if free_gib >= min_free else "FAIL",
                f"{free_gib:.1f} GiB free; configured reserve {min_free:.1f} GiB")

    tools = profile.get("tools") or {}
    resolved = {}
    for key in ("ffmpeg", "ffprobe", "orc", "python"):
        raw = tools.get(key) or ((profile.get("python") or {}).get("interpreter") if key == "python" else None)
        path = resolve_tool(str(raw)) if raw else None
        resolved[key] = path
        required = key in ("ffmpeg", "ffprobe", "orc", "python")
        add(f"tool:{key}", "PASS" if path else ("FAIL" if required else "WARN"),
            path or f"missing/unresolved: {raw!r}")

    ffmpeg = resolved.get("ffmpeg")
    if ffmpeg:
        version = invoke([ffmpeg, "-version"])
        encoders = invoke([ffmpeg, "-hide_banner", "-encoders"])
        demuxers = invoke([ffmpeg, "-hide_banner", "-demuxers"])
        add("ffmpeg:version", "PASS" if version["exit_code"] == 0 else "FAIL",
            version["stdout"].splitlines()[0] if version["stdout"] else version["stderr"][:300])
        add("ffmpeg:libx265", "PASS" if "libx265" in encoders["stdout"] else "FAIL",
            "encoder available" if "libx265" in encoders["stdout"] else "required encoder missing")
        add("ffmpeg:yuv4mpegpipe", "PASS" if "yuv4mpegpipe" in demuxers["stdout"] else "FAIL",
            "demuxer available" if "yuv4mpegpipe" in demuxers["stdout"] else "required demuxer missing")
    orc = resolved.get("orc")
    if orc:
        stages = invoke([orc, "stages", "list", "--core"])
        normalized = re.sub(r"[^a-z0-9]", "", stages["stdout"].casefold())
        required_stages = ("stacker", "source_align", "tbc_source", "EFMSink", "RawEFMSink",
                           "efm_audio_decode", "audio_align")
        missing = [s for s in required_stages if re.sub(r"[^a-z0-9]", "", s.casefold()) not in normalized]
        add("orc:core-stages", "PASS" if stages["exit_code"] == 0 and not missing else "FAIL",
            "required core stage list present" if not missing and stages["exit_code"] == 0 else
            f"exit={stages['exit_code']}; missing/unconfirmed={missing}")

    add("dependency-lock", "PASS" if lock_report.get("ok") else "FAIL",
        f"{lock_report.get('lock')} Python {lock_report.get('python')}; mismatches={lock_report.get('mismatches')}")
    return {"ok": all(c["status"] != "FAIL" for c in checks), "checks": checks,
            "episode": episode, "profile": str(profile_file)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="machine profile; defaults to LDP_PROFILE or bundle machine.json")
    ap.add_argument("--episode", help="also pass this episode to the platform preflight")
    ap.add_argument("--instance-root", help="active per-instance episode directory under shared devices/")
    ap.add_argument("--json", action="store_true", help="emit one JSON report")
    ap.add_argument("--report-out", help="write a new JSON report file; refuses overwrite")
    args = ap.parse_args()

    try:
        profile_file = profile_path(args.profile)
        profile = json.loads(profile_file.read_text(encoding="utf-8"))
        target = target_for()
        target_os = target.split("-", 1)[0]
        profile_os = profile_platform(profile)
        interpreter = resolve_interpreter(profile)
        env = os.environ.copy()
        env["LDP_PROFILE"] = str(profile_file)
        if args.instance_root:
            env["LDP_INSTANCE_ROOT"] = str(Path(args.instance_root).expanduser().resolve())
        shared_report = {"enabled": False}
        shared_config = profile.get("shared") or {}
        if shared_config.get("enabled") is True:
            release = Path(os.path.expandvars(os.path.expanduser(
                str(shared_config.get("release_root") or "")))).resolve()
            devices = release / "devices"
            shared_report = {"enabled": True, "release_root": str(release),
                             "devices_root": str(devices)}
            if release != ROOT.resolve():
                raise LockError(f"shared.release_root must equal the code bundle path: {ROOT}")
            instance_value = env.get("LDP_INSTANCE_ROOT")
            instance = None
            if instance_value:
                instance = shared_instance_root(profile)
                manifest_dir = instance / "manifest"
                expected_env = {"LDP_MANIFEST_DIR": str(manifest_dir),
                                "LDP_INSTANCE_LOGS": str(instance / "logs"),
                                "LDP_CUT_PAD_IMAGES": str(instance / "cut-pad-images")}
                for key, value in expected_env.items():
                    if env.get(key) and Path(env[key]).resolve() != Path(value).resolve():
                        raise LockError(f"{key} does not point at the active instance folder")
                    env[key] = value
                required_instance_dirs = (manifest_dir, instance / "logs", instance / "references",
                                          instance / "cut-pad-images")
                missing_dirs = [str(path) for path in required_instance_dirs if not path.is_dir()]
                if missing_dirs:
                    raise LockError("shared instance is incomplete; run shared_workspace.py ensure: " +
                                    ", ".join(missing_dirs))
            else:
                manifest_dir = None
            write_roots = ("downloads_root", "work", "probe", "avs", "audio", "subs",
                           "out", "prores", "stack", "logs")
            misplaced = []
            for name in write_roots:
                raw = (profile.get("roots") or {}).get(name)
                if not raw:
                    continue
                candidate = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
                try:
                    candidate.relative_to(release)
                    misplaced.append(f"roots.{name}={candidate}")
                except ValueError:
                    pass
            if misplaced:
                raise LockError("write roots must remain outside the shared release tree: " +
                                "; ".join(misplaced))
            shared_report.update({
                "instance_ready": instance is not None,
                "instance_root": str(instance) if instance else None,
                "manifest_dir": str(manifest_dir) if manifest_dir else None,
                "instance_storage_mode": env.get("LDP_INSTANCE_MODE") if instance else None,
                "device_write_scope": (str(devices / "<device-instance>") if devices.is_dir()
                                       else "unavailable; device-local fallback is used"),
                "release_root_acl_warning": (
                    "the current process appears able to write the release root; configure the "
                    "share ACL read-only for the release tree and writable only under this device's devices/<device-instance>/ folder"
                    if os.access(release, os.W_OK) else
                    "release-root write access is not reported for this process"),
                "release_root_writable_for_process": os.access(release, os.W_OK),
                "application_write_scope": "episode manifest, stage logs, references, and cut-PAD images are redirected into this unique instance folder",
            })
            if instance is None:
                shared_report["setup_note"] = "machine-level diagnostic only; run_stage will create the README-folder workspace or fall back to device-local roots without blocking the episode"
            elif env.get("LDP_INSTANCE_MODE") == "local-fallback":
                shared_report["setup_note"] = "README-folder devices/ workspace unavailable; this episode is using isolated device-local manifest/log/reference/cut-PAD folders"
        elif (ROOT / "devices").is_dir():
            raise LockError("this bundle has a shared devices/ area; set shared.enabled=true in a local profile")
        lock_report = compare_lock(target, interpreter)

        if profile_os != target_os:
            platform_report = {"ok": False, "exit_code": 2,
                               "error": f"profile os={profile.get('os')!r} does not match host {target}"}
        elif target_os == "windows":
            platform_report = windows_preflight(profile_file, args.episode, env)
        elif target_os == "macos":
            platform_report = mac_preflight(profile_file, args.episode, interpreter, env)
        else:
            platform_report = linux_preflight(profile, profile_file, interpreter, lock_report, args.episode)

        workflow_status = {
            "windows": "V11 Windows x86_64 reference workflow",
            "macos": "macOS arm64 end-to-end workflow demonstrated on M5 (S01E10); fresh-device setup is not independently certified",
            "linux": "UNTESTED: x86_64 dependency/capability candidate only; do not process a delivery",
        }[target_os]
        shared_ready = (shared_report.get("enabled") is not True or
                        (shared_report.get("instance_ready") is True and
                         shared_report.get("release_root_writable_for_process") is not True))
        ready = bool(platform_report.get("ok") and lock_report.get("ok") and
                     profile_os == target_os and target_os != "linux" and shared_ready)
        report = {
            "schema": "ld-pipeline-doctor-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
            "host": {"system": platform.system(), "release": platform.release(),
                     "architecture": platform.machine(), "target": target},
            "profile": str(profile_file),
            "profile_os": profile_os,
            "workflow_status": workflow_status,
            "dependency_lock": lock_report,
            "shared_workspace": shared_report,
            "platform_preflight": platform_report,
            "ready_for_episode": ready,
            "ok": ready,
            "read_only": True,
        }
        if shared_config.get("enabled") is True:
            report = json.loads(redact_shared_text(json.dumps(report),
                                                   shared_redactions(profile_file, profile)))
        if args.report_out:
            out = Path(args.report_out).expanduser().resolve()
            if out.exists():
                raise LockError(f"refusing to overwrite doctor report: {out}")
            if shared_config.get("enabled") is True:
                if not shared_report.get("instance_root"):
                    raise LockError("shared doctor report-out requires an initialized instance workspace")
                allowed = Path(shared_report["instance_root"]) / "logs"
                try:
                    out.relative_to(allowed.resolve())
                except ValueError as exc:
                    raise LockError(f"shared doctor reports must be written under the active instance logs: {allowed}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("x", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
                f.write("\n")
            report["report_path"] = str(out)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"V{report['workflow_version']} doctor — {target}\nprofile: {profile_file}")
            print(f"workflow: {workflow_status}")
            print(f"Python lock: {'PASS' if lock_report['ok'] else 'FAIL'} ({lock_report['lock']})")
            if shared_report.get("enabled"):
                print("shared workspace: " + str(shared_report.get("instance_root")))
                print("shared root policy: WARN - " + str(shared_report.get("release_root_acl_warning")))
            pf = platform_report
            if pf.get("checks"):
                for row in pf["checks"]:
                    print(f"  [{row.get('status')}] {row.get('check')}: {row.get('detail')}")
            elif pf.get("summary", {}).get("results"):
                for row in pf["summary"]["results"]:
                    print(f"  [{row.get('status')}] {row.get('check')}: {row.get('detail')}")
            elif pf.get("stdout"):
                print(pf["stdout"].rstrip())
            if pf.get("error"):
                print("preflight error: " + pf["error"])
            print("READY FOR EPISODE" if ready else "NOT READY / UNTESTED")
        return 0 if ready else 1
    except (OSError, json.JSONDecodeError, LockError) as exc:
        error = {"schema": "ld-pipeline-doctor-v1", "ok": False, "ready_for_episode": False,
                 "read_only": True, "error": str(exc)}
        if args.json:
            print(json.dumps(error, indent=2))
        else:
            print(f"DOCTOR ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
