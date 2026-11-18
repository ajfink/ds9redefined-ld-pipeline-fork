#!/usr/bin/env python3
"""Check newest versions and install only explicitly selected managed dependencies.

The checker is read-only. Python packages are checked against PyPI; supported
native tools are checked only when the configured executable is managed by the
platform package manager. Updates are restricted to lock-listed Python packages
and the small native-package allowlist below.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap_deps import load_profile  # noqa: E402
from dependency_lock import (LockError, inspect_interpreter,
                             read_lock, resolve_interpreter, target_for,
                             version_relation)  # noqa: E402


NATIVE_TOOLS = {
    "windows": {
        "python": {"id_prefix": "Python.Python", "version_args": ["--version"],
                   "version_pattern": r"(?im)^Python\s+([0-9]+(?:\.[0-9]+)+)"},
        "ffmpeg": {"id": "Gyan.FFmpeg.Shared", "version_args": ["-version"],
                   "version_pattern": r"(?im)^ffmpeg version\s+([^\s]+)"},
        "curl": {"id": "cURL.cURL", "version_args": ["--version"],
                 "version_pattern": r"(?im)^curl\s+([0-9]+(?:\.[0-9]+)+)"},
        "sevenzip": {"id": "7zip.7zip", "version_args": ["i"],
                     "version_pattern": r"(?im)^7-Zip\s+([0-9]+(?:\.[0-9]+)+)"},
    },
    "macos": {
        "ffmpeg": {"formula": "ffmpeg", "version_args": ["-version"],
                   "version_pattern": r"(?im)^ffmpeg version\s+([^\s]+)"},
        "curl": {"formula": "curl", "version_args": ["--version"],
                 "version_pattern": r"(?im)^curl\s+([0-9]+(?:\.[0-9]+)+)"},
    },
}


def _run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _stop_process_tree(process: subprocess.Popen) -> None:
    """Stop the selected installer and its descendants after an explicit user request."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=5)
            return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_stream(command: list[str], timeout: int = 3600,
                on_output: Callable[[str], None] | None = None,
                cancel_event: threading.Event | None = None) -> tuple[int, str, bool]:
    """Run an installer while forwarding each output line to the review window."""
    if cancel_event and cancel_event.is_set():
        return 130, "Installer stopped before launch.\n", True
    try:
        process_args = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                        "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
        if os.name == "nt":
            process_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_args["start_new_session"] = True
        process = subprocess.Popen(command, **process_args)
    except OSError as exc:
        return 127, str(exc), False
    assert process.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in process.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, name="dependency-installer-output", daemon=True)
    reader.start()
    output: list[str] = []
    started = time.monotonic()
    cancelled = False
    timed_out = False
    while True:
        if cancel_event and cancel_event.is_set() and process.poll() is None:
            cancelled = True
            _stop_process_tree(process)
            output.append("Installer stopped by user.\n")
            break
        try:
            line = lines.get(timeout=0.25)
        except queue.Empty:
            if time.monotonic() - started > timeout:
                timed_out = True
                _stop_process_tree(process)
                output.append(f"Installer timed out after {timeout} seconds.\n")
                break
            continue
        if line is None:
            break
        output.append(line)
        if on_output:
            try:
                on_output(line.rstrip("\r\n"))
            except Exception:
                # A UI progress callback must never interrupt an active installer.
                pass
    process.stdout.close()
    reader.join(timeout=5)
    try:
        code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_process_tree(process)
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = process.poll() if process.poll() is not None else 124
    if cancelled:
        return 130, "".join(output), True
    if timed_out and code == 0:
        code = 124
    return code, "".join(output), False


