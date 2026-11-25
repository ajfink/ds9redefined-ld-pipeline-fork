"""Removing an episode from this machine: the project record, its working files, and (optionally) the download.

Parts, chosen independently (owner, 2026-11-25):

* ``entry``     remove the episode from the app's list only; the project and every file are kept, and adding the
                episode again brings it back;
* ``project``   delete the project (manifest, jobs, approvals, logs); the entry goes with it;
* ``files``     delete the episode's working media (master, audio, scripts, encodes, finished video);
* ``captures``  delete the downloaded capture folder, **but only when fstk downloaded it** into the profile's
                downloads folder. Captures you pointed fstk at ("From local folder…") and the DVD are never deleted.

The older scopes still work: ``project`` → {project}, ``files`` → {project, files}, ``all`` → every part.

Every path is checked to sit inside the folder it is supposed to belong to before anything is removed.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId

SCOPES = ("project", "files", "all")
PARTS = ("entry", "project", "files", "captures")
_SCOPE_PARTS = {"project": {"project"}, "files": {"project", "files"}, "all": set(PARTS)}


def parts_of(scope: str | set[str] | frozenset[str]) -> frozenset[str]:
    """A scope name (older callers) or a set of parts → the parts. The project's deletion takes its entry along."""
    if isinstance(scope, str):
        if scope not in _SCOPE_PARTS:
            raise FstkError(f"unknown removal scope {scope!r}")
        parts = set(_SCOPE_PARTS[scope])
    else:
        parts = set(scope)
        unknown = parts - set(PARTS)
        if unknown:
            raise FstkError(f"unknown removal part(s) {sorted(unknown)}")
    if "project" in parts:
        parts.add("entry")
    return frozenset(parts)


@dataclass
class Item:
    label: str
    path: Path
    bytes: int = 0
    exists: bool = True


@dataclass
class RemovalPlan:
    episode: EpisodeId
    project: list[Item] = field(default_factory=list)
    files: list[Item] = field(default_factory=list)
    captures: list[Item] = field(default_factory=list)
    captures_note: str = ""                      # why the captures are not offered, if they are not
    captures_kept: Item | None = None            # a capture folder the person chose: shown, never deleted

    def items(self, scope: str | set[str] | frozenset[str]) -> list[Item]:
        parts = parts_of(scope)
        return ([*self.project] if "project" in parts else []) + ([*self.files] if "files" in parts else []) \
            + ([*self.captures] if "captures" in parts else [])

    def total_bytes(self, scope: str | set[str] | frozenset[str]) -> int:
        return sum(i.bytes for i in self.items(scope))


def dir_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _inside(path: Path, base: Path) -> bool:
    try:
        p, b = path.resolve(strict=False), base.resolve(strict=False)
    except OSError:
        return False
    return p != b and b in p.parents


def _item(label: str, path: Path) -> Item:
    ok = path.exists()
    return Item(label, path, dir_bytes(path) if ok and path.is_dir() else (path.stat().st_size if ok else 0), ok)


def plan_removal(profile, episode: EpisodeId, manifest=None) -> RemovalPlan:
    from fstk.config.profile import MEDIA_ROOTS

    plan = RemovalPlan(episode)
    plan.project.append(_item("Project (manifest, jobs, approvals, logs)", profile.episode_state_dir(episode)))
    seen: set[Path] = set()
    media_dir = profile.episode_media_dir(episode)
    candidates = [("Working files", media_dir)]
    for name in MEDIA_ROOTS:
        if profile.roots.get(name):
            candidates.append((f"Working files ({name})", profile.root(name, episode)))
    for label, path in candidates:
        key = path.resolve(strict=False)
        if key in seen:
            continue
        seen.add(key)
        plan.files.append(_item(label, path))
    # captures
    sources = (manifest.doc.get("sources") if manifest is not None else None) or {}
    if sources.get("downloads_override"):
        plan.captures_note = ("The captures come from a folder you chose (" + str(sources["downloads_override"]) +
                              "); fstk never deletes those.")
        plan.captures_kept = _item("LaserDisc captures (your folder)", Path(str(sources["downloads_override"])))
    else:
        try:
            folder = profile.downloads_dir(episode)
        except FstkError:
            plan.captures_note = "No downloads folder is set in the profile, so there is nothing fstk downloaded."
        else:
            if folder.is_dir():
                plan.captures.append(_item("Downloaded episode (captures)", folder))
            else:
                plan.captures_note = "No downloaded copy of this episode was found."
    return plan


def running_jobs(profile, episode: EpisodeId) -> list[str]:
    """Jobs of this episode whose lock is held by a live process (or by one we cannot tell about)."""
    from fstk.core.fsio import describe_process, read_lock

    out: list[str] = []
    plans = profile.episode_state_dir(episode) / "plans"
    if not plans.is_dir():
        return out
    for lock in sorted(plans.glob("*/.job.lock")):
        info = read_lock(lock)
        if info is None or info.local_process_alive is False:      # no lock, or its owner is gone: not running
            continue
        who = f"pid {info.pid}" if info.pid else "an unknown process"
        if info.pid and info.local_process_alive:
            what = describe_process(info.pid)
            who += f": {what}" if what else ""
        where = "" if info.local_process_alive else f" on {info.host or 'another machine'}"
        out.append(f"{lock.parent.name} ({who}{where})")
    return out


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def remove_episode(profile, episode: EpisodeId, scope, manifest=None, *, actor: str = "ui") -> list[str]:
    """Do the removal of ``scope`` (a set of ``PARTS``, or an older scope name). Returns what was removed. Raises
    FstkError (before deleting anything) if a path fails its safety check. The project record goes last so a
    half-finished removal can be retried."""
    from fstk.workspace.shared import frozen_reason

    parts = parts_of(scope)
    if not parts:
        raise FstkError("nothing was chosen to remove")
    plan = plan_removal(profile, episode, manifest)
    reason = frozen_reason(profile, episode)
    if reason:
        raise FstkError(reason)
    busy = running_jobs(profile, episode)
    if busy:
        raise FstkError(f"{episode} is being worked on: {'; '.join(busy)}. Wait for it to finish, or stop it, "
                        "before removing the episode. Nothing was deleted.")
    allowed: list[tuple[Item, Path]] = []
    for item in plan.items(parts):
        if item in plan.project:
            base = profile.data_root
        elif item in plan.files:
            base = Path(profile.roots.get(_root_name(profile, episode, item.path), "") or profile.media_root)
        else:
            base = Path(profile.sources["downloads_root"])
        if not _inside(item.path, base):
            raise FstkError(f"refusing to delete {item.path}: it is not inside {base}")
        allowed.append((item, base))
    removed: list[str] = []
    ordered = [a for a in allowed if a[0] not in plan.project] + [a for a in allowed if a[0] in plan.project]
    for item, _base in ordered:
        if item.exists:
            _delete(item.path)
            removed.append(str(item.path))
    if "entry" in parts and "project" not in parts and profile.episode_state_dir(episode).is_dir():
        from fstk.workspace.episodes import hide

        hide(profile, episode, actor=actor)
        removed.append("the episode's entry in the app (its project is kept)")
    return removed


def _root_name(profile, episode: EpisodeId, path: Path) -> str:
    from fstk.config.profile import MEDIA_ROOTS

    for name in MEDIA_ROOTS:
        if profile.roots.get(name) and profile.root(name, episode).resolve(strict=False) == path.resolve(strict=False):
            return name
    return ""
