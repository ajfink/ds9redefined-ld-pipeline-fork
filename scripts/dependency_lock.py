#!/usr/bin/env python3
"""Parse and inspect the direct Python dependency locks used by V7 tools."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent
DEPENDENCIES = BUNDLE / "dependencies"


class LockError(RuntimeError):
    pass


def version_relation(installed: str | None, target: str) -> str:
    """Compare PEP 440 versions; unsupported/unparseable values fail closed."""
    if installed is None:
        return "missing"
    if installed == target:
        return "exact"
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        try:
            # pip vendors packaging, so use that parser if the selected helper
            # environment does not expose packaging as a top-level module.
            from pip._vendor.packaging.version import InvalidVersion, Version
        except ImportError:
            return "unverified"
    try:
        actual, expected = Version(installed), Version(target)
    except InvalidVersion:
        return "unverified"
    if actual == expected:
        return "exact"
    return "newer" if actual > expected else "older"


def normalize_arch(value: str) -> str:
    value = value.casefold().replace("-", "_")
    if value in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value


def target_for(system: str | None = None, arch: str | None = None) -> str:
    system = (system or platform.system()).casefold()
    arch = normalize_arch(arch or platform.machine())
    if system == "windows" and arch == "x86_64":
        return "windows-x86_64"
    if system == "darwin" and arch == "arm64":
        return "macos-arm64"
    if system == "linux" and arch == "x86_64":
        return "linux-x86_64"
    raise LockError(f"no V7 Python lock for host {system}/{arch}; supported lock targets: "
                    "windows-x86_64, macos-arm64, linux-x86_64")


def profile_platform(profile: dict) -> str:
    value = str(profile.get("os") or "").casefold()
    return {"windows": "windows", "win32": "windows", "darwin": "macos",
            "macos": "macos", "linux": "linux"}.get(value, value)


def resolve_interpreter(profile: dict) -> str:
    value = ((profile.get("python") or {}).get("interpreter") or
             (profile.get("tools") or {}).get("python") or sys.executable)
    value = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(value)
    if path.is_file():
        return str(path.resolve())
    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    raise LockError(f"profile-selected Python interpreter not found: {value}")


def read_lock(target: str) -> tuple[Path, str, list[tuple[str, str]]]:
    lock = DEPENDENCIES / f"requirements-{target}.lock.txt"
    if not lock.is_file():
        raise LockError(f"lock file not found for {target}: {lock}")
    python_version = ""
    packages: list[tuple[str, str]] = []
    for number, raw in enumerate(lock.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            match = re.search(r"\bPython:\s*([0-9]+\.[0-9]+\.[0-9]+)", line, re.IGNORECASE)
            if match:
                python_version = match.group(1)
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        if not match:
            raise LockError(f"unsupported lock entry at {lock}:{number}: {raw!r}")
        packages.append((match.group(1), match.group(2)))
    if not python_version or not packages:
        raise LockError(f"lock needs a '# Python: x.y.z' header and pinned packages: {lock}")
    return lock, python_version, packages


def inspect_interpreter(interpreter: str) -> dict:
    code = r'''import importlib, importlib.metadata as metadata, json, platform, sys
dist_names = json.loads(sys.argv[1])
module_names = {"numpy": "numpy", "opencv-python": "cv2",
                "opencv-python-headless": "cv2", "Pillow": "PIL"}
packages, import_errors = {}, {}
for name in dist_names:
    try:
        packages[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        packages[name] = None
    module = module_names[name]
    try:
        importlib.import_module(module)
    except Exception as exc:
        import_errors[name] = f"{type(exc).__name__}: {exc}"
print(json.dumps({"python": platform.python_version(), "executable": sys.executable,
                  "packages": packages, "import_errors": import_errors}))
'''
    package_names = ["numpy", "opencv-python", "opencv-python-headless", "Pillow"]
    proc = subprocess.run([interpreter, "-c", code, json.dumps(package_names)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise LockError(f"could not inspect selected Python {interpreter}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise LockError(f"selected Python returned invalid dependency data: {proc.stdout!r}") from exc


def compare_lock(target: str, interpreter: str) -> dict:
    lock, expected_python, packages = read_lock(target)
    actual = inspect_interpreter(interpreter)
    mismatches = []
    if actual["python"] != expected_python:
        mismatches.append({"package": "Python", "expected": expected_python,
                           "actual": actual["python"]})
    installed = actual["packages"]
    version_status: dict[str, str] = {}
    newer_accepted: list[dict[str, str]] = []
    aliases = {"opencv-python": ("opencv-python", "opencv-python-headless"),
               "opencv-python-headless": ("opencv-python-headless", "opencv-python")}
    for name, expected in packages:
        actual_version = next((installed.get(alias) for alias in aliases.get(name, (name,))
                               if installed.get(alias)), None)
        relation = version_relation(actual_version, expected)
        version_status[name] = relation
        if relation == "newer":
            newer_accepted.append({"package": name, "target": expected,
                                   "installed": str(actual_version)})
        elif relation not in ("exact",):
            mismatches.append({"package": name, "expected": expected,
                               "actual": actual_version, "version_status": relation})
        if name in actual.get("import_errors", {}):
            mismatches.append({"package": name, "expected_import": "working",
                               "actual_import_error": actual["import_errors"][name]})
    return {"target": target, "lock": str(lock), "python": actual["python"],
            "interpreter": actual["executable"], "packages": installed,
            "import_errors": actual.get("import_errors", {}),
            "package_version_status": version_status,
            "newer_versions_accepted": newer_accepted,
            "expected_packages": dict(packages), "mismatches": mismatches,
            "ok": not mismatches}


def shared_redactions(profile_path: str | Path, profile: dict) -> list[tuple[str, str]]:
    """Map machine-local paths to stable logical labels before publishing logs."""
    pairs = [(str(Path(profile_path).resolve()), "<device-local-profile>")]
    if str(Path.home()) not in ("", "."):
        pairs.append((str(Path.home().resolve()), "<user-home>"))
    shared_root = None
    shared_value = (profile.get("shared") or {}).get("release_root")
    if shared_value:
        shared_root = Path(os.path.expandvars(os.path.expanduser(str(shared_value)))).resolve()
        pairs.append((str(shared_root), "<shared-release-root>"))
    for section in ("tools", "roots", "python"):
        values = profile.get(section) or {}
        for name, raw in values.items():
            if not raw or not isinstance(raw, (str, os.PathLike)):
                continue
            expanded = os.path.expandvars(os.path.expanduser(str(raw)))
            path = Path(expanded)
            if path.is_absolute():
                if shared_root is not None:
                    try:
                        path.resolve().relative_to(shared_root)
                        continue
                    except ValueError:
                        pass
                pairs.append((str(path.resolve()), f"<{section}.{name}>"))
    # Replace longer paths first so a work sub-root becomes its own logical token.
    return sorted(set(pairs), key=lambda item: len(item[0]), reverse=True)


def redact_shared_text(text: str, redactions: list[tuple[str, str]]) -> str:
    for original, replacement in redactions:
        # Reports are sometimes redacted after json.dumps(). Match that escaped
        # representation first; otherwise a drive root such as ``D:\`` can
        # consume only the first slash of JSON's ``D:\\`` and leave an invalid
        # ``\D`` escape behind.
        escaped = original.replace("\\", "\\\\")
        if escaped != original:
            escaped_replacement = replacement + ("\\\\" if original.endswith("\\") else "")
            text = re.sub(re.escape(escaped), lambda _: escaped_replacement,
                          text, flags=re.IGNORECASE)
        forward = original.replace("\\", "/")
        if forward != original:
            forward_replacement = replacement + ("/" if original.endswith(("\\", "/")) else "")
            text = re.sub(re.escape(forward), lambda _: forward_replacement,
                          text, flags=re.IGNORECASE)
        # In plain text, replace the original spelling. The negative lookahead
        # prevents a trailing drive-root slash from matching half of JSON's
        # doubled backslash if an earlier escaped-form replacement did not apply.
        raw_pattern = re.escape(original)
        raw_replacement = replacement
        if original.endswith("\\"):
            raw_pattern += r"(?!\\)"
        if original.endswith(("\\", "/")):
            raw_replacement += original[-1]
        text = re.sub(raw_pattern, lambda _: raw_replacement,
                      text, flags=re.IGNORECASE)
    return text