def _pypi_latest(package: str, python_version: str | None = None,
                 timeout: int = 15) -> str:
    request = Request(f"https://pypi.org/pypi/{package}/json",
                      headers={"User-Agent": "FromStunToKill-dependency-check/1"})
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    info = data.get("info") or {}
    version = str(info.get("version") or "").strip()
    if not version:
        raise ValueError("PyPI returned no current release version")
    requires_python = str(info.get("requires_python") or "").strip()
    if python_version and requires_python:
        try:
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version
        except ImportError:
            try:
                from pip._vendor.packaging.specifiers import SpecifierSet
                from pip._vendor.packaging.version import Version
            except ImportError:
                return version
        if Version(python_version) not in SpecifierSet(requires_python):
            candidates = []
            for candidate, files in (data.get("releases") or {}).items():
                if not files or not any(not file.get("yanked", False) for file in files):
                    continue
                try:
                    candidate_version = Version(candidate)
                except Exception:
                    continue
                if candidate_version.is_prerelease:
                    continue
                compatible = False
                for file in files:
                    requirement = str(file.get("requires_python") or "").strip()
                    if not requirement or Version(python_version) in SpecifierSet(requirement):
                        compatible = True
                        break
                if compatible:
                    candidates.append(candidate_version)
            if not candidates:
                raise ValueError(f"no stable release supports Python {python_version}")
            version = str(max(candidates))
    return version


def _resolved_tool(profile: dict, key: str) -> Path | None:
    raw = (profile.get("tools") or {}).get(key)
    if not raw:
        return None
    value = os.path.expandvars(os.path.expanduser(str(raw)))
    candidate = Path(value)
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(value)
    return Path(found).resolve() if found else None


def _version_from_tool(path: Path, spec: dict) -> str | None:
    code, output = _run([str(path), *spec["version_args"]], timeout=20)
    if code != 0:
        return None
    match = re.search(spec["version_pattern"], output)
    return match.group(1) if match else None


def _native_version_relation(installed: str | None, latest: str | None) -> str:
    """Compare the numeric release portion of native versions with build suffixes."""
    if not installed or not latest:
        return "unverified"
    local_match = re.search(r"\d+(?:\.\d+){1,3}", installed)
    latest_match = re.search(r"\d+(?:\.\d+){1,3}", latest)
    if not local_match or not latest_match:
        return "unverified"
    return version_relation(local_match.group(0), latest_match.group(0))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _pypi_rows(packages: list[tuple[str, str]], actual: dict) -> list[dict]:
    rows = []
    aliases = {"opencv-python": ("opencv-python", "opencv-python-headless"),
               "opencv-python-headless": ("opencv-python-headless", "opencv-python")}
    for locked_name, _locked_version in packages:
        installed_name = next((name for name in aliases.get(locked_name, (locked_name,))
                               if actual.get("packages", {}).get(name)), locked_name)
        installed = actual.get("packages", {}).get(installed_name)
        row = {"id": f"pypi:{installed_name.casefold()}", "name": installed_name,
               "source": "PyPI", "manager": "pip", "installed": installed,
               "latest": None, "available": False, "selectable": False,
               "detail": ""}
        if not installed:
            row["detail"] = "Not installed; the readiness check will report this separately."
            rows.append(row)
            continue
        try:
            latest = _pypi_latest(installed_name, actual.get("python"))
            row["latest"] = latest
            relation = version_relation(installed, latest)
            row["available"] = relation == "older"
            row["selectable"] = row["available"]
            row["detail"] = ("Update available" if row["available"] else
                             "Installed version is current" if relation == "exact" else
                             "Installed version is newer than the listed stable release"
                             if relation == "newer" else
                             f"Could not compare versions ({relation})")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError,
                json.JSONDecodeError) as exc:
            row["detail"] = f"Version check unavailable: {exc}"
        rows.append(row)
    return rows


