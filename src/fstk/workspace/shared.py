"""Shared multi-machine workspace: publication, presence, handoff and adoption (ADR 0002).

Layout under ``profile.shared_root``::

    devices/<DEVICE>-<ID>/device.json, heartbeat.json
    devices/<DEVICE>-<ID>/episodes/<SERIES>/<CODE>/status.json
    devices/<DEVICE>-<ID>/episodes/<SERIES>/<CODE>/snapshots/<snapshot-id>/
        files.json                  sha256 + size of every file below (the snapshot id is its hash)
        manifest/manifest.json, manifest/journal.jsonl
        approvals/*.json, reports/**, references/**

A device writes only inside its own ``devices/<DEVICE>-<ID>`` folder; everything of a peer is
read-only here. Snapshots are content-addressed, written to a temporary name and renamed into
place, and never changed afterwards. The manifest, journal and approvals travel with portable
paths (``portable.py``); reports and references travel byte for byte, so approvals bound to them
stay verifiable on the receiving device (the shared root is private to the owner's machines).
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fstk import __version__
from fstk.core.errors import FstkError, ManifestError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, canonical_json, read_json
from fstk.core.hashing import SCOPE_HEAD_TAIL, Fingerprint, sha256_bytes
from fstk.core.identity import EpisodeId
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.manifest.model import Manifest
from fstk.manifest.store import MANIFEST_NAME, ManifestStore

from . import portable
from .device import DeviceIdentity, claim_folder, load_or_create

STATUS_SCHEMA = "fstk-shared-episode-status-v1"
SNAPSHOT_SCHEMA = "fstk-shared-snapshot-v1"
HANDOFF_MARKER = "handoff.json"
ADOPTED_MARKER = "adopted.json"
TRAVELLING_DIRS = ("approvals", "reports", "references")
MAX_TRAVEL_FILE = 32 * 1024 * 1024        # reports/references larger than this stay local
STATES = ("active", "handed-off", "released")


class SharedError(FstkError):
    pass


def frozen_marker(profile, episode: EpisodeId) -> Path:
    return profile.episode_state_dir(episode) / HANDOFF_MARKER


def frozen_reason(profile, episode: EpisodeId) -> str | None:
    """Why this device must not run the episode (it was handed off), or None."""
    marker = frozen_marker(profile, episode)
    if not marker.is_file():
        return None
    doc = read_json(marker)
    return (f"{episode} was handed off to {doc.get('to')} at {doc.get('utc')} (snapshot {doc.get('snapshot')}); "
            "this device no longer runs it. `fstk share reclaim` takes it back if the other device has "
            "not adopted it.")


@dataclass
class Presence:
    device: str
    state: str
    updated_utc: str
    title: str
    snapshot: str | None
    detail: dict[str, Any]
    mine: bool

    def describe(self) -> str:
        bits = [f"{self.device}: {self.state}"]
        if self.detail.get("handoff"):
            bits.append(f"→ {self.detail['handoff'].get('to')}")
        if self.detail.get("adopted_from"):
            bits.append(f"(adopted from {self.detail['adopted_from'].get('device')})")
        if self.detail.get("awaiting_reviews"):
            bits.append(f"awaiting review: {', '.join(self.detail['awaiting_reviews'])}")
        bits.append(f"updated {self.updated_utc}")
        return " ".join(bits)


class SharedTree:
    """This device's view of the shared root."""

    def __init__(self, profile):
        if not profile.shared_root:
            raise SharedError("this machine profile has no shared_root; the shared workspace is off")
        self.profile = profile
        self.root = Path(profile.shared_root)
        if not self.root.is_dir():
            raise SharedError(f"shared_root {self.root} is not reachable (offline share?); local work is unaffected")
        if any((self.root / name).exists() for name in ("WORKFLOW_VERSION", "ldcommon.py", "scripts")):
            raise SharedError(f"{self.root} looks like a v38 release tree; give fstk its own shared folder "
                              "(e.g. W:\\fstk-shared) so v38's devices/ folders are never touched")
        self.devices = self.root / "devices"
        self.ident: DeviceIdentity = load_or_create(profile)
        self.folder = claim_folder(self.devices, self.ident, os_name=profile.os)
        self.pmap = portable.PathMap.for_profile(profile)

    @property
    def device(self) -> str:
        return self.ident.name

    # ------------------------------------------------------------------ paths
    def episode_dir(self, device: str, episode: EpisodeId) -> Path:
        return self.devices / device / "episodes" / episode.series / episode.code

    def _own(self, episode: EpisodeId) -> Path:
        return self.episode_dir(self.device, episode)

    def peers(self) -> list[str]:
        if not self.devices.is_dir():
            return []
        return sorted(p.name for p in self.devices.iterdir()
                      if p.is_dir() and p.name != self.device and (p / "device.json").is_file())

    # ------------------------------------------------------------------ presence
    def presence(self, episode: EpisodeId) -> list[Presence]:
        out = []
        for device in [self.device, *self.peers()]:
            path = self.episode_dir(device, episode) / "status.json"
            if not path.is_file():
                continue
            try:
                doc = read_json(path)
            except FstkError as exc:
                out.append(Presence(device, "unreadable", "", "", None, {"error": str(exc)}, device == self.device))
                continue
            out.append(Presence(device, doc.get("state", "?"), doc.get("updated_utc", ""), doc.get("title", ""),
                                doc.get("snapshot"), doc, device == self.device))
        return out

    def all_presence(self) -> dict[str, list[Presence]]:
        """Every episode any device has published: canonical id → presence rows."""
        found: dict[str, list[Presence]] = {}
        for device in [self.device, *self.peers()]:
            base = self.devices / device / "episodes"
            if not base.is_dir():
                continue
            for status in base.glob("*/*/status.json"):
                try:
                    ep = EpisodeId.parse(f"{status.parent.parent.name}:{status.parent.name}")
                except FstkError:
                    continue
                found.setdefault(ep.canonical, [])
        for canonical in found:
            found[canonical] = self.presence(EpisodeId.parse(canonical))
        return found

    def active_elsewhere(self, episode: EpisodeId) -> list[Presence]:
        return [p for p in self.presence(episode) if not p.mine and p.state == "active"]

    def check_new_episode(self, episode: EpisodeId, *, allow_parallel: bool = False) -> None:
        """Refuse to start an episode a peer is actively working on (unless the operator says so)."""
        busy = self.active_elsewhere(episode)
        if busy and not allow_parallel:
            raise SharedError(f"{episode} is active on {', '.join(p.device for p in busy)}; adopt it after a "
                              "handoff, fork it, or start a separate parallel workspace deliberately")

    # ------------------------------------------------------------------ snapshot
    def _local_state(self, episode: EpisodeId) -> Path:
        state = self.profile.episode_state_dir(episode)
        if not (state / "manifest" / MANIFEST_NAME).is_file():
            raise SharedError(f"{episode} has no local workspace on this device")
        return state

    def _snapshot_files(self, state: Path) -> dict[str, bytes]:
        """Relative path → portable bytes of everything that travels."""
        files: dict[str, bytes] = {}
        store = ManifestStore(state / "manifest")
        files["manifest/manifest.json"] = canonical_json(portable.encode(read_json(store.path), self.pmap))
        if store.journal_path.is_file():
            lines = [json.dumps(portable.encode(row, self.pmap), sort_keys=True, ensure_ascii=False)
                     for row in store.journal()]
            files["manifest/journal.jsonl"] = ("\n".join(lines) + "\n").encode("utf-8")
        for name in TRAVELLING_DIRS:
            base = state / name
            if not base.is_dir():
                continue
            for path in sorted(p for p in base.rglob("*") if p.is_file() and not p.name.startswith(".")):
                if path.stat().st_size > MAX_TRAVEL_FILE:
                    continue
                rel = f"{name}/{path.relative_to(base).as_posix()}"
                raw = path.read_bytes()
                if name == "approvals" and path.suffix == ".json":
                    # approvals are re-pointed at the receiving device's copies of their evidence
                    with contextlib.suppress(UnicodeDecodeError, ValueError):
                        raw = canonical_json(portable.encode(json.loads(raw.decode("utf-8")), self.pmap))
                # reports/references travel byte for byte: approvals bound to them stay verifiable
                files[rel] = raw
        return files

    def _write_snapshot(self, episode: EpisodeId, files: dict[str, bytes]) -> str:
        index = {rel: {"sha256": sha256_bytes(b), "bytes": len(b)} for rel, b in sorted(files.items())}
        index_doc = {"schema": SNAPSHOT_SCHEMA, "episode": episode.canonical, "files": index}
        snap_id = sha256_bytes(canonical_json(index_doc))[:16]
        snaps = self._own(episode) / "snapshots"
        final = snaps / snap_id
        if final.is_dir() and (final / "files.json").is_file():
            return snap_id                                     # identical content already published
        temp = snaps / f".tmp-{snap_id}-{secrets.token_hex(4)}"
        for rel, payload in files.items():
            atomic_write_bytes(temp / rel, payload)
        atomic_write_json(temp / "files.json", index_doc)
        try:
            os.replace(temp, final)
        except OSError:
            if not (final / "files.json").is_file():
                raise
            shutil.rmtree(temp, ignore_errors=True)           # a concurrent publish of the same content won
        return snap_id

    def _jobs_summary(self, state: Path) -> tuple[list[dict], list[str]]:
        jobs, awaiting = [], []
        plans = state / "plans"
        if plans.is_dir():
            for st in sorted(plans.glob("*/state.json")):
                try:
                    doc = read_json(st)
                except FstkError:
                    continue
                steps = {sid: s.get("status") for sid, s in (doc.get("steps") or {}).items()}
                jobs.append({"job": st.parent.name, "steps": steps})
                awaiting += [f"{st.parent.name}/{sid}" for sid, s in steps.items() if s == "awaiting-review"]
        return jobs, awaiting

    def publish(self, episode: EpisodeId, *, state: str | None = None, extra: dict | None = None) -> dict:
        """Publish this device's snapshot + status for one episode. Returns the status document."""
        local = self._local_state(episode)
        snap_id = self._write_snapshot(episode, self._snapshot_files(local))
        store = ManifestStore(local / "manifest")
        snapshot = store.read()
        own_status = self._own(episode) / "status.json"
        previous = read_json(own_status) if own_status.is_file() else {}
        if state is None:
            state = "handed-off" if frozen_marker(self.profile, episode).is_file() else "active"
        if state not in STATES:
            raise SharedError(f"unknown shared state {state!r}")
        jobs, awaiting = self._jobs_summary(local)
        m = snapshot.manifest
        adopted = local / ADOPTED_MARKER
        doc = {"schema": STATUS_SCHEMA, "device": self.device, "machine": self.profile.machine,
               "episode": episode.canonical, "title": m.title, "state": state, "updated_utc": utc_now_iso(),
               "snapshot": snap_id, "manifest_sha256": snapshot.sha256, "journal_entries": len(store.journal()),
               "values_set": len(m.doc["values"]), "artifacts": sorted(m.doc["artifacts"]), "jobs": jobs,
               "awaiting_reviews": awaiting, "fstk_version": __version__,
               "adopted_from": read_json(adopted).get("from") if adopted.is_file() else None,
               "handoff": previous.get("handoff") if state == "handed-off" else None,
               **(extra or {})}
        atomic_write_json(own_status, doc)
        return doc

    # ------------------------------------------------------------------ handoff
    def media_inventory(self, episode: EpisodeId) -> list[dict]:
        """Each manifest artifact: logical location, and size + head/tail hash if present here."""
        m = ManifestStore(self._local_state(episode) / "manifest").load()
        rows = []
        for key, spec in sorted(m.doc["artifacts"].items()):
            if "path" in spec:
                path = Path(spec["path"])
                logical = {"path": self.pmap.encode_path(str(path)) or str(path)}
            else:
                path = self.profile.root(spec["root"], episode) / spec["name"]
                logical = {"root": spec["root"], "name": spec["name"]}
            row = {"artifact": key, **logical, "present": path.is_file()}
            if path.is_file():
                fp = Fingerprint.of(path, scope=SCOPE_HEAD_TAIL)
                row.update(bytes=fp.size_bytes, sha256=fp.sha256, scope=fp.scope)
            rows.append(row)
        return rows

    def handoff(self, episode: EpisodeId, *, to: str, actor: str, note: str = "") -> dict:
        if to == self.device:
            raise SharedError("an episode cannot be handed off to the same device")
        if to not in self.peers():
            raise SharedError(f"no device {to!r} in the shared tree (known: {', '.join(self.peers()) or 'none'})")
        marker = frozen_marker(self.profile, episode)
        if marker.is_file():
            raise SharedError(frozen_reason(self.profile, episode))
        self._local_state(episode)
        inventory = self.media_inventory(episode)
        status = self.publish(episode, state="active")
        handoff = {"to": to, "by": actor, "utc": utc_now_iso(), "note": note, "snapshot": status["snapshot"],
                   "manifest_sha256": status["manifest_sha256"], "media": inventory}
        atomic_write_json(marker, {"schema": "fstk-handoff-v1", "from": self.device, **handoff}, exclusive=True)
        atomic_write_json(self._own(episode) / "status.json", {**status, "state": "handed-off",
                                                               "updated_utc": utc_now_iso(), "handoff": handoff})
        return handoff

    def adopted_by(self, episode: EpisodeId) -> list[str]:
        """Peers whose workspace for this episode was adopted from this device."""
        return [p.device for p in self.presence(episode)
                if not p.mine and (p.detail.get("adopted_from") or {}).get("device") == self.device]

    def reclaim(self, episode: EpisodeId, *, actor: str) -> None:
        marker = frozen_marker(self.profile, episode)
        if not marker.is_file():
            raise SharedError(f"{episode} is not handed off on this device")
        taken = self.adopted_by(episode)
        if taken:
            raise SharedError(f"{episode} was already adopted by {', '.join(taken)}; it continues there "
                              "(hand it back from that device instead)")
        doc = read_json(marker)
        atomic_write_json(marker.with_name(f"handoff-reclaimed-{utc_stamp()}.json"),
                          {**doc, "reclaimed_by": actor, "reclaimed_utc": utc_now_iso()}, exclusive=True)
        os.replace(marker, marker.with_name(f".handoff-superseded-{utc_stamp()}.json"))
        self.publish(episode, state="active")

    # ------------------------------------------------------------------ adopt
    def adopt(self, episode: EpisodeId, *, source: str, actor: str, fork: bool = False) -> dict:
        """Create this device's workspace from a peer's snapshot. Returns an adoption report."""
        if source == self.device:
            raise SharedError("cannot adopt from this device itself")
        status_path = self.episode_dir(source, episode) / "status.json"
        if not status_path.is_file():
            raise SharedError(f"{source} has not published {episode}")
        status = read_json(status_path)
        handoff = status.get("handoff") or {}
        if not fork and (status.get("state") != "handed-off" or handoff.get("to") != self.device):
            raise SharedError(f"{source} has not handed {episode} off to {self.device} (state "
                              f"{status.get('state')!r}); ask for a handoff, or adopt with fork=True to make "
                              "a deliberate parallel copy")
        snap_id = handoff.get("snapshot") if not fork else status.get("snapshot")
        snap = self.episode_dir(source, episode) / "snapshots" / str(snap_id)
        files = self._verified_snapshot(snap)
        state = self.profile.episode_state_dir(episode)
        store = ManifestStore(state / "manifest")
        if store.exists():
            raise SharedError(f"{episode} already has a workspace on this device ({store.path}); it is never "
                              "overwritten by an adoption")
        self.pmap.unmapped.clear()
        doc = portable.decode(json.loads(files["manifest/manifest.json"]), self.pmap)
        try:
            manifest = Manifest(doc)
        except ManifestError as exc:
            raise SharedError(f"snapshot manifest is not valid here: {exc}") from None
        if manifest.episode != episode:
            raise SharedError(f"snapshot is for {manifest.episode}, not {episode}")
        source_sha = status.get("manifest_sha256") if fork else handoff.get("manifest_sha256")
        reason = (f"{'forked' if fork else 'adopted'} from {source} snapshot {snap_id} "
                  f"(source manifest sha256 {source_sha})")
        store.create(manifest, by=actor, reason=reason, origin="shared-adopt")
        if "manifest/journal.jsonl" in files:
            atomic_write_bytes(state / "manifest" / f"journal.source-{source}.jsonl",
                               files["manifest/journal.jsonl"], exclusive=True)
        for rel, payload in files.items():
            top = rel.split("/", 1)[0]
            if top not in TRAVELLING_DIRS:
                continue
            if top == "approvals" and rel.endswith(".json"):
                with contextlib.suppress(ValueError):
                    payload = canonical_json(portable.decode(json.loads(payload), self.pmap))
            target = state / rel
            if target.exists():
                continue
            atomic_write_bytes(target, payload, exclusive=True)
        media = self._check_media(episode, handoff.get("media") or [], manifest)
        report = {"schema": "fstk-adoption-v1", "episode": episode.canonical, "from": {
                      "device": source, "snapshot": snap_id, "manifest_sha256": source_sha, "fork": fork},
                  "by": actor, "utc": utc_now_iso(), "media": media,
                  "unmapped_tokens": sorted(self.pmap.unmapped),
                  "note": "job state does not travel; plan the remaining stages on this device"}
        atomic_write_json(state / ADOPTED_MARKER, report, exclusive=True)
        self.publish(episode, state="active")
        return report

    def _verified_snapshot(self, snap: Path) -> dict[str, bytes]:
        index_path = snap / "files.json"
        if not index_path.is_file():
            raise SharedError(f"snapshot {snap} is incomplete (no files.json)")
        index = read_json(index_path)
        if sha256_bytes(canonical_json(index))[:16] != snap.name:
            raise SharedError(f"snapshot {snap.name} does not match its own index (altered after publication)")
        files = {}
        for rel, meta in index["files"].items():
            path = snap / rel
            raw = path.read_bytes() if path.is_file() else None
            if raw is None or len(raw) != meta["bytes"] or sha256_bytes(raw) != meta["sha256"]:
                raise SharedError(f"snapshot file {rel} is missing or changed since publication")
            files[rel] = raw
        if "manifest/manifest.json" not in files:
            raise SharedError("snapshot has no manifest")
        return files

    def _check_media(self, episode: EpisodeId, inventory: list[dict], manifest: Manifest) -> list[dict]:
        """Compare the sender's media inventory with what exists at this device's resolved paths."""
        rows = []
        listed = {row["artifact"]: row for row in inventory}
        for key, spec in sorted(manifest.doc["artifacts"].items()):
            sent = listed.get(key, {})
            path = Path(spec["path"]) if "path" in spec else self.profile.root(spec["root"], episode) / spec["name"]
            row = {"artifact": key, "local_path": str(path), "sender_had_it": bool(sent.get("present"))}
            if not path.is_file():
                row["status"] = "missing"
            elif sent.get("sha256"):
                fp = Fingerprint.of(path, scope=sent.get("scope", SCOPE_HEAD_TAIL))
                same = fp.size_bytes == sent.get("bytes") and fp.sha256 == sent["sha256"]
                row["status"] = "present" if same else "different"
            else:
                row["status"] = "present (not compared)"
            if sent.get("bytes"):
                row["bytes"] = sent["bytes"]
            rows.append(row)
        return rows


def try_publish(profile, episode: EpisodeId) -> str:
    """Best-effort publication after local work; never raises. Returns a one-line outcome."""
    if not profile.shared_root:
        return "shared workspace off"
    try:
        doc = SharedTree(profile).publish(episode)
        return f"published {episode} snapshot {doc['snapshot']} ({doc['state']})"
    except (FstkError, OSError) as exc:
        return f"not published: {exc}"


def media_line(row: dict) -> str:
    size = f" {row['bytes'] / 1024 ** 3:.2f} GiB" if row.get("bytes") else ""
    return f"{row['artifact']}: {row['status']}{size} — {row['local_path']}"

