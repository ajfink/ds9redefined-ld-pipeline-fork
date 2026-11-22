"""Episode manifest document.

Shape (schema ``fstk-episode-manifest-v1``)::

    {
      "schema": "...", "identity": "DS9:S01E20", "title": "...",
      "sources":   {"archive_item": ..., "archive_item_verified": ..., "downloads_override": ...,
                    "captures": [{"label": "L1", "files": {"tbc": {"name", "bytes", "md5"}, ...}}],
                    "provenance": {...}},
      "values":    {"video.head_trim": {"value": 12, "by": "...", "stage": "...", "utc": "...",
                                         "evidence": [...], "note": "...", "origin": "..."}},
      "artifacts": {"stack_master": {"root": "prores", "name": "S01E20-stack-master.mkv"}},
      "legacy":    {...}   # unmapped v38 content, preserved verbatim, never read by stages
    }

Values are keyed by declared field (``fstk.manifest.registry``). Each carries who/what set
it, when, and the evidence fingerprints it was derived from, so downstream approvals can be
invalidated when upstream evidence changes.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fstk.core.errors import ManifestError
from fstk.core.identity import EpisodeId
from fstk.core.timeutil import utc_now_iso

from .registry import field_def

SCHEMA = "fstk-episode-manifest-v1"
_UNSET = object()
ARTIFACT_KEYS_RE = r"^[a-z][a-z0-9_]{0,63}$"


@dataclass(frozen=True)
class Change:
    op: str            # "set" | "unset" | "artifact" | "artifact-unset" | "sources" | "title"
    key: str
    old: Any
    new: Any


class Manifest:
    def __init__(self, doc: dict[str, Any]):
        if doc.get("schema") != SCHEMA:
            raise ManifestError(f"unsupported manifest schema {doc.get('schema')!r} (expected {SCHEMA})")
        self.doc = doc
        self.episode = EpisodeId.parse(doc["identity"])
        for key, entry in doc.get("values", {}).items():
            field_def(key).validate(entry.get("value"))

    # ------------------------------------------------------------------ creation
    @classmethod
    def new(cls, episode: EpisodeId, *, title: str = "") -> Manifest:
        return cls({"schema": SCHEMA, "identity": episode.canonical, "title": title,
                    "sources": {"captures": []}, "values": {}, "artifacts": {}, "legacy": {}})

    def copy(self) -> Manifest:
        return Manifest(copy.deepcopy(self.doc))

    # ------------------------------------------------------------------ values
    @property
    def title(self) -> str:
        return self.doc.get("title") or ""

    @title.setter
    def title(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ManifestError("title must be a non-empty string")
        self.doc["title"] = value.strip()

    def is_set(self, key: str) -> bool:
        field_def(key)
        return key in self.doc["values"]

    def get(self, key: str, default: Any = _UNSET) -> Any:
        field_def(key)
        entry = self.doc["values"].get(key)
        if entry is None:
            if default is _UNSET:
                return None
            return default
        return copy.deepcopy(entry["value"])

    def require(self, key: str) -> Any:
        """Return a value or fail loudly: unset means unmeasured, never a default."""
        if not self.is_set(key):
            fd = field_def(key)
            verb = {"measurement": "measure and record it", "choice": "the operator must decide it",
                    "setting": "configure it", "record": "complete that review"}[fd.kind]
            raise ManifestError(f"{self.episode}: {key} is not set ({fd.description}); {verb}. "
                                "Never guess or copy another episode's value.")
        return self.get(key)

    def entry(self, key: str) -> dict[str, Any] | None:
        field_def(key)
        entry = self.doc["values"].get(key)
        return copy.deepcopy(entry) if entry else None

    def set(self, key: str, value: Any, *, by: str, stage: str | None = None,
            evidence: Iterable[dict] = (), note: str | None = None, origin: str = "fstk") -> Change:
        fd = field_def(key)
        fd.validate(value)
        if not by:
            raise ManifestError(f"{key}: every value needs an actor ('by')")
        if fd.kind in ("choice", "record") and by.startswith("stage:") and not note:
            # Operator decisions recorded by automation must say why they are not a human call.
            raise ManifestError(f"{key} is a {fd.kind}; automated writes must carry a note")
        old = self.get(key)
        entry = {"value": copy.deepcopy(value), "by": by, "utc": utc_now_iso(), "origin": origin}
        if stage:
            entry["stage"] = stage
        ev = [dict(e) for e in evidence]
        if ev:
            entry["evidence"] = ev
        if note:
            entry["note"] = note
        self.doc["values"][key] = entry
        return Change("set", key, old, copy.deepcopy(value))

    def unset(self, key: str, *, by: str, reason: str) -> Change | None:
        field_def(key)
        if not reason:
            raise ManifestError(f"{key}: unsetting a value requires a reason")
        if key not in self.doc["values"]:
            return None
        old = self.doc["values"].pop(key)["value"]
        return Change("unset", key, old, None)

    def set_keys(self) -> list[str]:
        return sorted(self.doc["values"])

    # ------------------------------------------------------------------ artifacts
    def artifact(self, key: str) -> dict[str, str] | None:
        spec = self.doc["artifacts"].get(key)
        return dict(spec) if spec else None

    def set_artifact(self, key: str, *, root: str | None = None, name: str | None = None,
                     path: str | None = None) -> Change:
        import re

        if not re.match(ARTIFACT_KEYS_RE, key):
            raise ManifestError(f"artifact key {key!r} must match {ARTIFACT_KEYS_RE}")
        if path is not None and (root or name):
            raise ManifestError(f"artifact {key}: give either root+name or path")
        if path is None and not (root and name):
            raise ManifestError(f"artifact {key}: root and name are both required")
        if name and (name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")):
            raise ManifestError(f"artifact {key}: name must be relative and stay inside its root")
        spec = {"path": path} if path is not None else {"root": root, "name": name}
        old = self.artifact(key)
        self.doc["artifacts"][key] = spec
        return Change("artifact", key, old, dict(spec))

    # ------------------------------------------------------------------ sources
    @property
    def captures(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.doc["sources"].get("captures") or [])

    def capture(self, label: str) -> dict[str, Any]:
        for cap in self.doc["sources"].get("captures") or []:
            if cap["label"] == label:
                return copy.deepcopy(cap)
        raise ManifestError(f"{self.episode}: no capture labelled {label!r}")

    @property
    def capture_labels(self) -> list[str]:
        return [c["label"] for c in self.doc["sources"].get("captures") or []]

    def set_sources(self, sources: dict[str, Any]) -> Change:
        labels = [c.get("label") for c in sources.get("captures") or []]
        if len(set(labels)) != len(labels) or not all(isinstance(x, str) and x for x in labels):
            raise ManifestError("capture labels must be unique non-empty strings")
        old = copy.deepcopy(self.doc["sources"])
        self.doc["sources"] = copy.deepcopy(sources)
        return Change("sources", "sources", old, copy.deepcopy(sources))


def diff(before: Manifest, after: Manifest) -> list[Change]:
    """Field-level differences between two manifests (used for the journal)."""
    changes: list[Change] = []
    if before.doc.get("title") != after.doc.get("title"):
        changes.append(Change("title", "title", before.doc.get("title"), after.doc.get("title")))
    bv, av = before.doc["values"], after.doc["values"]
    for key in sorted(set(bv) | set(av)):
        if key not in av:
            changes.append(Change("unset", key, bv[key]["value"], None))
        elif bv.get(key) != av[key]:
            changes.append(Change("set", key, bv[key]["value"] if key in bv else None, av[key]["value"]))
    ba, aa = before.doc["artifacts"], after.doc["artifacts"]
    for key in sorted(set(ba) | set(aa)):
        if ba.get(key) != aa.get(key):
            changes.append(Change("artifact" if key in aa else "artifact-unset", key, ba.get(key), aa.get(key)))
    if before.doc["sources"] != after.doc["sources"]:
        changes.append(Change("sources", "sources", None, None))
    if before.doc.get("legacy") != after.doc.get("legacy"):
        changes.append(Change("legacy", "legacy", None, None))
    return changes
