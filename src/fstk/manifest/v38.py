"""Lossless conversion between v38 episode manifests and the new manifest schema.

``import_v38`` maps every declared field; anything it does not recognise is preserved
verbatim under ``legacy.v38`` (dotted paths), so ``export_v38(import_v38(doc))`` is
equivalent to ``doc``. Equivalence treats v38's null/empty placeholders (``null``, ``[]``,
``""``, ``{}`` where the template uses them for "unknown") as unset.

Imported values are tagged ``origin: "v38"``. Records (capture selection, trim/cadence
reviews) are carried as historical: the new approval model requires them to be confirmed
again before they can gate a stage (REFACTOR-PLAN §7.4).
"""
from __future__ import annotations

import copy
import re
from typing import Any

from fstk.core.errors import ManifestError
from fstk.core.identity import EpisodeId

from .model import Manifest
from .registry import BY_V38, FIELDS

V38_SECTIONS = ("stack", "audio", "video", "pads", "retime", "qc")
IMPORT_ACTOR = "import:v38"
_CAPTURE_FILE_KEY = re.compile(r"^(?P<kind>[a-z0-9]+)_(?P<part>file|bytes|md5)$")
_IDENTITY_KEYS = ("episode", "series", "identity", "title")
_SOURCE_KEYS = ("archive_item", "archive_item_verified", "source_provenance")


def _empty_placeholder(fd, value: Any) -> bool:
    if value is None or (fd.v38_unset is not None and value == fd.v38_unset):
        return True
    return fd.type == "str" and isinstance(value, str) and not value.strip()


