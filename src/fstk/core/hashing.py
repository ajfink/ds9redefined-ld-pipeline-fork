"""Streaming hashes and file fingerprints.

Two fingerprint scopes exist:

* ``full`` – SHA-256 of the whole file. Required for anything an approval or a manifest
  record binds to (reports, WAVs, PNG stills, encodes).
* ``head-tail`` – SHA-256 of the first and last 4 MiB. v38 calls this ``first-last-4MiB``
  ("balanced" policy) and uses it for 80 GB TBC captures where a full hash per resume is
  impractical. It detects replacement/truncation, not single-byte edits in the middle,
  and is labelled as such everywhere it is stored.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

CHUNK = 1024 * 1024
HEAD_TAIL_BYTES = 4 * 1024 * 1024

SCOPE_FULL = "full"
SCOPE_HEAD_TAIL = "head-tail"
_V38_SCOPE_ALIASES = {"first-last-4MiB": SCOPE_HEAD_TAIL, "full": SCOPE_FULL}


def hash_file(path: str | os.PathLike, *, algorithm: str = "sha256", chunk_size: int = CHUNK,
              on_chunk: Callable[[int], None] | None = None) -> str:
    """Hash a whole file incrementally. ``on_chunk`` receives bytes read so far."""
    if chunk_size <= 0:
        raise ValueError("chunk size must be positive")
    digest = hashlib.new(algorithm)
    done = 0
    with open(path, "rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
            done += len(block)
            if on_chunk:
                on_chunk(done)
    return digest.hexdigest()


def sha256_file(path: str | os.PathLike, **kw) -> str:
    return hash_file(path, algorithm="sha256", **kw)


def md5_file(path: str | os.PathLike, **kw) -> str:
    return hash_file(path, algorithm="md5", **kw)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def head_tail_sha256(path: str | os.PathLike, window: int = HEAD_TAIL_BYTES) -> tuple[str, str]:
    """Return ``(sha256, scope)`` hashing the first and last ``window`` bytes.

    Byte-compatible with v38 ``local_source.fingerprint_file`` (policy "balanced"): files of
    at most 2×window bytes are hashed whole and reported with scope ``full``. Size is not
    mixed into the digest; it is stored beside it. Fails if the file changes while hashed.
    """
    p = Path(path)
    before = p.stat()
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        if before.st_size <= 2 * window:
            scope = SCOPE_FULL
            while block := stream.read(CHUNK):
                digest.update(block)
        else:
            scope = SCOPE_HEAD_TAIL
            digest.update(stream.read(window))
            stream.seek(before.st_size - window)
            digest.update(stream.read(window))
    after = p.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError(f"file changed while it was fingerprinted: {p}")
    return digest.hexdigest(), scope


@dataclass(frozen=True)
class Fingerprint:
    path: str
    size_bytes: int
    mtime_ns: int
    sha256: str
    scope: str = SCOPE_FULL

    @classmethod
    def of(cls, path: str | os.PathLike, *, scope: str = SCOPE_FULL,
           on_chunk: Callable[[int], None] | None = None) -> Fingerprint:
        p = Path(path)
        stat = p.stat()
        if scope == SCOPE_FULL:
            digest = sha256_file(p, on_chunk=on_chunk)
            after = p.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError(f"file changed while it was fingerprinted: {p}")
        elif scope == SCOPE_HEAD_TAIL:
            digest, scope = head_tail_sha256(p)
        else:
            raise ValueError(f"unknown fingerprint scope {scope!r}")
        return cls(str(p), stat.st_size, stat.st_mtime_ns, digest, scope)

    def matches(self, path: str | os.PathLike | None = None, *, trust_mtime: bool = False) -> bool:
        """Re-verify against disk. ``trust_mtime`` skips rehashing when size+mtime agree."""
        p = Path(path or self.path)
        try:
            stat = p.stat()
        except FileNotFoundError:
            return False
        if stat.st_size != self.size_bytes:
            return False
        if trust_mtime and stat.st_mtime_ns == self.mtime_ns:
            return True
        return Fingerprint.of(p, scope=self.scope).sha256 == self.sha256

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Fingerprint:
        scope = _V38_SCOPE_ALIASES.get(data.get("scope") or data.get("hash_scope") or SCOPE_FULL,
                                       data.get("scope") or SCOPE_FULL)
        return cls(path=str(data.get("path") or data.get("name") or ""),
                   size_bytes=int(data["size_bytes"]), mtime_ns=int(data.get("mtime_ns") or 0),
                   sha256=str(data["sha256"]), scope=scope)
