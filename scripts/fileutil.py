"""Shared streaming file hashes used by workflow reports and provenance checks."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

DEFAULT_HASH_CHUNK_BYTES = 1024 * 1024


def hash_file(path: Path, *, algorithm: str = "sha256",
              chunk_size: int = DEFAULT_HASH_CHUNK_BYTES,
              on_chunk: Callable[[], None] | None = None) -> str:
    """Hash a file incrementally, optionally notifying after each data block.

    Chunk size and callbacks remain caller-controlled so existing performance and
    workflow-heartbeat behavior can be preserved while sharing one implementation.
    """
    if chunk_size <= 0:
        raise ValueError("hash chunk size must be positive")
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(chunk_size), b""):
            digest.update(block)
            if on_chunk:
                on_chunk()
    return digest.hexdigest()


def sha256_file(path: Path, *, chunk_size: int = DEFAULT_HASH_CHUNK_BYTES) -> str:
    return hash_file(path, algorithm="sha256", chunk_size=chunk_size)


def md5_file(path: Path, *, chunk_size: int = DEFAULT_HASH_CHUNK_BYTES) -> str:
    return hash_file(path, algorithm="md5", chunk_size=chunk_size)
