"""Create episode workspaces from a catalog selection or a local capture folder.

A workspace is a manifest (plus its journal) under ``data_root/episodes/<SERIES>/<CODE>``. Creation
records *where the captures come from*; nothing is downloaded or measured here. Titles taken from
capture filenames are recorded with their source and stay hints until confirmed.
"""
from __future__ import annotations

from pathlib import Path

from fstk.catalog.index import CatalogEntry
from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.manifest.store import ManifestStore
from fstk.workspace.local_source import inspect_local_source


def store_for(profile, episode: EpisodeId) -> ManifestStore:
    return ManifestStore(profile.episode_state_dir(episode) / "manifest")


#: an episode whose project is kept but whose entry was removed from the app's list (Remove episode…)
HIDDEN_MARKER = "removed-from-list.json"


def is_hidden(profile, episode: EpisodeId) -> bool:
    return (profile.episode_state_dir(episode) / HIDDEN_MARKER).is_file()


def hide(profile, episode: EpisodeId, *, actor: str) -> Path:
    from fstk.core.fsio import atomic_write_json
    from fstk.core.timeutil import utc_now_iso

    path = profile.episode_state_dir(episode) / HIDDEN_MARKER
    atomic_write_json(path, {"schema": "fstk-removed-from-list-v1", "by": actor, "utc": utc_now_iso(),
                             "note": "the project is kept; adding the episode again brings it back"})
    return path


def _reopen_hidden(profile, store: ManifestStore, ep: EpisodeId) -> bool:
    """Adding an episode whose entry was only removed from the list brings the kept project back."""
    marker = profile.episode_state_dir(ep) / HIDDEN_MARKER
    if store.exists() and marker.is_file():
        marker.unlink()
        return True
    return False


def _shared_guard(profile, episode: EpisodeId, allow_parallel: bool) -> None:
    """In shared mode, refuse an episode a peer is actively working on (ADR 0002 §5).
    An unreachable share never blocks local work."""
    if not profile.shared_root:
        return
    from fstk.workspace.shared import SharedError, SharedTree

    try:
        tree = SharedTree(profile)
    except SharedError:
        return
    tree.check_new_episode(episode, allow_parallel=allow_parallel)


def _publish(profile, episode: EpisodeId) -> None:
    if profile.shared_root:
        from fstk.workspace.shared import try_publish

        try_publish(profile, episode)


def create_from_catalog(profile, entry: CatalogEntry, *, actor: str, catalog_summary: dict | None = None,
                        allow_parallel: bool = False) -> ManifestStore:
    if not entry.selectable:
        raise FstkError(f"{entry.series}:{entry.code} cannot be started from the catalog: {entry.reason}")
    ep = entry.episode_id
    store = store_for(profile, ep)
    if _reopen_hidden(profile, store, ep):
        return store
    if store.exists():
        raise FstkError(f"{ep} already has a workspace ({store.path})")
    _shared_guard(profile, ep, allow_parallel)
    m = Manifest.new(ep, title=entry.title)
    m.set_sources({"captures": [], "archive_item": entry.identifier, "archive_url": entry.url,
                   "title_source": "catalog capture filenames (unconfirmed)",
                   "catalog": {"identifier": entry.identifier, "capture_labels": list(entry.capture_labels),
                               "raw_efm_count": entry.raw_efm_count, "selected_bytes": entry.selected_bytes,
                               "warnings": list(entry.warnings),
                               "snapshot_generated_at_utc": (catalog_summary or {}).get("generated_at_utc")}})
    store.create(m, by=actor, reason=f"created from catalog item {entry.identifier}")
    _publish(profile, ep)
    return store


def create_from_local(profile, folder: str | Path, *, actor: str, allow_parallel: bool = False) -> ManifestStore:
    scan = inspect_local_source(Path(folder))
    ep = EpisodeId.parse(f"{scan['series']}:{scan['episode']}")
    store = store_for(profile, ep)
    if _reopen_hidden(profile, store, ep):
        return store
    if store.exists():
        raise FstkError(f"{ep} already has a workspace ({store.path})")
    _shared_guard(profile, ep, allow_parallel)
    tbc = next((c["tbc_file"] for c in scan["captures"] if c.get("tbc_file")), "")
    stem = Path(tbc).stem
    prefix = f"{ep.series}-{ep.code}-"
    title = stem[len(prefix):].rsplit("-", 1)[0].strip(" -_") if stem.upper().startswith(prefix.upper()) else ""
    m = Manifest.new(ep, title=title)
    sources = {"captures": [], "downloads_override": scan["source_dir"],
               "title_source": "local capture filenames (unconfirmed)" if title else "none"}
    if scan.get("archive_id_hint"):
        sources["archive_item"] = scan["archive_id_hint"]
    m.set_sources(sources)
    store.create(m, by=actor, reason=f"created from local folder {scan['source_dir']}")
    _publish(profile, ep)
    return store
