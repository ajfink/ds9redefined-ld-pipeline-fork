"""Files from attempts that cannot be used again, removed once the episode's final output is approved.

Owner, 2026-11-25: "delete or overwrite mistake files that can't be used again once the final output is approved".
After the ``delivery`` review approves the deliverable, these go:

* a failed or cut-off attempt's file: ``*.previous-attempt.*`` (encode sets one aside, CP70), and any ``*.tmp.*``,
  ``*.tmp`` or ``*.partial*`` file a step was writing when it stopped;
* earlier revisions of the encode and of the deliverable in the output folder (``…-padfixed-video-r2.mkv``,
  ``…-r3.mkv``) that the manifest no longer names, and the audio a superseded remux encoded for them.

Never touched: anything the manifest names (the approved deliverable, its encode, masters, audio, scripts,
descriptors…), reports, the captures and the DVD. Only the episode's own working folders are searched, every path
is checked to sit inside one, and nothing is removed while a job of the episode is running.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId

LEFTOVER = re.compile(r"(\.previous-attempt\.|\.tmp\.|\.tmp$|\.partial(\.|$))", re.IGNORECASE)


@dataclass
class Leftover:
    path: Path
    why: str
    bytes: int


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _roots(profile, episode: EpisodeId) -> list[tuple[str, Path]]:
    from fstk.config.profile import MEDIA_ROOTS

    out, seen = [], set()
    for name in MEDIA_ROOTS:
        try:
            path = profile.root(name, episode)
        except FstkError:
            continue
        key = path.resolve(strict=False)
        if key not in seen and path.is_dir():
            seen.add(key)
            out.append((name, path))
    return out


def _named(profile, episode: EpisodeId, manifest) -> set[Path]:
    keep: set[Path] = set()
    for spec in (manifest.doc.get("artifacts") or {}).values():
        if not isinstance(spec, dict):
            continue
        try:
            p = Path(spec["path"]) if "path" in spec else profile.root(spec["root"], episode) / spec["name"]
        except (KeyError, FstkError):
            continue
        keep.add(p.resolve(strict=False))
    return keep


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
    except ValueError:
        return False
    return True


def find_leftovers(profile, episode: EpisodeId, manifest) -> list[Leftover]:
    keep = _named(profile, episode, manifest)
    found: dict[Path, Leftover] = {}
    roots = _roots(profile, episode)
    for _name, root in roots:
        for p in root.rglob("*"):
            if p.is_file() and LEFTOVER.search(p.name) and p.resolve(strict=False) not in keep:
                why = ("the file of a failed or stopped attempt" if "previous-attempt" in p.name.lower()
                       else "left unfinished by a step that stopped")
                found[p] = Leftover(p, why, p.stat().st_size)
    # earlier revisions of the encode / deliverable in the output folder
    out_root = next((r for n, r in roots if n == "out"), None)
    if out_root is not None and any(k.parent == out_root.resolve(strict=False) for k in keep):
        code = episode.code
        audio_root = next((r for n, r in roots if n == "audio"), None)
        for p in sorted(out_root.glob(f"{code}*.mkv")):
            if p in found or p.resolve(strict=False) in keep:
                continue
            found[p] = Leftover(p, "an earlier revision the approved output replaced", p.stat().st_size)
            enc = audio_root / "delivery" / p.stem if audio_root is not None else None
            if enc is not None and enc.is_dir() and enc.resolve(strict=False) not in keep:
                found[enc] = Leftover(enc, f"audio encoded for {p.name}", _size(enc))
    return sorted(found.values(), key=lambda x: str(x.path))


def remove_leftovers(profile, episode: EpisodeId, manifest) -> list[Leftover]:
    """Delete what ``find_leftovers`` lists. Refuses (deleting nothing) while a job of the episode runs."""
    from fstk.workspace.removal import running_jobs

    busy = running_jobs(profile, episode)
    if busy:
        raise FstkError(f"{episode} is being worked on ({'; '.join(busy)}); leftover files were not removed")
    items = find_leftovers(profile, episode, manifest)
    roots = [r for _n, r in _roots(profile, episode)]
    for it in items:
        if not any(_inside(it.path, r) for r in roots):
            raise FstkError(f"refusing to delete {it.path}: not inside the episode's working folders")
    for it in items:
        if it.path.is_dir():
            shutil.rmtree(it.path)
        elif it.path.exists():
            it.path.unlink()
    return items
