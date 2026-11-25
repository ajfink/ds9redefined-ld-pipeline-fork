"""Files of attempts that cannot be used again go once the final output is approved (owner, 2026-11-25)."""
from __future__ import annotations

import json

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.ui.services import PendingReview, Services
from fstk.workflow.engine import EpisodeWorkspace
from fstk.workspace.leftovers import find_leftovers, remove_leftovers

EP = EpisodeId.parse("DS9:S01E20")


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    out, audio, master = (prof.root(n, EP) for n in ("out", "audio", "prores"))
    files = {
        "final": out / "S01E20-final-r2.mkv",                         # the approved deliverable (named)
        "encode": out / "S01E20-23.976-padfixed-video-r2.mkv",        # its encode (named)
        "old_encode": out / "S01E20-23.976-padfixed-video.mkv",       # superseded revision
        "old_final": out / "S01E20-final.mkv",                         # superseded revision
        "attempt": out / "S01E20-23.976-padfixed-video.previous-attempt.mkv",
        "partial": master / "S01E20-stack-master.without-timecode-pads.partial.mkv",
        "tmp": audio / "S01E20-dvd51.tmp.flac",
        "old_audio": audio / "delivery" / "S01E20-final" / "a.flac",   # audio encoded for the superseded remux
        "audio": audio / "S01E20-dvd51.flac",                          # named
        "kept_audio": audio / "delivery" / "S01E20-final-r2" / "a.flac",
        "master": master / "S01E20-stack-master.mkv",                  # named
    }
    for p in files.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
    m = Manifest.new(EP, title="t")
    m.set("deliverable.key", "final", by="t")
    m.set_artifact("final", root="out", name=files["final"].name)
    m.set_artifact("padfixed_video", root="out", name=files["encode"].name)
    m.set_artifact("fiveone", root="audio", name=files["audio"].name)
    m.set_artifact("stack_master", root="prores", name=files["master"].name)
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    return prof, ws, files


GONE = ("old_encode", "old_final", "attempt", "partial", "tmp", "old_audio")


def test_only_unusable_attempts_are_listed(env):
    prof, ws, files = env
    found = {x.path for x in find_leftovers(prof, EP, ws.store.load())}
    want = {files[k] for k in GONE if k != "old_audio"} | {files["old_audio"].parent}
    assert found == want


def test_removal_keeps_everything_the_manifest_names(env):
    prof, ws, files = env
    removed = remove_leftovers(prof, EP, ws.store.load())
    assert len(removed) == len(GONE)
    for k, p in files.items():
        assert p.exists() == (k not in GONE), k


def test_nothing_goes_while_a_job_runs(env, monkeypatch):
    prof, ws, files = env
    monkeypatch.setattr("fstk.workspace.removal.running_jobs", lambda *a: ["delivery-x (pid 1)"])
    with pytest.raises(FstkError, match="being worked on"):
        remove_leftovers(prof, EP, ws.store.load())
    assert all(p.exists() for p in files.values())


def test_approving_the_delivery_clears_them_and_keeps_a_list(env, tmp_path):
    prof, ws, files = env
    svc = Services(prof)
    evidence = prof.root("reports", EP) / "qc-1.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("{}")
    review = PendingReview("delivery-x", "qc", "qc-report", "delivery", (str(evidence),), "")
    svc.decide(EP, review, decision="REJECTED", note="no", actor="op")
    assert files["attempt"].exists() and svc.last_cleanup is None             # a rejection keeps everything
    svc.decide(EP, review, decision="APPROVED", note="ok", actor="op")
    doc = svc.last_cleanup
    assert len(doc["removed"]) == len(GONE) and not files["attempt"].exists() and files["final"].exists()
    from pathlib import Path

    assert json.loads(Path(doc["report"]).read_text())["why"] == "final output approved"


def test_a_stale_old_style_lock_does_not_block_the_cleanup(env):
    """A lock written before ptime existed, whose pid now belongs to a process that started later (PC-A)."""
    import os
    import socket

    prof, ws, files = env
    lock = prof.episode_state_dir(EP) / "plans" / "intake-old" / ".job.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"pid={os.getpid()} host={socket.gethostname()} started_utc=2020-01-01T00:00:00+00:00 purpose=t\n")
    removed = remove_leftovers(prof, EP, ws.store.load())
    assert removed and not files["attempt"].exists()


def test_leftovers_can_be_removed_again_from_the_episode_menu(env, monkeypatch):
    prof, ws, files = env
    svc = Services(prof)
    assert {x.path for x in svc.leftovers(EP)} == {x.path for x in find_leftovers(prof, EP, ws.store.load())}
    monkeypatch.setattr(Services, "episode_status", lambda self, e: ("3 of 9 steps", "quiet"))
    assert not svc.final_output_approved(EP)
    monkeypatch.setattr(Services, "episode_status", lambda self, e: ("delivered", "good"))
    assert svc.final_output_approved(EP)
