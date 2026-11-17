#!/usr/bin/env python3
"""Inventory a selected local LaserDisc rip without claiming Archive verification."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EPISODE_RE = re.compile(r"S\d{2}E\d{2}", re.IGNORECASE)
SERIES_PREFIX_RE = re.compile(r"\b(DS9|VOY|TNG)-", re.IGNORECASE)
CAPTURE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
EDGE_BYTES = 4 * 1024 * 1024
RAW_RF_SUFFIXES = (".lds", ".ldf", ".s16", ".raw")
AMBIGUOUS_RF_SUFFIXES = (".flac", ".wav")


class LocalSourceError(ValueError):
    pass


def _selected_root(path_value: str | Path) -> tuple[Path, Path | None]:
    selected = Path(path_value).expanduser()
    if not selected.is_absolute():
        raise LocalSourceError("local source selection must be an absolute path")
    if selected.is_symlink():
        raise LocalSourceError("local source selection must not be a symbolic link")
    selected = selected.resolve(strict=False)
    if not selected.exists():
        raise LocalSourceError(f"selected local source does not exist: {selected}")
    if selected.is_dir():
        return selected, None
    if selected.is_file() and selected.name.casefold().endswith(
            (".tbc", ".tbc.json", ".efm", ".efm.7z", "download-manifest.csv")):
        return selected.parent, selected
    if selected.is_file() and selected.suffix.casefold() in RAW_RF_SUFFIXES:
        raise LocalSourceError(
            f"raw RF capture detected ({selected.suffix}); this intake currently needs decoded .tbc files. "
            "Raw-RF decoding is planned but not implemented yet")
    if selected.is_file() and selected.suffix.casefold() in AMBIGUOUS_RF_SUFFIXES:
        raise LocalSourceError(
            f"{selected.suffix} alone does not identify the source format (it may be audio or RF); "
            "identify its format or select a decoded TBC folder. Raw-RF decoding is not implemented yet")
    raise LocalSourceError(
        "select a folder or one existing TBC, TBC JSON, EFM, or download-manifest.csv file")


def inspect_local_source(path_value: str | Path) -> dict[str, Any]:
    """Validate pairing/identity and return a lightweight inventory without reading media payloads."""
    source_dir, selected_file = _selected_root(path_value)
    tbc_paths = sorted((p for p in source_dir.iterdir()
                        if p.is_file() and p.suffix.casefold() == ".tbc"),
                       key=lambda p: p.name.casefold())
    if not 2 <= len(tbc_paths) <= 6:
        raw_rf = sorted(p.name for p in source_dir.iterdir()
                        if p.is_file() and p.suffix.casefold() in RAW_RF_SUFFIXES)
        ambiguous = sorted(p.name for p in source_dir.iterdir()
                           if p.is_file() and p.suffix.casefold() in AMBIGUOUS_RF_SUFFIXES)
        if raw_rf:
            raise LocalSourceError(
                "raw RF capture(s) detected: " + ", ".join(raw_rf[:6]) +
                ". This intake currently needs decoded .tbc captures; raw-RF decoding is planned but not implemented yet")
        if not tbc_paths and ambiguous:
            raise LocalSourceError(
                "only .flac/.wav files were found and their role is ambiguous (audio vs RF); "
                "identify the format or select a decoded TBC folder")
        raise LocalSourceError(f"local source needs 2..6 TBC captures for the weighted-stack workflow; found {len(tbc_paths)}")

    archive_ids: set[str] = set()
    optional_manifest_warning = ""
    csv_path = source_dir / "download-manifest.csv"
    if csv_path.is_file():
        try:
            import csv
            with csv_path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if "ArchiveId" in (reader.fieldnames or []):
                    rows = list(reader)
                    archive_ids = {str(row.get("ArchiveId", "")).strip() for row in rows
                                   if str(row.get("ArchiveId", "")).strip()}
                else:
                    optional_manifest_warning = "existing download-manifest.csv has no ArchiveId column; ignored for local identity"
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            optional_manifest_warning = f"existing download-manifest.csv could not be read and was ignored: {exc}"
        if len(archive_ids) > 1:
            optional_manifest_warning = f"existing download-manifest.csv has multiple Archive IDs and was ignored: {sorted(archive_ids)}"
            archive_ids.clear()

    candidates: list[dict[str, Any]] = []
    labels: set[str] = set()
    codes: set[str] = set()
    series_codes: set[str] = set()
    for tbc in tbc_paths:
        if tbc.is_symlink():
            raise LocalSourceError(f"TBC source is a symbolic link: {tbc.name}")
        sidecar = Path(str(tbc) + ".json")
        if not sidecar.is_file():
            raise LocalSourceError(f"TBC capture has no matching JSON sidecar: {sidecar.name}")
        if sidecar.is_symlink():
            raise LocalSourceError(f"TBC JSON sidecar is a symbolic link: {sidecar.name}")
        stem = tbc.stem
        label = stem.rsplit("-", 1)[-1]
        if not CAPTURE_RE.fullmatch(label):
            raise LocalSourceError(f"cannot derive a safe capture label from {tbc.name!r}")
        if label.casefold() in labels:
            raise LocalSourceError(f"capture labels are not unique: {label}")
        labels.add(label.casefold())
        codes.update(code.upper() for code in EPISODE_RE.findall(tbc.name))
        series_codes.update(match.upper() for match in SERIES_PREFIX_RE.findall(tbc.name))
        efm_candidates = [p for p in (source_dir / f"{stem}.efm", source_dir / f"{stem}.efm.7z")
                          if p.is_file()]
        if len(efm_candidates) > 1:
            raise LocalSourceError(f"multiple EFM candidates for capture {label}: {[p.name for p in efm_candidates]}")
        if efm_candidates and efm_candidates[0].is_symlink():
            raise LocalSourceError(f"EFM source is a symbolic link: {efm_candidates[0].name}")
        candidates.append({"label": label, "tbc": tbc, "json": sidecar,
                           "efm": efm_candidates[0] if efm_candidates else None})

    for candidate in (source_dir.name, selected_file.name if selected_file else ""):
        codes.update(code.upper() for code in EPISODE_RE.findall(candidate))
        series_codes.update(match.upper() for match in SERIES_PREFIX_RE.findall(candidate))
    for item in archive_ids:
        codes.update(code.upper() for code in EPISODE_RE.findall(item))
        series_codes.update(match.upper() for match in SERIES_PREFIX_RE.findall(item))
    if len(codes) != 1:
        detail = "no SxxExx code found" if not codes else f"multiple codes found: {sorted(codes)}"
        raise LocalSourceError(f"cannot uniquely identify the local source episode: {detail}")
    episode = next(iter(codes))
    if len(series_codes) > 1:
        raise LocalSourceError(f"local capture set contains multiple series prefixes: {sorted(series_codes)}")
    series_inferred = bool(series_codes)
    series = next(iter(series_codes), "DS9")
    if series == "TNG":
        raise LocalSourceError("TNG combined/extra items are not supported for single-episode local intake")

    if selected_file and selected_file.name.casefold() != "download-manifest.csv":
        selected_capture_files = {p.resolve() for row in candidates for p in (row["tbc"], row["json"], row["efm"])
                                  if p is not None}
        if selected_file.resolve() not in selected_capture_files:
            raise LocalSourceError(f"selected file is not part of the detected TBC capture set: {selected_file}")

    captures = []
    for row in candidates:
        record = {"label": row["label"]}
        for key in ("tbc", "json", "efm"):
            path = row[key]
            record[f"{key}_file"] = path.name if path else ""
            record[f"{key}_bytes"] = path.stat().st_size if path else None
        captures.append(record)

    known = {p.name.casefold() for row in candidates for p in (row["tbc"], row["json"], row["efm"])
             if p is not None}
    if csv_path.is_file():
        known.add(csv_path.name.casefold())
    ignored = sorted(p.name for p in source_dir.iterdir()
                     if p.is_file() and p.name.casefold() not in known)
    raw_rf_files = sorted(p.name for p in source_dir.iterdir()
                          if p.is_file() and p.suffix.casefold() in RAW_RF_SUFFIXES)
    return {
        "schema": "ld-pipeline-local-source-scan-v1",
        "source_dir": str(source_dir),
        "selected_path": str(selected_file.resolve() if selected_file else source_dir),
        "episode": episode,
        "series": series,
        "series_inferred": series_inferred,
        "archive_id_hint": next(iter(archive_ids), ""),
        "download_manifest_present": csv_path.is_file(),
        "optional_manifest_warning": optional_manifest_warning,
        "captures": captures,
        "raw_rf_files": raw_rf_files,
        "ignored_files": ignored,
    }


def fingerprint_file(path: Path, policy: str = "balanced") -> dict[str, Any]:
    """Hash every file or, for large media, hash its first and last blocks."""
    if policy not in ("balanced", "full"):
        raise LocalSourceError("fingerprint policy must be 'balanced' or 'full'")
    before = path.stat()
    full = policy == "full" or before.st_size <= EDGE_BYTES * 2
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if full:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        else:
            digest.update(stream.read(EDGE_BYTES))
            stream.seek(max(0, before.st_size - EDGE_BYTES))
            digest.update(stream.read(EDGE_BYTES))
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise LocalSourceError(f"source file changed while it was fingerprinted: {path}")
    return {"size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
            "sha256": digest.hexdigest(), "hash_scope": "full" if full else "first-last-4MiB"}


def build_local_source_report(scan: dict[str, Any], *, policy: str = "balanced") -> dict[str, Any]:
    if scan.get("schema") != "ld-pipeline-local-source-scan-v1":
        raise LocalSourceError("unsupported local-source scan record")
    root = Path(scan["source_dir"])
    captured = []
    for capture in scan["captures"]:
        row = {"label": capture["label"], "files": {}}
        for key in ("tbc", "json", "efm"):
            name = capture.get(f"{key}_file")
            if not name:
                continue
            row["files"][key] = {"name": name, **fingerprint_file(root / name, policy)}
        captured.append(row)
    return {
        "schema": "ld-pipeline-local-source-inventory-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": scan["episode"],
        "source_mode": "local-inventory",
        "source_dir": scan["source_dir"],
        "selected_path": scan["selected_path"],
        "archive_id_hint": scan.get("archive_id_hint", ""),
        "archive_verified": False,
        "verification_method": "local SHA-256 file fingerprint; not proof of Archive origin or authenticity",
        "fingerprint_policy": policy,
        "captures": captured,
        "raw_rf_files": scan.get("raw_rf_files", []),
        "ignored_files": scan.get("ignored_files", []),
    }
