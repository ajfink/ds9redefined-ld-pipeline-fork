"""Human review decisions bound to the exact evidence they were made on (invariant 4).

An approval record is an immutable JSON file (exclusive create, never edited) holding:

* ``decision``  – ``APPROVED`` or ``REJECTED`` (nothing else);
* ``note``      – required human explanation;
* ``bindings``  – fingerprints of every evidence file the reviewer saw, and the manifest
                  values the decision depends on.

``status()`` re-verifies all bindings. If any evidence file changed or any bound manifest
value differs, the approval is **stale** and no longer satisfies a gate; the stage must
regenerate evidence and a human must decide again. Records imported from v38 carry
``origin: "v38"`` and are always treated as historical (never current) — REFACTOR-PLAN §7.4.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fstk.core.errors import FstkError
from fstk.core.fsio import atomic_write_json
from fstk.core.hashing import Fingerprint
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.manifest.model import Manifest

SCHEMA = "fstk-approval-v1"
DECISIONS = ("APPROVED", "REJECTED")


@dataclass(frozen=True)
class ApprovalStatus:
    state: str              # "approved" | "rejected" | "stale" | "missing" | "historical"
    record: dict | None
    reasons: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.state == "approved"


class ApprovalBook:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def record(self, *, review: str, decision: str, note: str, actor: str,
               evidence: list[Path | Fingerprint], manifest: Manifest | None = None,
               bound_keys: list[str] | tuple[str, ...] = (), extra: dict[str, Any] | None = None,
               origin: str = "fstk") -> Path:
        if decision not in DECISIONS:
            raise FstkError(f"decision must be one of {DECISIONS}")
        if not note or not note.strip():
            raise FstkError("a review decision requires a human note")
        if not actor:
            raise FstkError("a review decision requires an actor")
        if not evidence and origin == "fstk":
            raise FstkError("an approval must bind at least one evidence file")
        fps = [e.to_dict() if isinstance(e, Fingerprint) else Fingerprint.of(e).to_dict() for e in evidence]
        values: dict[str, Any] = {}
        if bound_keys:
            if manifest is None:
                raise FstkError("bound manifest keys need the manifest")
            values = {k: manifest.get(k) for k in bound_keys}
        doc = {"schema": SCHEMA, "review": review, "decision": decision, "note": note.strip(),
               "actor": actor, "utc": utc_now_iso(), "origin": origin,
               "bindings": {"evidence": fps, "manifest_values": values}, "extra": extra or {}}
        path = self.directory / f"{review}-{utc_stamp()}.json"
        atomic_write_json(path, doc, exclusive=True)
        return path

    def history(self, review: str) -> list[dict]:
        if not self.directory.is_dir():
            return []
        rows = []
        for p in sorted(self.directory.glob(f"{review}-*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FstkError(f"unreadable approval record {p}: {exc}") from None
            if doc.get("schema") == SCHEMA and doc.get("review") == review:
                doc["_path"] = str(p)
                rows.append(doc)
        return rows

    def status(self, review: str, *, manifest: Manifest | None = None,
               expected_evidence: list[Path] | None = None) -> ApprovalStatus:
        rows = self.history(review)
        if not rows:
            return ApprovalStatus("missing", None, ("no decision recorded",))
        latest = rows[-1]
        if latest.get("origin") == "v38":
            return ApprovalStatus("historical", latest, ("imported from v38; re-confirm in fstk",))
        if latest["decision"] == "REJECTED":
            return ApprovalStatus("rejected", latest, (latest.get("note", ""),))
        reasons: list[str] = []
        bound = {fp["path"]: fp for fp in latest["bindings"]["evidence"]}
        for fp in bound.values():
            if not Fingerprint.from_dict(fp).matches():
                reasons.append(f"evidence changed since review: {fp['path']}")
        if expected_evidence is not None:
            missing = [str(p) for p in expected_evidence if str(p) not in bound]
            if missing:
                reasons.append(f"approval does not cover current evidence: {missing}")
        for key, value in latest["bindings"]["manifest_values"].items():
            if manifest is None:
                reasons.append("manifest not supplied to verify bound values")
                break
            if manifest.get(key) != value:
                reasons.append(f"{key} changed since review ({value!r} → {manifest.get(key)!r})")
        if reasons:
            return ApprovalStatus("stale", latest, tuple(reasons))
        return ApprovalStatus("approved", latest)
