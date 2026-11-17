#!/usr/bin/env python3
"""Review and recycle exact episode downloads/audio while preserving evidence."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Any, Callable

from dependency_lock import redact_shared_text, shared_redactions
from workflow_app_model import WorkflowAppError

LARGE_AUDIO_BYTES = 100 * 1024 * 1024
PRESERVED_METADATA_BYTES = 5 * 1024 * 1024
PRESERVED_METADATA_SUFFIXES = {".csv", ".json", ".txt", ".md", ".xml"}
AUDIO_FILE_KEYS = {"ld_native", "ld_corrected", "ld_synced", "dvd51_source", "retimed_51"}
FINGERPRINT_CHUNK_BYTES = 4 * 1024 * 1024
FINGERPRINT_FULL_LIMIT = 64 * 1024 * 1024
QC_SUMMARY_RE = re.compile(
    r"\*\*(\d+)\s+pass,\s*(\d+)\s+warn,\s*(\d+)\s+skip,\s*(\d+)\s+fail/unverified\*\*",
    re.IGNORECASE)


class CleanupError(WorkflowAppError):
    """Cleanup review or Recycle Bin operation could not be completed safely."""


@dataclass(frozen=True)
class CleanupCandidate:
    key: str
    category: str
    path: Path
    size_bytes: int
    mtime_ns: int
    file_count: int
    tree_snapshot: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class CleanupInventory:
    identity: str
    downloads: Path
    deliverable: Path
    qc_report: Path
    candidates: tuple[CleanupCandidate, ...]
    metadata_paths: tuple[Path, ...]
    large_audio_threshold_bytes: int = LARGE_AUDIO_BYTES


def cleanup_review_available(exit_code: str, action: str | None,
                             plan: dict[str, Any] | None) -> bool:
    """Expose cleanup only after an executed, successful plan that included full QC."""
    return (str(exit_code) == "0" and action in ("run", "resume", "approve") and
            any(step.get("stage") == "qc" for step in (plan or {}).get("steps", [])))


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        except OSError:
            return False
    return False


def _assert_no_reparse_components(path: Path) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if _is_link_or_reparse(component):
            raise CleanupError(f"refusing a symbolic-link/reparse path: {component}")


def _safe_file(path: Path) -> Path:
    _assert_no_reparse_components(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise CleanupError(f"expected a regular file: {resolved}")
    return resolved


def _tree_snapshot(root: Path) -> tuple[tuple[tuple[str, int, int], ...], int, int]:
    _assert_no_reparse_components(root)
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise CleanupError(f"download path is not a directory: {root}")
    rows: list[tuple[str, int, int]] = []
    total = 0
    file_count = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        dirs.sort(key=str.casefold)
        files.sort(key=str.casefold)
        for name in dirs:
            child = base / name
            if _is_link_or_reparse(child):
                raise CleanupError(f"download directory contains a symbolic link: {child}")
            stat = child.stat()
            relative = child.relative_to(root).as_posix()
            rows.append((f"D:{relative}", 0, stat.st_mtime_ns))
        for name in files:
            child = base / name
            if _is_link_or_reparse(child) or not child.is_file():
                raise CleanupError(f"download directory contains a non-regular entry: {child}")
            stat = child.stat()
            relative = child.relative_to(root).as_posix()
            rows.append((relative, stat.st_size, stat.st_mtime_ns))
            total += stat.st_size
            file_count += 1
    return tuple(rows), total, file_count


def _profile_root(context: dict[str, Any], key: str) -> Path | None:
    configured = (context.get("protected_roots") or {}).get(key)
    if configured:
        return Path(configured).resolve(strict=False)
    ld = context.get("ld")
    try:
        return Path(ld.root(key)).expanduser().resolve(strict=False) if ld else None
    except (OSError, RuntimeError, ValueError, KeyError):
        return None


def _overlaps(left: Path, right: Path) -> bool:
    a = left.resolve(strict=False)
    b = right.resolve(strict=False)
    return a == b or a in b.parents or b in a.parents


def _assert_not_protected(context: dict[str, Any], path: Path) -> None:
    for key in ("dvd_root", "prores", "out"):
        root = _profile_root(context, key)
        if root and _overlaps(path, root):
            raise CleanupError(f"cleanup candidate overlaps protected roots.{key}: {path}")
    instance = Path(context["instance"]).resolve(strict=True)
    for protected in (instance / "manifest", instance / "logs", instance / "plans",
                      instance / "references", instance / "cut-pad-images"):
        if _overlaps(path, protected):
            raise CleanupError(f"cleanup candidate overlaps preserved episode evidence: {path}")


def _path_id(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path, policy: str) -> dict[str, Any]:
    path = _safe_file(path)
    stat = path.stat()
    row: dict[str, Any] = {"kind": "file", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if policy == "full" or stat.st_size <= FINGERPRINT_FULL_LIMIT:
        row["method"] = "sha256"
        row["sha256"] = _sha256(path)
    else:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            digest.update(stream.read(FINGERPRINT_CHUNK_BYTES))
            stream.seek(max(0, stat.st_size - FINGERPRINT_CHUNK_BYTES))
            digest.update(stream.read(FINGERPRINT_CHUNK_BYTES))
        row["method"] = "stat+edge-sha256"
        row["edge_sha256"] = digest.hexdigest()
    row["path_id"] = _path_id(path)
    return row


def _assert_fingerprint_matches(path: Path, rows: list[dict[str, Any]], policy: str,
                                role: str) -> None:
    actual = _file_fingerprint(path, policy)
    expected = next((row for row in rows if row.get("path_id") == actual["path_id"] or
                     (row.get("path") and Path(row["path"]).resolve(strict=False) == path.resolve())), None)
    if not expected or any(expected.get(key) != value for key, value in actual.items()):
        raise CleanupError(f"the {role} changed after QC; refresh QC before source cleanup")


def _verified_qc_step(plan: dict[str, Any], state_path: Path,
                      plan_path: Path) -> tuple[Path, dict[str, Any]]:
    _assert_no_reparse_components(plan_path)
    _assert_no_reparse_components(state_path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError(f"cannot read completed workflow state: {exc}") from exc
    if state.get("status") != "PASS":
        raise CleanupError("cleanup is available only after the complete workflow passes")
    if state.get("plan_sha256") != hashlib.sha256(plan_path.read_bytes()).hexdigest():
        raise CleanupError("saved workflow state does not match the currently open immutable plan")
    if (state.get("episode", "").casefold() != str(plan.get("episode", "")).casefold() or
            state.get("series", "DS9").upper() != str(plan.get("series", "DS9")).upper()):
        raise CleanupError("saved workflow state belongs to a different episode identity")
    state_steps = {row.get("id"): row for row in state.get("steps", []) if isinstance(row, dict)}
    qc_steps = [step for step in plan.get("steps", []) if step.get("stage") == "qc"]
    if not qc_steps:
        raise CleanupError("run delivery QC successfully before source cleanup is available")
    qc_step = qc_steps[-1]
    saved = state_steps.get(qc_step.get("id"))
    if not saved or saved.get("status") != "PASS" or saved.get("exit_code") != 0:
        raise CleanupError("the current workflow has not recorded a passing QC stage")
    reports = [Path(value).expanduser() for value in qc_step.get("outputs", [])
               if str(value).casefold().endswith(".md")]
    if len(reports) != 1:
        raise CleanupError("workflow plan does not identify exactly one QC markdown report")
    report = _safe_file(reports[0])
    policy = str(plan.get("fingerprint_policy", "balanced"))
    if policy not in ("balanced", "full"):
        raise CleanupError("workflow has an unsupported fingerprint policy")
    _assert_fingerprint_matches(report, saved.get("output_fingerprints") or [], policy, "QC report")
    return report, saved


def _require_clean_qc(report_path: Path) -> None:
    try:
        text = report_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CleanupError(f"cannot read the current QC report: {exc}") from exc
    summary = QC_SUMMARY_RE.search(text)
    if not summary:
        raise CleanupError("QC report has no recognized final gate summary")
    _passes, warnings, _skips, failures = (int(value) for value in summary.groups())
    if warnings or failures:
        raise CleanupError(
            f"source cleanup requires QC with zero warnings/limits and zero failures; "
            f"report records {warnings} warning(s), {failures} failure(s)")
    table_rows = []
    in_table = False
    for line in text.splitlines():
        if line.strip().casefold().startswith("| gate | status | detail |"):
            in_table = True
            continue
        if in_table and line.strip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[1].upper() not in ("---", "STATUS"):
                table_rows.append(cells[1].upper())
        elif in_table and line.strip():
            in_table = False
    if not table_rows or any(status not in ("PASS", "SKIP") for status in table_rows):
        raise CleanupError("QC report contains a non-PASS gate; source cleanup is blocked")


def _metadata_candidates(download_root: Path,
                         snapshot: tuple[tuple[str, int, int], ...]) -> tuple[Path, ...]:
    paths = []
    for relative, size, _mtime in snapshot:
        if relative.startswith("D:"):
            continue
        path = download_root / Path(relative)
        suffix = path.suffix.casefold()
        is_download_manifest = path.name.casefold() == "download-manifest.csv"
        is_tbc_sidecar = path.name.casefold().endswith(".tbc.json")
        if size <= PRESERVED_METADATA_BYTES and (
                is_download_manifest or is_tbc_sidecar or suffix in PRESERVED_METADATA_SUFFIXES):
            paths.append(path)
    return tuple(paths)


def _audio_candidates(context: dict[str, Any]) -> list[CleanupCandidate]:
    raw_audio_root = Path(context.get("audio_root_raw", context["audio_root"])).expanduser()
    _assert_no_reparse_components(raw_audio_root)
    audio_root = Path(context["audio_root"]).resolve(strict=True)
    data = context.get("manifest_data") or {}
    paths = context.get("file_paths") or {}
    candidates: dict[str, CleanupCandidate] = {}

    def add_candidate(value: str | Path | None, label: str) -> None:
        if not value:
            return
        raw = Path(value).expanduser()
        _assert_no_reparse_components(raw)
        if not raw.is_file():
            return
        path = raw.resolve(strict=True)
        try:
            path.relative_to(audio_root)
        except ValueError as exc:
            raise CleanupError(f"audio cleanup candidate is outside roots.audio: {path}") from exc
        _assert_not_protected(context, path)
        stat = path.stat()
        if stat.st_size < LARGE_AUDIO_BYTES:
            return
        key = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]
        candidates[key] = CleanupCandidate(key, f"Large audio - {label}", path,
                                           stat.st_size, stat.st_mtime_ns, 1)

    for key, value in paths.items():
        spec = (data.get("files") or {}).get(key)
        root = spec.get("root") if isinstance(spec, dict) else None
        suffix = Path(value).suffix.casefold()
        if key in AUDIO_FILE_KEYS or (root == "audio" and suffix in {".wav", ".flac", ".ac3", ".wavpack"}):
            add_candidate(value, key)

    for capture in context.get("captures") or []:
        label = str(capture.get("label") or "").strip()
        if not label or not re.fullmatch(r"[A-Za-z0-9_-]+", label):
            continue
        episode = str(context["episode"])
        for candidate in (audio_root / episode / f"{label}-efm.wav",
                          audio_root / f"{episode}-{label}-efm.wav",
                          audio_root / f"{label}-efm.wav"):
            if candidate.is_file():
                add_candidate(candidate, f"{label} EFM WAV")
    return sorted(candidates.values(), key=lambda row: (-row.size_bytes, row.path.name.casefold()))


def build_cleanup_inventory(context: dict[str, Any], plan: dict[str, Any],
                            state_path: str | Path, plan_path: str | Path) -> CleanupInventory:
    """Require a fresh, clean QC result before proposing source deletion."""
    report, qc_state_step = _verified_qc_step(
        plan, Path(state_path).expanduser(), Path(plan_path).expanduser())
    _require_clean_qc(report)
    data = context.get("manifest_data") or {}
    deliverable_key = str(data.get("deliverable") or "")
    deliverable_value = (context.get("file_paths") or {}).get(deliverable_key)
    if not deliverable_value:
        raise CleanupError("manifest does not identify the final delivery file")
    deliverable = _safe_file(Path(deliverable_value).expanduser())
    _assert_fingerprint_matches(
        deliverable, qc_state_step.get("input_fingerprints") or [],
        str(plan.get("fingerprint_policy", "balanced")), "final deliverable")
    manifest_path = _safe_file(Path(context["manifest"]).expanduser())
    _assert_fingerprint_matches(
        manifest_path, qc_state_step.get("input_fingerprints") or [],
        str(plan.get("fingerprint_policy", "balanced")), "episode manifest")
    raw_downloads = Path(context.get("downloads_raw", context["downloads"])).expanduser()
    _assert_no_reparse_components(raw_downloads)
    downloads = raw_downloads
    _assert_not_protected(context, downloads)
    candidates: list[CleanupCandidate] = []
    metadata: tuple[Path, ...] = ()
    if downloads.exists():
        snapshot, size, count = _tree_snapshot(downloads)
        if str(context["episode"]).casefold() not in downloads.name.casefold():
            raise CleanupError(f"download folder name does not identify this episode: {downloads}")
        expected_names = {str(cap.get(key) or "") for cap in context.get("captures") or []
                          for key in ("tbc_file", "json_file", "efm_file")}
        expected_names.discard("")
        present = {Path(relative).name for relative, _bytes, _mtime in snapshot
                   if not relative.startswith("D:")}
        if not (expected_names & present):
            raise CleanupError("download folder does not contain a manifest-listed capture file")
        resolved_downloads = downloads.resolve(strict=True)
        key = "downloads"
        candidates.append(CleanupCandidate(key, "Episode source-download folder",
                                           resolved_downloads, size,
                                           resolved_downloads.stat().st_mtime_ns, count, snapshot))
        metadata = _metadata_candidates(resolved_downloads, snapshot)
    audio = _audio_candidates(context)
    download_path = downloads.resolve(strict=False)
    candidates.extend(row for row in audio if download_path not in row.path.parents)
    if not candidates:
        raise CleanupError("no episode download folder or audio files meet the cleanup criteria")
    return CleanupInventory(str(context["identity"]), downloads.resolve(strict=False), deliverable,
                            report, tuple(candidates), metadata)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def preserve_download_metadata(inventory: CleanupInventory,
                              instance: Path) -> tuple[dict[str, Any], ...]:
    """Copy only small manifest/sidecar/text metadata into an exclusive evidence folder."""
    if not inventory.metadata_paths:
        return ()
    _assert_no_reparse_components(instance)
    instance = instance.resolve(strict=True)
    references = instance / "references"
    if _is_link_or_reparse(references):
        raise CleanupError(f"references directory may not be a symbolic link: {references}")
    references.mkdir(parents=True, exist_ok=True)
    base = references / "post-run-cleanup"
    if _is_link_or_reparse(base):
        raise CleanupError(f"cleanup metadata directory may not be a symbolic link: {base}")
    base.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = base / f"{inventory.identity.replace(':', '-')}-{stamp}-{uuid.uuid4().hex[:8]}"
    destination.mkdir()
    preserved = []
    for source in inventory.metadata_paths:
        source = _safe_file(source)
        try:
            relative = source.relative_to(inventory.downloads)
        except ValueError as exc:
            raise CleanupError(f"metadata file is outside the reviewed download folder: {source}") from exc
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = source.stat()
        digest = hashlib.sha256()
        with source.open("rb") as src, target.open("xb") as dst:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(block)
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        after = source.stat()
        target_stat = target.stat()
        source_hash = digest.hexdigest()
        if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or
                target_stat.st_size != after.st_size or _sha256(target) != source_hash):
            raise CleanupError(f"preserved metadata verification failed; original remains in place: {source}")
        shutil.copystat(source, target, follow_symlinks=False)
        preserved.append({"source_name": relative.as_posix(), "preserved_path": str(target),
                          "size_bytes": target_stat.st_size, "sha256": source_hash})
    return tuple(preserved)


def _assert_trash_available(path: Path) -> None:
    if sys.platform == "win32":
        import ctypes
        drive = path.drive + "\\" if path.drive else ""
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive) if drive else 0
        if drive_type != 3:  # DRIVE_FIXED only; don't risk network/removable-volume deletion.
            raise CleanupError(f"Recycle Bin cleanup is enabled only for fixed local drives: {path}")
        if not Path(drive, "$Recycle.Bin").is_dir():
            raise CleanupError(f"this volume has no accessible Recycle Bin: {path}")
    elif sys.platform == "darwin":
        if not shutil.which("osascript"):
            raise CleanupError("osascript is unavailable; no permanent-delete fallback is allowed")
    else:
        raise CleanupError("Recycle Bin/Trash cleanup is not enabled on this platform")


def _windows_send_to_recycle_bin(path: Path) -> None:
    import ctypes.wintypes as wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    multi_string = ctypes.create_unicode_buffer(str(path) + "\0")
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = ctypes.cast(multi_string, wintypes.LPCWSTR)
    operation.fFlags = 0x0004 | 0x0010 | 0x0040 | 0x0400  # silent, no prompts, undo, no error UI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise CleanupError(f"Windows Recycle Bin operation failed (code {result}); no permanent-delete fallback")


def _macos_send_to_trash(path: Path) -> None:
    script = ("on run argv\n"
              "tell application \"Finder\"\n"
              "set targetItem to POSIX file (item 1 of argv) as alias\n"
              "delete targetItem\n"
              "end tell\n"
              "end run")
    try:
        result = subprocess.run(["osascript", "-e", script, str(path)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"macOS Trash operation could not complete: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "Finder refused the Trash operation").strip()
        raise CleanupError(detail)


def send_to_trash(path: str | Path) -> None:
    """Move one reviewed path to the platform trash; never permanently delete as fallback."""
    target = Path(path).expanduser()
    _assert_no_reparse_components(target)
    target = target.resolve(strict=True)
    _assert_trash_available(target)
    if sys.platform == "win32":
        _windows_send_to_recycle_bin(target)
    elif sys.platform == "darwin":
        _macos_send_to_trash(target)
    else:
        raise CleanupError("Recycle Bin/Trash cleanup is not enabled on this platform")
    if target.exists() or _is_link_or_reparse(target):
        raise CleanupError(f"Recycle Bin/Trash reported success but the original path remains: {target}")


def _write_cleanup_event(log_path: Path, event: dict[str, Any], redactions) -> None:
    encoded = json.dumps(event, ensure_ascii=False)
    if redactions:
        encoded = redact_shared_text(encoded, redactions)
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _verify_candidate_snapshot(candidate: CleanupCandidate) -> None:
    if _is_link_or_reparse(candidate.path) or not candidate.path.exists():
        raise CleanupError(f"cleanup candidate changed or disappeared: {candidate.path}")
    if candidate.key == "downloads":
        snapshot, size, count = _tree_snapshot(candidate.path)
        if (snapshot != candidate.tree_snapshot or size != candidate.size_bytes or
                count != candidate.file_count):
            raise CleanupError("download-folder contents changed after review; refresh the inventory")
    else:
        stat = candidate.path.stat()
        if stat.st_size != candidate.size_bytes or stat.st_mtime_ns != candidate.mtime_ns:
            raise CleanupError(f"audio file changed after review: {candidate.path}")


def execute_cleanup(inventory: CleanupInventory, selected_keys: set[str],
                    context: dict[str, Any], plan: dict[str, Any], state_path: str | Path,
                    plan_path: str | Path,
                    *, trash: Callable[[Path], None] = send_to_trash) -> dict[str, Any]:
    """Preserve small download metadata, then move selected targets to OS Trash."""
    selected = [candidate for candidate in inventory.candidates if candidate.key in selected_keys]
    if not selected or len(selected) != len(selected_keys):
        raise CleanupError("select one or more reviewed cleanup candidates")
    current_qc, qc_state_step = _verified_qc_step(
        plan, Path(state_path).expanduser(), Path(plan_path).expanduser())
    _require_clean_qc(current_qc)
    if current_qc != inventory.qc_report:
        raise CleanupError("QC report or final deliverable changed after cleanup review; refresh the inventory")
    _assert_fingerprint_matches(
        inventory.deliverable, qc_state_step.get("input_fingerprints") or [],
        str(plan.get("fingerprint_policy", "balanced")), "final deliverable")
    for candidate in selected:
        _verify_candidate_snapshot(candidate)
        _assert_trash_available(candidate.path)

    instance = Path(context["instance"]).expanduser()
    _assert_no_reparse_components(instance)
    logs = instance / "logs"
    if _is_link_or_reparse(logs) or not logs.is_dir():
        raise CleanupError(f"episode logs folder is missing or unsafe: {logs}")
    profile = context.get("ld").profile if context.get("ld") else {}
    shared = (profile.get("shared") or {}).get("enabled") is True
    redactions = (shared_redactions(context["profile"], profile) if shared else [])
    record = logs / f"post-run-cleanup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:6]}.jsonl"
    with record.open("x", encoding="utf-8", newline="\n"):
        pass
    _write_cleanup_event(record, {
        "schema": "fromstuntokill-post-run-cleanup-v1", "event": "approved",
        "created_utc": datetime.now(timezone.utc).isoformat(), "identity": inventory.identity,
        "qc_report": str(inventory.qc_report), "deliverable": str(inventory.deliverable),
        "selected": [{"category": row.category, "path": str(row.path),
                      "size_bytes": row.size_bytes} for row in selected],
        "permanent_deletion": False,
    }, redactions)

    preserved: tuple[dict[str, Any], ...] = ()
    if any(row.key == "downloads" for row in selected):
        try:
            preserved = preserve_download_metadata(inventory, instance)
        except Exception as exc:
            _write_cleanup_event(record, {
                "event": "metadata-preservation-failed",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "error": str(exc), "no_cleanup_items_moved": True,
            }, redactions)
            if isinstance(exc, CleanupError):
                raise
            raise CleanupError(f"metadata preservation failed; no cleanup items were moved: {exc}") from exc
        _write_cleanup_event(record, {
            "event": "metadata-preserved", "created_utc": datetime.now(timezone.utc).isoformat(),
            "metadata": list(preserved),
        }, redactions)

    moved, failures = [], []
    for candidate in selected:
        try:
            _verify_candidate_snapshot(candidate)
            trash(candidate.path)
            moved.append(candidate.key)
            _write_cleanup_event(record, {
                "event": "moved-to-trash", "created_utc": datetime.now(timezone.utc).isoformat(),
                "candidate": candidate.key, "path": str(candidate.path),
                "size_bytes": candidate.size_bytes,
            }, redactions)
        except Exception as exc:
            failures.append({"candidate": candidate.key, "error": str(exc)})
            _write_cleanup_event(record, {
                "event": "trash-failed", "created_utc": datetime.now(timezone.utc).isoformat(),
                "candidate": candidate.key, "error": str(exc),
            }, redactions)
            break
    return {"record": record, "moved": moved, "failures": failures,
            "preserved_metadata": list(preserved), "complete": not failures and len(moved) == len(selected)}


class PostRunCleanupWindow:
    """Two-choice post-run panel followed by an exact-path inventory/confirmation."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any], plan: dict[str, Any],
                 plan_path: Path, state_path: Path, on_complete: Callable[[], None] | None = None):
        self.parent = parent
        self.context = context
        self.plan = plan
        self.plan_path = plan_path
        self.state_path = state_path
        self.on_complete = on_complete
        self.window: tk.Toplevel | None = None
        self.body: ttk.Frame | None = None
        self.inventory: CleanupInventory | None = None
        self.tree: ttk.Treeview | None = None
        self.detail: tk.Text | None = None
        self.status = tk.StringVar(value="No files have been changed.")

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Post-run storage - {self.context['identity']}")
        self.window.geometry("790x510")
        self.window.minsize(700, 430)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.body = ttk.Frame(self.window, padding=14)
        self.body.pack(fill="both", expand=True)
        self._show_choices()
        self.window.grab_set()

    def _clear_body(self) -> None:
        assert self.body is not None
        for child in self.body.winfo_children():
            child.destroy()

    def _show_choices(self) -> None:
        self._clear_body()
        assert self.body is not None and self.window is not None
        ttk.Label(self.body, text=f"Post-run storage - {self.context['identity']}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(self.body,
                  text=("Cleanup is optional. No files are removed unless you review the exact paths "
                        "and confirm. Logs, plans, manifests, QC reports, provenance, and metadata are kept."),
                  wraplength=750, justify="left").pack(anchor="w", pady=(6, 16))
        ttk.Button(self.body, text="Keep everything for now", command=self.close).pack(
            anchor="w", pady=5, ipadx=10, ipady=6)
        trash_supported = sys.platform in ("win32", "darwin")
        cleanup_button = ttk.Button(
            self.body, text="Free up space - review downloads and large audio files...",
            command=self._show_inventory, state="normal" if trash_supported else "disabled")
        cleanup_button.pack(anchor="w", pady=5, ipadx=10, ipady=6)
        ttk.Label(self.body,
                  text=(f"Large audio means files at least {LARGE_AUDIO_BYTES // (1024 * 1024)} MiB. "
                        "The download folder is one exact episode folder; small manifest/sidecar/text "
                        "metadata is preserved in the episode references folder. Selected files go "
                        "to the OS Recycle Bin/Trash, never permanent deletion."),
                  wraplength=750, justify="left").pack(anchor="w", pady=(12, 0))
        ttk.Label(self.body, textvariable=self.status, wraplength=750).pack(anchor="w", pady=(14, 0))
        if not trash_supported:
            ttk.Label(self.body, text="Moving items to Trash is not enabled on this platform.",
                      foreground="#8b1a1a").pack(anchor="w", pady=(5, 0))

    def _show_inventory(self) -> None:
        try:
            self.inventory = build_cleanup_inventory(
                self.context, self.plan, self.state_path, self.plan_path)
        except (CleanupError, OSError, ValueError) as exc:
            messagebox.showinfo("Cleanup is not available", str(exc), parent=self.window)
            return
        self._clear_body()
        assert self.body is not None and self.window is not None
        inventory = self.inventory
        total = sum(row.size_bytes for row in inventory.candidates)
        ttk.Label(self.body, text=f"Review exact cleanup targets - {inventory.identity}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self.body,
                  text=(f"{len(inventory.candidates)} candidate(s), {total / (1024 ** 3):.2f} GiB total. "
                        "All candidates start selected; unselect anything to keep. Nothing has moved yet."),
                  wraplength=750).pack(anchor="w", pady=(4, 8))
        table_frame = ttk.Frame(self.body)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=("category", "size", "name"),
                                 show="headings", selectmode="extended", height=8)
        for column, title, width in (("category", "Type", 230), ("size", "Size", 95), ("name", "Name", 300)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == "name")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for candidate in inventory.candidates:
            size = f"{candidate.size_bytes / (1024 ** 3):.2f} GiB" if candidate.size_bytes >= 1024 ** 3 else (
                f"{candidate.size_bytes / (1024 ** 2):.1f} MiB")
            self.tree.insert("", "end", iid=candidate.key,
                             values=(candidate.category, size, candidate.path.name))
        if inventory.candidates:
            self.tree.selection_set([row.key for row in inventory.candidates])
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_paths)
        selection_bar = ttk.Frame(self.body)
        selection_bar.pack(fill="x", pady=(3, 0))
        ttk.Button(selection_bar, text="Select all", command=self._select_all).pack(side="left")
        ttk.Button(selection_bar, text="Select none", command=self._select_none).pack(side="left", padx=5)
        detail_frame = ttk.LabelFrame(self.body, text="Exact paths and retained metadata", padding=5)
        detail_frame.pack(fill="x", pady=(8, 0))
        self.detail = tk.Text(detail_frame, wrap="word", height=5, state="disabled")
        self.detail.pack(fill="x", expand=True)
        self._show_selected_paths()
        if inventory.metadata_paths:
            ttk.Label(self.body,
                      text=(f"{len(inventory.metadata_paths)} small download metadata file(s) will be copied "
                            "and hash-verified in the episode references folder before the download folder "
                            "is moved to Trash."), wraplength=750).pack(anchor="w", pady=(6, 0))
        controls = ttk.Frame(self.body)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="Back", command=self._show_choices).pack(side="left")
        ttk.Button(controls, text="Move selected to Recycle Bin/Trash...",
                   command=self._confirm_cleanup).pack(side="right")

    def _show_selected_paths(self, _event=None) -> None:
        if not self.tree or not self.detail or not self.inventory:
            return
        selected = set(self.tree.selection())
        lines = [str(row.path) for row in self.inventory.candidates if row.key in selected]
        if "downloads" in selected and self.inventory.metadata_paths:
            lines.append("Preserved metadata will be copied under:")
            lines.append(str(Path(self.context["instance"]) / "references" / "post-run-cleanup"))
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", "\n".join(lines) if lines else "Select one or more rows to review exact paths.")
        self.detail.configure(state="disabled")

    def _select_all(self) -> None:
        if self.tree and self.inventory:
            self.tree.selection_set([row.key for row in self.inventory.candidates])

    def _select_none(self) -> None:
        if self.tree:
            self.tree.selection_remove(self.tree.selection())

    def _confirm_cleanup(self) -> None:
        if not self.inventory or not self.tree:
            return
        selected = set(self.tree.selection())
        chosen = [row for row in self.inventory.candidates if row.key in selected]
        if not chosen:
            messagebox.showinfo("Nothing selected", "Select at least one reviewed item.", parent=self.window)
            return
        total = sum(row.size_bytes for row in chosen)
        exact = "\n".join(str(row.path) for row in chosen)
        prompt = (f"Move these {len(chosen)} exact item(s) ({total / (1024 ** 3):.2f} GiB) to the OS "
                  f"Recycle Bin/Trash?\n\n{exact}\n\nLogs, metadata, QC evidence, and the delivery are kept. "
                  "This may prevent rerunning stages that need the removed source media. No permanent-delete "
                  "fallback will be attempted.")
        if not messagebox.askyesno("Confirm post-run cleanup", prompt, parent=self.window):
            return
        try:
            result = execute_cleanup(self.inventory, selected, self.context, self.plan,
                                     self.state_path, self.plan_path)
        except (CleanupError, OSError, ValueError) as exc:
            messagebox.showerror("Cleanup stopped", str(exc), parent=self.window)
            return
        if self.on_complete:
            self.on_complete(bool(result["complete"]))
        if result["failures"]:
            detail = "\n".join(f"{row['candidate']}: {row['error']}" for row in result["failures"])
            messagebox.showwarning(
                "Cleanup partially completed",
                f"Moved {len(result['moved'])} item(s) to Trash; stopped at the first failure. "
                f"No permanent-delete fallback was attempted.\n\nCleanup log: {result['record']}\n{detail}",
                parent=self.window)
        else:
            messagebox.showinfo(
                "Cleanup completed",
                f"Moved {len(result['moved'])} item(s) to Trash. Preserved metadata files: "
                f"{len(result['preserved_metadata'])}.\n\nCleanup log: {result['record']}",
                parent=self.window)
        if self.window:
            self.close()

    def close(self) -> None:
        if self.window:
            window, self.window = self.window, None
            try:
                if window.grab_current() is window:
                    window.grab_release()
            except tk.TclError:
                pass
            window.destroy()