def _get_path(doc: dict, dotted: str) -> tuple[bool, Any]:
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _convert_capture(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw.get("label"):
        raise ManifestError(f"v38 capture without a label: {raw!r}")
    cap: dict[str, Any] = {"label": str(raw["label"]), "files": {}}
    extra: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "label":
            continue
        m = _CAPTURE_FILE_KEY.match(key)
        if not m:
            extra[key] = copy.deepcopy(value)
            continue
        slot = cap["files"].setdefault(m["kind"], {})
        part = {"file": "name", "bytes": "bytes", "md5": "md5"}[m["part"]]
        if part == "md5" and value == "":
            slot.setdefault("_v38_md5_blank", True)
            continue
        slot[part] = value
    if extra:
        cap["extra"] = extra
    return cap


def _export_capture(cap: dict[str, Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {"label": cap["label"]}
    for kind, slot in cap.get("files", {}).items():
        if "name" in slot:
            raw[f"{kind}_file"] = slot["name"]
        if "bytes" in slot:
            raw[f"{kind}_bytes"] = slot["bytes"]
        if "md5" in slot:
            raw[f"{kind}_md5"] = slot["md5"]
        elif slot.get("_v38_md5_blank"):
            raw[f"{kind}_md5"] = ""
    raw.update(copy.deepcopy(cap.get("extra") or {}))
    return raw


def import_v38(doc: dict[str, Any], *, source_sha256: str | None = None) -> Manifest:
    if not isinstance(doc, dict):
        raise ManifestError("v38 manifest must be a JSON object")
    episode = EpisodeId.parse(str(doc.get("episode") or ""), str(doc.get("series") or "DS9"))
    if doc.get("identity") not in (None, episode.canonical):
        raise ManifestError(f"v38 identity {doc.get('identity')!r} disagrees with {episode}")
    manifest = Manifest.new(episode, title=str(doc.get("title") or ""))
    consumed: set[str] = set(_IDENTITY_KEYS) | set(_SOURCE_KEYS) | {"captures", "files", "roots"}

    sources: dict[str, Any] = {"captures": [_convert_capture(c) for c in doc.get("captures") or []]}
    for key in _SOURCE_KEYS:
        if key in doc:
            sources[key] = copy.deepcopy(doc[key])
    roots = doc.get("roots")
    legacy: dict[str, Any] = {}
    if isinstance(roots, dict):
        if roots.get("downloads"):
            sources["downloads_override"] = roots["downloads"]
        for key, value in roots.items():
            if key != "downloads":
                legacy[f"roots.{key}"] = copy.deepcopy(value)
    elif roots is not None:
        legacy["roots"] = copy.deepcopy(roots)
    manifest.set_sources(sources)

    evidence = [{"kind": "v38-manifest", "sha256": source_sha256}] if source_sha256 else []
    for fd in FIELDS:
        if not fd.v38:
            continue
        present, value = _get_path(doc, fd.v38)
        if not present:
            continue
        consumed.add(fd.v38)
        if _empty_placeholder(fd, value):
            continue
        manifest.set(fd.key, copy.deepcopy(value), by=IMPORT_ACTOR, origin="v38", evidence=evidence)

    files = doc.get("files") or {}
    if not isinstance(files, dict):
        raise ManifestError("v38 files must be an object")
    for key, spec in files.items():
        if isinstance(spec, str):
            manifest.set_artifact(key, path=spec)
        elif isinstance(spec, dict) and "root" in spec and "name" in spec and len(spec) == 2:
            manifest.set_artifact(key, root=spec["root"], name=spec["name"])
        elif isinstance(spec, dict) and "path" in spec and len(spec) == 1:
            manifest.set_artifact(key, path=spec["path"])
        else:
            legacy[f"files.{key}"] = copy.deepcopy(spec)

    for key, value in doc.items():
        if key in consumed:
            continue
        if key in V38_SECTIONS and isinstance(value, dict):
            for sub, subvalue in value.items():
                if f"{key}.{sub}" not in consumed:
                    legacy[f"{key}.{sub}"] = copy.deepcopy(subvalue)
            legacy.setdefault(f"__section__.{key}", True)
        else:
            legacy[key] = copy.deepcopy(value)
    manifest.doc["legacy"] = {"v38": legacy} if legacy else {}
    if source_sha256:
        manifest.doc["legacy"]["v38_source_sha256"] = source_sha256
    Manifest(manifest.doc)
    return manifest


def export_v38(manifest: Manifest) -> dict[str, Any]:
    """Rebuild a v38-shaped manifest (for the shared tree / v38 tools during transition)."""
    ep = manifest.episode
    doc: dict[str, Any] = {"episode": ep.code, "series": ep.series, "identity": ep.canonical,
                           "title": manifest.title}
    src = manifest.doc["sources"]
    for key in _SOURCE_KEYS:
        if key in src:
            doc[key] = copy.deepcopy(src[key])
    doc["captures"] = [_export_capture(c) for c in src.get("captures") or []]
    if src.get("downloads_override"):
        doc.setdefault("roots", {})["downloads"] = src["downloads_override"]
    for section in V38_SECTIONS:
        doc[section] = {}
    for fd in FIELDS:
        if not fd.v38:
            continue
        value = manifest.get(fd.key) if manifest.is_set(fd.key) else copy.deepcopy(fd.v38_unset)
        node = doc
        parts = fd.v38.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    original = (manifest.doc.get("legacy") or {}).get("v38_files_original") or {}
    doc["files"] = {k: copy.deepcopy(original.get(k, v)) for k, v in manifest.doc["artifacts"].items()}
    legacy = (manifest.doc.get("legacy") or {}).get("v38") or {}
    for dotted, value in legacy.items():
        if dotted.startswith("__section__."):
            continue
        if "." in dotted and dotted.split(".", 1)[0] in (*V38_SECTIONS, "roots", "files"):
            head, tail = dotted.split(".", 1)
            doc.setdefault(head, {})[tail] = copy.deepcopy(value)
        else:
            doc[dotted] = copy.deepcopy(value)
    return doc


def _normalise(doc: dict[str, Any]) -> dict[str, Any]:
    """Canonical v38 form for equivalence: placeholders and missing declared keys → None."""
    out = copy.deepcopy(doc)
    for fd in FIELDS:
        if not fd.v38:
            continue
        present, value = _get_path(out, fd.v38)
        parts = fd.v38.split(".")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None if (not present or _empty_placeholder(fd, value)) else value
    for section in V38_SECTIONS:
        out.setdefault(section, {})
    out.setdefault("files", {})
    out["captures"] = [{k: v for k, v in c.items() if not (k.endswith("_md5") and v == "")}
                       for c in out.get("captures") or []]
    out.setdefault("series", "DS9")
    out.setdefault("identity", f"{out['series']}:{out.get('episode')}")
    return out


def v38_equivalent(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _normalise(a) == _normalise(b)


__all__ = ["BY_V38", "export_v38", "import_v38", "v38_equivalent"]


def v38_machine_roots(profile_doc: dict[str, Any]) -> dict[str, str]:
    """Root name → folder from a v38 machine profile (``machine.json``: ``roots`` plus ``dvd_root``)."""
    roots = dict(profile_doc.get("roots") or {})
    return {k: str(v) for k, v in roots.items() if isinstance(v, str) and v and k != "downloads_template"}


def resolve_v38_artifacts(manifest: Manifest, roots: dict[str, str]) -> dict[str, list[str]]:
    """Make an imported episode continuable on the machine that ran v38.

    v38's media folders are flat (``probe/<file>``) or per episode (``audio/S01E20/<file>``) and
    can't be expressed as fstk root overrides, so every ``{root, name}`` artifact whose root the v38
    profile names becomes an absolute ``{path}``. The original references are kept under
    ``legacy.v38_files_original`` and ``export_v38`` writes them back, so the v38 view of the
    manifest is unchanged. Returns ``{"resolved": [...], "unresolved": [...]}``.
    """
    original: dict[str, Any] = {}
    out = {"resolved": [], "unresolved": []}
    for key, spec in list(manifest.doc["artifacts"].items()):
        if not (isinstance(spec, dict) and "root" in spec and "name" in spec):
            continue
        base = roots.get(spec["root"]) or roots.get(f"{spec['root']}_root")
        if not base:
            out["unresolved"].append(key)
            continue
        original[key] = copy.deepcopy(spec)
        sep = "\\" if ("\\" in base or ":" in base[:3]) else "/"
        name = spec["name"].replace("/", sep) if sep == "\\" else spec["name"]
        manifest.doc["artifacts"][key] = {"path": base.rstrip("\\/") + sep + name}
        out["resolved"].append(key)
    if original:
        manifest.doc.setdefault("legacy", {})["v38_files_original"] = original
    return out
