"""Single manifest writer: lock → verify base → write atomically → append journal.

Replaces v38's per-write ``.bak-<label>-<stamp>`` copies (13 of them in about an hour on
S01E20) with one append-only ``journal.jsonl``. Each journal line records the base and new
content hashes and the field-level old/new values, so history is complete and reversible
without copying the whole manifest each time.

Integrity: ``verify()`` checks that the journal hashes chain and that the newest entry
matches the manifest on disk. A mismatch means someone edited the manifest outside the
store; that is reported, not repaired.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fstk.core.errors import ConcurrentModificationError, ManifestError
from fstk.core.fsio import atomic_write_bytes, canonical_json, exclusive_lock
from fstk.core.hashing import sha256_bytes
from fstk.core.timeutil import utc_now_iso

from .model import Manifest, diff

MANIFEST_NAME = "manifest.json"
JOURNAL_NAME = "journal.jsonl"
LOCK_NAME = ".manifest.lock"
JOURNAL_SCHEMA = "fstk-manifest-journal-v1"


@dataclass(frozen=True)
class Snapshot:
    manifest: Manifest
    raw: bytes

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.raw)


class ManifestStore:
    def __init__(self, directory: str | os.PathLike):
        self.directory = Path(directory)
        self.path = self.directory / MANIFEST_NAME
        self.journal_path = self.directory / JOURNAL_NAME
        self.lock_path = self.directory / LOCK_NAME

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> Snapshot:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raise ManifestError(f"no manifest at {self.path}") from None
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"{self.path} is not valid JSON: {exc}") from None
        return Snapshot(Manifest(doc), raw)

    def load(self) -> Manifest:
        return self.read().manifest

    # ------------------------------------------------------------------ writes
    def create(self, manifest: Manifest, *, by: str, reason: str, origin: str = "fstk") -> Snapshot:
        payload = canonical_json(manifest.doc)
        with exclusive_lock(self.lock_path, purpose="manifest-create"):
            if self.path.exists():
                raise ManifestError(f"refusing to overwrite existing manifest {self.path}")
            atomic_write_bytes(self.path, payload, exclusive=True)
            self._append({"op": "create", "by": by, "reason": reason, "origin": origin,
                          "base_sha256": None, "new_sha256": sha256_bytes(payload),
                          "changes": [{"op": "create", "key": manifest.episode.canonical}]})
        return Snapshot(Manifest(json.loads(payload)), payload)

    @contextmanager
    def transaction(self, *, by: str, reason: str, stage: str | None = None,
                    expect_sha256: str | None = None) -> Iterator[Manifest]:
        """Edit under lock. ``expect_sha256`` pins the version the caller decided from.

        A caller that read the manifest, showed evidence to a human, and now records the
        decision should pass the hash it read: if anything changed meanwhile the decision is
        refused rather than silently applied to different data.
        """
        if not by or not reason:
            raise ManifestError("manifest transactions need 'by' and 'reason'")
        with exclusive_lock(self.lock_path, purpose=f"manifest:{stage or 'edit'}"):
            snap = self.read()
            if expect_sha256 and snap.sha256 != expect_sha256:
                raise ConcurrentModificationError(
                    f"{self.path} changed since it was reviewed (expected {expect_sha256[:12]}, "
                    f"found {snap.sha256[:12]}); re-review against the current manifest")
            working = snap.manifest.copy()
            yield working
            Manifest(working.doc)  # re-validate whole document
            changes = diff(snap.manifest, working)
            if not changes:
                return
            payload = canonical_json(working.doc)
            atomic_write_bytes(self.path, payload, expected_original=snap.raw)
            self._append({"op": "update", "by": by, "reason": reason, "stage": stage,
                          "base_sha256": snap.sha256, "new_sha256": sha256_bytes(payload),
                          "changes": [{"op": c.op, "key": c.key, "old": c.old, "new": c.new}
                                      for c in changes]})

    def _append(self, entry: dict[str, Any]) -> None:
        entry = {"schema": JOURNAL_SCHEMA, "utc": utc_now_iso(), **entry}
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    # ------------------------------------------------------------------ history
    def journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        rows = []
        for number, line in enumerate(self.journal_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ManifestError(f"{self.journal_path}:{number} is corrupt: {exc}") from None
        return rows

    def verify(self) -> list[str]:
        """Return integrity problems (empty list means the history is consistent)."""
        problems: list[str] = []
        rows = self.journal()
        if not rows:
            return ["journal is empty or missing"]
        previous = None
        for index, row in enumerate(rows, 1):
            if index > 1 and row.get("base_sha256") != previous:
                problems.append(f"journal entry {index} does not chain from entry {index - 1}")
            previous = row.get("new_sha256")
        current = self.read().sha256
        if previous != current:
            problems.append("manifest on disk does not match the last journal entry "
                            "(edited outside the store?)")
        return problems
