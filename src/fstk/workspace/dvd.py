"""An episode's DVD reference: find candidate files, record the choice (or "no DVD") in the manifest.

Shared by the ``fstk episode dvd`` command and the DVD dialog in the app, so both record exactly the same thing.
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import FstkError

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".vob", ".iso"})
_SKIP = 5000                  # a runaway search of a huge share stops listing here


def find_dvd(dvd_root: Path, code: str, *, any_video: bool = False) -> list[Path]:
    """Video files under ``dvd_root`` whose name carries the episode code (S01E20), case-insensitive.

    ``any_video`` lists every video file instead (to pick one whose name does not carry the code).
    """
    pat = code.casefold().replace(" ", "")
    try:
        if not dvd_root.is_dir():
            return []
        hits: list[Path] = []
        for p in dvd_root.rglob("*"):
            if p.suffix.lower() in VIDEO_EXTENSIONS and p.is_file() \
                    and (any_video or pat in p.name.casefold().replace(" ", "")):
                hits.append(p)
                if len(hits) >= _SKIP:
                    break
    except OSError:
        return []
    return sorted(hits)


def dvd_status(manifest) -> dict:
    """What is recorded: ``state`` is "set" (a file), "unfiled" (marked available, e.g. by a v38 import, but no
    file recorded), "none" (the owner said there is no DVD) or "unset"."""
    available = manifest.get("mode.dvd_reference_available")
    art = manifest.artifact("dvd") if available else None
    path = (art or {}).get("path") if isinstance(art, dict) else None
    if available is True:
        return {"state": "set", "path": path} if path else {"state": "unfiled", "path": ""}
    return {"state": "none" if available is False else "unset", "path": ""}


def record_dvd(store, actor: str, file: str | Path | None) -> str | None:
    """Record the DVD file, or ``None`` for "this episode has no DVD (LD-only route)". Returns the path or None."""
    if file is None:
        with store.transaction(by=actor, reason="operator: no DVD reference for this episode") as m:
            m.set("mode.dvd_reference_available", False, by=actor, note="operator: no DVD")
        return None
    dvd = Path(file).absolute()
    if not dvd.is_file():
        raise FstkError(f"DVD file not found: {dvd}")
    with store.transaction(by=actor, reason=f"operator: DVD reference {dvd.name}") as m:
        m.set_artifact("dvd", path=str(dvd))
        m.set("mode.dvd_reference_available", True, by=actor, note=f"operator: {dvd}")
    return str(dvd)
