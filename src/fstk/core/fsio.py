"""Atomic file writes, exclusive lock files, and canonical JSON.

Ported from v38 ``manifest_io.py`` (validated on Windows, where ``os.replace`` fails with a
sharing violation while any reader holds the destination open: the GUI polls state files
while the runner replaces them). Extended with:

* lock files that record pid/host/time and can be inspected for staleness (never broken
  automatically: the operator decides, because a live job on another machine sharing the
  folder looks identical to a dead one);
* canonical JSON (stable key order, LF, trailing newline) so content hashes are reproducible.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import FstkError
from .timeutil import utc_now_iso

REPLACE_ATTEMPTS = 60
REPLACE_RETRY_SECONDS = 0.25


def replace_with_retry(temp: Path, path: Path, *, attempts: int = REPLACE_ATTEMPTS,
                       delay: float = REPLACE_RETRY_SECONDS) -> None:
    """``os.replace`` that tolerates transient Windows sharing violations."""
    last: OSError | None = None
    for _ in range(attempts):
        try:
            os.replace(temp, path)
            return
        except PermissionError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM):
                raise
            last = exc
            time.sleep(delay)
    raise last if last is not None else OSError(f"replace failed: {path}")


def _temp_beside(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")


def atomic_write_bytes(path: str | os.PathLike, payload: bytes, *,
                       expected_original: bytes | None = None, exclusive: bool = False) -> None:
    """Write ``payload`` to ``path`` so readers see either the old or the new file.

    ``expected_original`` guards against lost updates: the write is refused if the file on
    disk differs from what the caller read. ``exclusive`` refuses to replace an existing file.
    """
    path = Path(path)
    if path.is_symlink():
        raise FstkError(f"refusing to write through a symlink: {path}")
    if exclusive and path.exists():
        raise FileExistsError(path)
    if expected_original is not None and path.read_bytes() != expected_original:
        raise FstkError(f"{path} changed on disk since it was read")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_beside(path)
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_original is not None and path.read_bytes() != expected_original:
            raise FstkError(f"{path} changed on disk while the new content was staged")
        if exclusive:
            # os.link is atomic create-if-absent on every supported filesystem.
            try:
                os.link(temp, path)
            except FileExistsError:
                raise
            except OSError:
                if path.exists():
                    raise FileExistsError(path) from None
                replace_with_retry(temp, path)
        else:
            replace_with_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def canonical_json(data: Any) -> bytes:
    """Deterministic JSON bytes (sorted keys, 2-space indent, LF, trailing newline)."""
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")


def atomic_write_json(path: str | os.PathLike, data: Any, **kw) -> bytes:
    payload = canonical_json(data)
    atomic_write_bytes(path, payload, **kw)
    return payload


def read_json(path: str | os.PathLike) -> Any:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise FstkError(f"file not found: {p}") from None
    try:
        return json.loads(decode_text(raw))
    except json.JSONDecodeError as exc:
        raise FstkError(f"{p} is not valid JSON: {exc}") from None


def decode_text(raw: bytes) -> str:
    """Decode tool/log output honouring a BOM.

    PowerShell ``Tee-Object`` writes UTF-16LE; an empty UTF-8 regex match over such a file
    is not proof that a program emitted nothing (PORTING §5).
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) >= 4 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00" and raw[0:1] != b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8", errors="strict")


# ---------------------------------------------------------------------------------------
# Lock files
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LockInfo:
    path: Path
    pid: int | None
    host: str | None
    started_utc: str | None
    purpose: str | None

    @property
    def local_process_alive(self) -> bool | None:
        """``True``/``False`` for a lock owned by this host; ``None`` when unknowable."""
        if self.pid is None or self.host != socket.gethostname():
            return None
        return pid_alive(self.pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockHeldError(FstkError):
    def __init__(self, info: LockInfo):
        alive = info.local_process_alive
        state = {True: "owner is running", False: "owner process is gone (stale)",
                 None: "owner state unknown (other host or unreadable)"}[alive]
        super().__init__(f"lock held: {info.path} (pid={info.pid} host={info.host} "
                         f"since={info.started_utc} purpose={info.purpose}; {state})")
        self.info = info


def read_lock(path: str | os.PathLike) -> LockInfo | None:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    fields: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
    pid = int(fields["pid"]) if fields.get("pid", "").isdigit() else None
    return LockInfo(p, pid, fields.get("host"), fields.get("started_utc"), fields.get("purpose"))


@contextmanager
def exclusive_lock(path: str | os.PathLike, *, purpose: str = ""):
    """Hold an exclusive lock file for the duration of the block.

    Creation uses ``O_EXCL``. An existing lock raises ``LockHeldError`` carrying the owner
    details; it is never broken silently.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    purpose_token = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in purpose)
    try:
        stream = p.open("x", encoding="utf-8", newline="\n")
    except FileExistsError:
        info = read_lock(p) or LockInfo(p, None, None, None, None)
        raise LockHeldError(info) from None
    try:
        stream.write(f"pid={os.getpid()} host={socket.gethostname()} started_utc={utc_now_iso()} "
                     f"purpose={purpose_token}\n")
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        yield p
    finally:
        if not stream.closed:
            stream.close()
        p.unlink(missing_ok=True)