def _windows_native_rows(profile: dict) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    notes: list[str] = []
    winget = shutil.which("winget")
    if not winget:
        return rows, ["WinGet is unavailable; native-tool update checks were skipped."]
    local_appdata = os.environ.get("LOCALAPPDATA")
    package_root = (Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
                    if local_appdata else None)
    if package_root is None:
        return rows, ["WinGet package root could not be resolved; native-tool update checks were skipped."]
    for key, spec in NATIVE_TOOLS["windows"].items():
        path = (_resolved_tool(profile, key) if key != "python" else
                Path(resolve_interpreter(profile)).resolve())
        if path is None or not _inside(path, package_root):
            continue
        local = _version_from_tool(path, spec)
        if key == "python":
            match = re.match(r"^(\d+)\.(\d+)", local or "")
            if not match:
                continue
            package_id = f"{spec['id_prefix']}.{match.group(1)}.{match.group(2)}"
        else:
            package_id = spec["id"]
        listed_rc, listed = _run([winget, "list", "--id", package_id, "--exact",
                                  "--source", "winget", "--accept-source-agreements"])
        if listed_rc != 0 or package_id.casefold() not in listed.casefold():
            continue
        show_rc, shown = _run([winget, "show", "--id", package_id, "--exact",
                               "--source", "winget", "--accept-source-agreements"])
        latest_match = re.search(r"(?im)^\s*Version\s*:\s*([^\s]+)", shown)
        latest = latest_match.group(1) if show_rc == 0 and latest_match else None
        relation = _native_version_relation(local, latest)
        available = relation == "older"
        rows.append({"id": f"winget:{package_id}", "name": key, "source": "WinGet",
                     "manager": "winget", "package_id": package_id,
                     "installed": local, "latest": latest, "available": available,
                     "selectable": available,
                     "detail": ("Update available" if available else
                               "Could not compare local and catalog versions" if relation == "unverified" else
                               "Installed version is current" if relation == "exact" else
                               "Installed version is newer than the catalog release")})
    notes.append("Only configured executables inside the WinGet package root are matched to WinGet updates.")
    return rows, notes


def _macos_native_rows(profile: dict) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    notes: list[str] = []
    brew = shutil.which("brew")
    if not brew:
        return rows, ["Homebrew is unavailable; native-tool update checks were skipped."]
    for key, spec in NATIVE_TOOLS["macos"].items():
        path = _resolved_tool(profile, key)
        if path is None:
            continue
        prefix_rc, prefix_text = _run([brew, "--prefix", spec["formula"]])
        prefix = Path(prefix_text.strip()) if prefix_rc == 0 and prefix_text.strip() else None
        if prefix is None or not _inside(path, prefix):
            continue
        rc, output = _run([brew, "info", "--json=v2", "--formula", spec["formula"]])
        local = _version_from_tool(path, spec)
        latest = None
        if rc == 0:
            try:
                data = json.loads(output)
                formulae = data.get("formulae") or []
                item = next((row for row in formulae if row.get("name") == spec["formula"]), None)
                if item:
                    latest = str((item.get("versions") or {}).get("stable") or "") or None
            except json.JSONDecodeError:
                pass
        relation = _native_version_relation(local, latest)
        available = relation == "older"
        rows.append({"id": f"brew:{spec['formula']}", "name": key, "source": "Homebrew",
                     "manager": "brew", "formula": spec["formula"],
                     "installed": local, "latest": latest, "available": available,
                     "selectable": available,
                     "detail": ("Update available" if available else
                               "Could not compare local and catalog versions" if not local or not latest else
                               "Installed version is current" if relation == "exact" else
                               "Installed version is newer than the catalog release")})
    notes.append("Only configured executables under their matching Homebrew formula prefix are matched.")
    return rows, notes


def check_updates(profile_path: str | Path) -> dict:
    """Inspect installed lock-listed packages and supported package-managed tools."""
    target = target_for()
    profile_file, profile = load_profile(str(profile_path))
    expected_os = str(profile.get("os") or "").casefold()
    host_os = target.split("-", 1)[0]
    if expected_os in ("win32",):
        expected_os = "windows"
    if expected_os in ("darwin",):
        expected_os = "macos"
    if expected_os and expected_os != host_os:
        raise LockError(f"profile platform {expected_os!r} does not match this host ({host_os})")
    interpreter = resolve_interpreter(profile)
    _lock, _runtime_reference, packages = read_lock(target)
    actual = inspect_interpreter(interpreter)
    rows = _pypi_rows(packages, actual)
    notes: list[str] = [
        "Python runtime and unmanaged specialist tools are checked for presence/capability by doctor, "
        "but are not automatically replaced by this updater.",
    ]
    if host_os == "windows":
        native, details = _windows_native_rows(profile)
    elif host_os == "macos":
        native, details = _macos_native_rows(profile)
    else:
        native, details = [], ["No native package-manager updates are currently configured for this platform."]
    rows.extend(native)
    notes.extend(details)
    return {"ok": True, "profile": str(profile_file), "platform": target,
            "interpreter": interpreter, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "rows": rows, "notes": notes}


