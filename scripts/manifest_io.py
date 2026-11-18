"""Shared exclusive-lock, backup, and atomic-replace primitives for manifests."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Type


class _ManifestLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("x", encoding="utf-8", newline="\n")
        self.stream.write(
            f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stream.close()
        self.path.unlink(missing_ok=True)
        return False


def manifest_lock(path: Path) -> _ManifestLock:
    """Create the same exclusive lock-file context used by the record helpers."""
    return _ManifestLock(path)


def exclusive_backup(path: Path, payload: bytes, *, label: str,
                     error_type: Type[Exception]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak-{label}-{stamp}")
    with backup.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if backup.read_bytes() != payload:
        raise error_type(f"manifest backup verification failed: {backup}")
    return backup


def atomic_replace(path: Path, payload: bytes, expected_original: bytes, *,
                   error_type: Type[Exception], changed_message: str,
                   pre_replace_message: str) -> None:
    if path.is_symlink() or path.read_bytes() != expected_original:
        raise error_type(changed_message)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != expected_original:
            raise error_type(pre_replace_message)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
