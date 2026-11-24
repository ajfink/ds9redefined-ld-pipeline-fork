"""Projected free-space gate with cross-process reservations (invariant 6).

Ported from v38 ``check_space_budget.py`` + ``space_ledger.py``. A stage that will write
large outputs states a *defensible estimate* of its peak additional bytes (never a prior
episode's file size used as a default). The gate passes only if

    free − (live reservations by other stages on that volume) − planned ≥ reserve

Reservations live in one JSON ledger per device, guarded by an OS file lock that is
released automatically if the holder dies; reservations whose owning process is gone (on
this host) are pruned on every access, so a crash never strands space.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fstk.core.errors import SpaceError
from fstk.core.fsio import atomic_write_json, pid_alive, process_start_time
from fstk.workspace.volumes import hint_for_space

GIB = 1024 ** 3
LEDGER_NAME = "space-reservations.json"
SCHEMA = "fstk-space-ledger-v1"
LOCK_TIMEOUT_S = 30.0


def volume_key(path: str | os.PathLike) -> str:
    """Stable identifier of the volume holding an existing directory."""
    resolved = Path(path).expanduser().resolve(strict=True)
    if os.name == "nt":
        return (resolved.drive or resolved.anchor).casefold()
    return str(os.stat(resolved).st_dev)


def nearest_existing_dir(path: str | os.PathLike) -> Path:
    p = Path(path).expanduser().absolute()
    while not p.is_dir():
        if p.parent == p:
            raise SpaceError(f"no existing directory above {path}")
        p = p.parent
    return p


@dataclass(frozen=True)
class SpaceCheck:
    ok: bool
    target: Path
    free_bytes: int
    others_bytes: int
    planned_bytes: int
    reserve_bytes: int
    #: Set by a re-check: free bytes at the previous measurement, and when this one was taken (local HH:MM:SS).
    was_free_bytes: int | None = None
    measured_at: str = ""

    @property
    def projected_bytes(self) -> int:
        return self.free_bytes - self.others_bytes - self.planned_bytes

    def change_note(self) -> str:
        """What a re-check found compared with the last measurement ("" for a first measurement)."""
        if self.was_free_bytes is None:
            return ""
        delta = self.free_bytes - self.was_free_bytes
        when = f"measured again at {self.measured_at}: " if self.measured_at else "measured again: "
        if abs(delta) < 16 * 2**20:
            return f"{when}free space is unchanged ({self.free_bytes / GIB:.1f} GiB, same as last time)"
        return f"{when}free space went from {self.was_free_bytes / GIB:.1f} to {self.free_bytes / GIB:.1f} GiB"

    def describe(self) -> str:
        text = (f"SPACE {'PASS' if self.ok else 'FAIL'} | target={self.target} | "
                f"free={self.free_bytes / GIB:.1f} GiB | reserved by others={self.others_bytes / GIB:.1f} GiB | "
                f"planned={self.planned_bytes / GIB:.1f} GiB | projected={self.projected_bytes / GIB:.1f} GiB | "
                f"reserve={self.reserve_bytes / GIB:.1f} GiB")
        note = self.change_note()
        return f"{text} | {note}" if note else text


def _planned_bytes(planned_gib: float) -> int:
    if not isinstance(planned_gib, (int, float)) or not math.isfinite(planned_gib) or planned_gib <= 0:
        raise SpaceError("planned space must be a finite, positive, measured estimate in GiB")
    return math.ceil(planned_gib * GIB)


class SpaceLedger:
    def __init__(self, path: str | os.PathLike, *, reserve_gib: float,
                 disk_usage=shutil.disk_usage):
        if not (reserve_gib > 0):
            raise SpaceError("min_free_gb reserve must be positive; it is never lowered automatically")
        self.path = Path(path)
        self.reserve_bytes = math.ceil(reserve_gib * GIB)
        self._disk_usage = disk_usage

    @classmethod
    def for_profile(cls, profile) -> SpaceLedger:
        """The ledger lives beside the machine profile (machine-local, never on a share).

        An older ledger under ``data_root`` is moved there once, reservations intact.
        """
        path = (profile.path.parent if profile.path else profile.data_root) / LEDGER_NAME
        old = profile.data_root / LEDGER_NAME
        if profile.path and old.is_file() and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, path)
        return cls(path, reserve_gib=profile.limits.min_free_gb)

    # ------------------------------------------------------------------ locking / IO
    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise SpaceError(f"space ledger is locked by another process: {lock_path}") from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        if self.path.is_symlink():
            raise SpaceError(f"space ledger may not be a symlink: {self.path}")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpaceError(f"space ledger is unreadable: {self.path}: {exc}") from None
        if data.get("schema") != SCHEMA or not isinstance(data.get("reservations"), list):
            raise SpaceError(f"space ledger has an unexpected format: {self.path}")
        host = socket.gethostname()
        return [r for r in data["reservations"] if isinstance(r, dict)
                and not (r.get("host") == host and not pid_alive(int(r.get("pid") or 0), r.get("ptime")))]

    def _write(self, rows: list[dict]) -> None:
        atomic_write_json(self.path, {"schema": SCHEMA, "reservations": rows})

    # ------------------------------------------------------------------ API
    def reservations(self) -> list[dict]:
        with self._locked():
            return self._read()

    def check(self, target_dir: str | os.PathLike, planned_gib: float, *,
              exclude_id: str | None = None) -> SpaceCheck:
        """Read-only check against free space minus other live reservations."""
        planned = _planned_bytes(planned_gib)
        target = nearest_existing_dir(target_dir)
        volume = volume_key(target)
        with self._locked():
            rows = self._read()
        others = sum(int(r.get("bytes") or 0) for r in rows
                     if r.get("volume") == volume and r.get("id") != exclude_id)
        free = self._disk_usage(target).free
        return SpaceCheck(free - others - planned >= self.reserve_bytes, target, free, others, planned,
                          self.reserve_bytes)

    def try_reserve(self, target_dir: str | os.PathLike, planned_gib: float, *, label: str,
                    owner_pid: int | None = None) -> tuple[str | None, SpaceCheck]:
        planned = _planned_bytes(planned_gib)
        target = nearest_existing_dir(target_dir)
        volume = volume_key(target)
        with self._locked():
            rows = self._read()
            others = sum(int(r.get("bytes") or 0) for r in rows if r.get("volume") == volume)
            free = self._disk_usage(target).free
            check = SpaceCheck(free - others - planned >= self.reserve_bytes, target, free, others,
                               planned, self.reserve_bytes)
            if not check.ok:
                self._write(rows)
                return None, check
            rid = uuid.uuid4().hex
            rows.append({"id": rid, "volume": volume, "bytes": planned, "pid": owner_pid or os.getpid(),
                         "host": socket.gethostname(), "label": label, "created_epoch": time.time(),
                         "ptime": process_start_time(owner_pid)})
            self._write(rows)
        return rid, check

    def release(self, reservation_id: str) -> None:
        with self._locked():
            rows = [r for r in self._read() if r.get("id") != reservation_id]
            self._write(rows)

    @contextmanager
    def reserve(self, target_dir: str | os.PathLike, planned_gib: float, *, label: str) -> Iterator[SpaceCheck]:
        """Hold a reservation for the duration of a write-heavy step, or raise SpaceError."""
        rid, check = self.try_reserve(target_dir, planned_gib, label=label)
        if rid is None:
            raise SpaceError(check.describe() + " — make space or move the work root; the reserve is "
                             "never lowered and nothing is deleted without the operator's authorization. "
                             + hint_for_space(check.reserve_bytes / GIB, target_dir))
        try:
            yield check
        finally:
            self.release(rid)