def install_selected(profile_path: str | Path, selected_ids: list[str],
                     progress: Callable[[str | None, str, str], None] | None = None,
                     cancel_event: threading.Event | None = None) -> dict:
    """Recheck selected IDs, then install only currently available allowlisted updates."""
    def notify(item_id: str | None, event: str, text: str) -> None:
        if progress:
            try:
                progress(item_id, event, text)
            except Exception:
                pass

    selected = set(selected_ids)
    if not selected:
        raise LockError("select at least one available update")
    notify(None, "output", "Rechecking selected updates before installation...")
    report = check_updates(profile_path)
    by_id = {row["id"]: row for row in report["rows"]}
    unknown = selected - set(by_id)
    if unknown:
        raise LockError("unknown update selection(s): " + ", ".join(sorted(unknown)))
    results = []
    for item_id in selected_ids:
        row = by_id[item_id]
        if cancel_event and cancel_event.is_set():
            notify(item_id, "status", "Stopped")
            notify(item_id, "output", f"> {row['name']}: stopped before installation started.")
            results.append({"id": item_id, "name": row["name"], "ok": False,
                            "cancelled": True, "detail": "Stopped before installation started."})
            continue
        if not row.get("available") or not row.get("selectable"):
            notify(item_id, "status", "Failed")
            notify(item_id, "output", f"{row['name']}: update is no longer available; run the check again.")
            results.append({"id": item_id, "name": row["name"], "ok": False,
                            "detail": "No longer available; run the update check again."})
            continue
        if row["manager"] == "pip":
            command = [report["interpreter"], "-m", "pip", "install", "--upgrade",
                       "--no-input", row["name"]]
        elif row["manager"] == "winget":
            winget = shutil.which("winget")
            if not winget:
                results.append({"id": item_id, "name": row["name"], "ok": False,
                                "detail": "WinGet is no longer available."})
                continue
            command = [winget, "upgrade", "--id", row["package_id"], "--exact",
                       "--accept-source-agreements", "--accept-package-agreements",
                       "--disable-interactivity"]
        elif row["manager"] == "brew":
            brew = shutil.which("brew")
            if not brew:
                results.append({"id": item_id, "name": row["name"], "ok": False,
                                "detail": "Homebrew is no longer available."})
                continue
            command = [brew, "upgrade", row["formula"]]
        else:
            notify(item_id, "status", "Failed")
            results.append({"id": item_id, "name": row["name"], "ok": False,
                            "detail": "Unsupported update manager."})
            continue
        notify(item_id, "status", "Updating...")
        notify(item_id, "output", f"> {row['name']}: updating via {row['manager']}...")
        code, output, was_cancelled = _run_stream(
            command, timeout=3600,
            on_output=lambda line, selected_id=item_id, name=row["name"]:
                notify(selected_id, "output", f"[{name}] {line}"),
            cancel_event=cancel_event)
        if was_cancelled:
            notify(item_id, "status", "Stopped")
            notify(item_id, "output", f"> {row['name']}: stopped by user.")
            results.append({"id": item_id, "name": row["name"], "ok": False,
                            "cancelled": True, "exit_code": code,
                            "detail": "Stopped by user."})
            continue
        success = code == 0
        notify(item_id, "status", "Updated!" if success else "Failed")
        notify(item_id, "output", f"> {row['name']}: {'Updated!' if success else f'Failed (exit {code})'}")
        results.append({"id": item_id, "name": row["name"], "ok": code == 0,
                        "exit_code": code, "command": command,
                        "detail": output[-8000:] or ("Update installed." if code == 0 else "No output." )})
    cancelled = any(item.get("cancelled") for item in results)
    return {"ok": not cancelled and all(item["ok"] for item in results),
            "cancelled": cancelled, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="device-local machine profile")
    parser.add_argument("--install", action="append", default=[], metavar="UPDATE_ID",
                        help="install a selected update ID; repeat for multiple selections")
    args = parser.parse_args()
    try:
        result = install_selected(args.profile, args.install) if args.install else check_updates(args.profile)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except (LockError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
