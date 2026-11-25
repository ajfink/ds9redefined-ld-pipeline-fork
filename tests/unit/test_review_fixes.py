"""Regression tests for the 2026-11-23 deep review (docs/DEVLOG CP31)."""
from __future__ import annotations

import os
import socket
import sys

import pytest

from fstk.core.errors import FstkError
from fstk.core.fsio import (
    JsonFileError,
    LockHeldError,
    exclusive_lock,
    keep_backup,
    move_aside,
    pid_alive,
    process_start_time,
    read_json,
    read_lock,
)
from fstk.workflow.queue import QueueRunner, QueueStore
from tests.unit.test_removal import build


# ------------------------------------------------------------------------- A1 damaged files
def test_bad_json_is_a_value_error_and_a_fstk_error(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a": [')
    with pytest.raises(JsonFileError) as exc:
        read_json(p)
    assert isinstance(exc.value, ValueError) and isinstance(exc.value, FstkError)
    with pytest.raises(FileNotFoundError):
        read_json(tmp_path / "missing.json")
    with pytest.raises(FstkError):
        read_json(tmp_path / "missing.json")


def test_damaged_queue_starts_empty_and_is_kept_aside(tmp_path):
    p = tmp_path / "queue.json"
    q = QueueStore(p)
    q.add("DS9:S01E01", "intake")
    p.write_text(p.read_text()[:20])
    q2 = QueueStore(p)
    assert q2.items == []
    q2.add("DS9:S01E03", "intake")
    assert [i.episode for i in QueueStore(p).items] == ["DS9:S01E03"]
    kept = list(tmp_path.glob("queue.json.bad-*"))
    assert len(kept) == 1 and kept[0].read_text().startswith('{')


def test_queue_file_of_another_schema_is_not_overwritten(tmp_path):
    p = tmp_path / "queue.json"
    p.write_text('{"schema": "something-else", "items": []}')
    QueueStore(p)
    assert list(tmp_path.glob("queue.json.bad-*"))


def test_damaged_stack_and_efm_caches_are_ignored(tmp_path):
    from fstk.stages.stack import _load_pass

    p = tmp_path / "pass1.result.json"
    p.write_text('{"order": [')
    assert _load_pass(p, [], "k") is None
    assert not p.exists() and list(tmp_path.glob("pass1.result.json.bad-*"))


# ------------------------------------------------------------------------- A2 removal guard
def test_removal_refuses_while_a_job_lock_is_live(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    job_dir = svc.workspace("DS9:S01E20").job_dir("intake")
    job_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(job_dir / ".job.lock", purpose="engine"), pytest.raises(FstkError, match="being worked on"):
        svc.remove_episode("DS9:S01E20", "files")
    assert media.exists() and prof.episode_state_dir(svc.workspace("DS9:S01E20").episode).exists()


def test_removal_ignores_a_stale_lock_and_drops_waiting_queue_items(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    job_dir = svc.workspace("DS9:S01E20").job_dir("intake")
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / ".job.lock").write_text(f"pid=999999 host={socket.gethostname()} started_utc=x purpose=engine\n")
    store = svc.queue_store()
    store.add("DS9:S01E20", "intake")
    store.add("DS9:S01E21", "intake")
    svc.remove_episode("DS9:S01E20", "files")
    assert not media.exists()
    assert [i.episode for i in QueueStore(prof.data_root / "queue.json").items] == ["DS9:S01E21"]


def test_removal_refuses_an_item_running_in_the_queue(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    store = svc.queue_store()
    it = store.add("DS9:S01E20", "intake")
    it.status = "running"
    with pytest.raises(FstkError, match="running in the queue"):
        svc.remove_episode("DS9:S01E20", "files")
    assert media.exists()


# ------------------------------------------------------------------------- A3 two windows
def test_two_windows_merge_instead_of_overwriting(tmp_path):
    p = tmp_path / "queue.json"
    a, b = QueueStore(p), QueueStore(p)
    a.add("DS9:S01E01", "intake")
    b.add("DS9:S01E02", "intake")
    assert sorted(i.episode for i in QueueStore(p).items) == ["DS9:S01E01", "DS9:S01E02"]
    assert sorted(i.episode for i in b.items) == ["DS9:S01E01", "DS9:S01E02"]
    assert a.sync() and sorted(i.episode for i in a.items) == ["DS9:S01E01", "DS9:S01E02"]


def test_a_change_in_one_window_reaches_the_other_unless_both_changed_it(tmp_path):
    p = tmp_path / "queue.json"
    a = QueueStore(p)
    it = a.add("DS9:S01E01", "intake")
    b = QueueStore(p)
    b.get(it.id).status, b.get(it.id).detail = "failed", "boom"
    b.save()
    assert a.sync() and a.get(it.id).status == "failed"
    a.remove(it.id)
    assert b.sync() and b.items == []


def test_one_runner_per_data_folder(tmp_path):
    p = tmp_path / "queue.json"
    qa, qb = QueueStore(p), QueueStore(p)
    ra = QueueRunner(qa, planner=lambda e, k: None, engine_factory=lambda *a: None)
    rb = QueueRunner(qb, planner=lambda e, k: None, engine_factory=lambda *a: None)
    ra.start()
    try:
        # the same process holds it here, so the second runner is refused the same way another window would be
        with pytest.raises(FstkError, match="already being run"):
            rb.start()
    finally:
        ra.shutdown(5)
    rb.start()
    rb.shutdown(5)
    assert not (tmp_path / "queue-runner.lock").exists()


def test_stale_runner_lock_of_a_dead_process_is_replaced(tmp_path):
    p = tmp_path / "queue.json"
    (tmp_path / "queue-runner.lock").write_text(f"pid=999999 host={socket.gethostname()} started_utc=x purpose=queue-runner\n")
    r = QueueRunner(QueueStore(p), planner=lambda e, k: None, engine_factory=lambda *a: None)
    r.start()
    r.shutdown(5)


# ------------------------------------------------------------------------- A4 backups
def test_profile_and_queue_keep_a_previous_version(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    prof.save()
    prof.save()
    assert prof.path.with_name(prof.path.name + ".bak").is_file()
    q = QueueStore(tmp_path / "queue.json")
    q.add("DS9:S01E01", "intake")
    q.add("DS9:S01E02", "intake")
    assert (tmp_path / "queue.json.bak").is_file()
    assert keep_backup(tmp_path / "nothing.json") is None


def test_damaged_profile_points_at_the_backup(tmp_path):
    from fstk.config.profile import Profile
    from fstk.core.errors import ConfigError

    prof, *_ = build(tmp_path)
    prof.save()
    prof.save()
    prof.path.write_text("{")
    with pytest.raises(ConfigError, match=r"\.bak"):
        Profile.load(prof.path)


def test_move_aside_names_the_damaged_file(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("x")
    kept = move_aside(f)
    assert kept is not None and kept.name.startswith("a.json.bad-") and not f.exists()
    assert move_aside(f) is None


# ------------------------------------------------------------------------- A5 pid liveness
def test_pid_alive_knows_the_process_start_time():
    me = os.getpid()
    started = process_start_time()
    assert started is not None and pid_alive(me, started)
    assert not pid_alive(me, started - 3600)          # same pid, but a different (earlier) process: reused pid
    assert not pid_alive(0) and not pid_alive(-5)


def test_locks_record_the_start_time(tmp_path):
    with exclusive_lock(tmp_path / "x.lock", purpose="t"):
        info = read_lock(tmp_path / "x.lock")
        assert info.ptime is not None and info.local_process_alive is True
    stale = tmp_path / "y.lock"
    stale.write_text(f"pid={os.getpid()} host={socket.gethostname()} started_utc=x purpose=t ptime=1000.0\n")
    assert read_lock(stale).local_process_alive is False       # our pid, but not the process that wrote the lock
    with pytest.raises(LockHeldError):
        with exclusive_lock(stale):
            pass


def test_an_old_lock_without_ptime_is_stale_when_its_pid_was_reused(tmp_path):
    """PC-A, 2026-11-25: a two-day-old intake lock (written before ptime) named a pid Windows had given to another
    process, so the leftover cleanup thought S01E20 was still being worked on."""
    from datetime import UTC, datetime, timedelta

    started = process_start_time()
    host, me = socket.gethostname(), os.getpid()

    def lock(when):
        p = tmp_path / f"{when.timestamp():.0f}.lock"
        p.write_text(f"pid={me} host={host} started_utc={when.isoformat()} purpose=t\n")
        return read_lock(p)

    before = datetime.fromtimestamp(started, UTC) - timedelta(hours=1)    # taken before this process existed
    after = datetime.fromtimestamp(started, UTC) + timedelta(seconds=5)   # taken by this process
    assert lock(before).local_process_alive is False
    assert lock(after).local_process_alive is True
    assert pid_alive(me, started_by=started + 1) and not pid_alive(me, started_by=started - 60)


@pytest.mark.skipif(sys.platform == "win32", reason="posix zombie handling")
def test_a_finished_child_is_not_alive():
    import subprocess

    c = subprocess.Popen([sys.executable, "-c", "pass"])
    c.wait()
    assert not pid_alive(c.pid)


# ------------------------------------------------------------------------- start-up with a running queue
@pytest.mark.gui
def test_window_resumes_a_saved_running_queue_without_crashing(tmp_path, qtbot, monkeypatch):
    import json
    from pathlib import Path

    from fstk.config.profile import SCHEMA, Profile
    from fstk.manifest.v38 import import_v38
    from fstk.ui.app import MainWindow
    from fstk.ui.services import Services
    from fstk.workflow.engine import EpisodeWorkspace
    from tests.unit.test_removal import EP, FIX

    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "data"),
                              "media_root": str(tmp_path / "media"), "limits": {"min_free_gb": 0.001}})
    EpisodeWorkspace(prof, EP).store.create(import_v38(json.loads(Path(FIX).read_text())), by="t", reason="fixture")
    svc = Services(prof)
    store = svc.queue_store()
    store.add("DS9:S01E20", "intake")
    store.running = True
    store.save()
    def no_plan(self, episode, kind):
        raise FstkError("no plan in this test")                  # the item fails at once instead of really running

    monkeypatch.setattr(Services, "queue_plan", no_plan)
    svc2 = Services(prof)
    w = MainWindow(svc2, actor="op")
    qtbot.addWidget(w)
    assert w.queue_runner.running
    w._resume_queue()                                   # what the 3-second timer calls
    qtbot.waitUntil(lambda: store_item_done(svc2), timeout=5000)
    w.queue_runner.shutdown(5)


def store_item_done(svc) -> bool:
    return all(i.status != "queued" and i.status != "running" for i in svc.queue_store().items)


# ------------------------------------------------------------------------- B1 drive advice
def _vol(mount, free_gib, total_gib=1000, kind="local"):
    from fstk.workspace.volumes import GIB, Volume

    return Volume(mount, int(total_gib * GIB), int(free_gib * GIB), kind)


def _profile(tmp_path, *, media, data=None, downloads="D:\\Captures", reserve=80):
    from fstk.config.profile import SCHEMA, Profile

    return Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "windows", "data_root": data or media,
                              "media_root": media, "sources": {"downloads_root": downloads},
                              "limits": {"min_free_gb": reserve}})


def test_advice_for_the_owners_laptop_layout(tmp_path):
    """v38 §6: C: is a 238 GB SSD with 30.5 GiB free, E: has 2.2 TB, the 80 GiB reserve cannot be met on C:."""
    from fstk.workspace.volumes import advise, worst

    vols = [_vol("C:\\", 30.5, 238), _vol("D:\\", 900, 2000), _vol("E:\\", 2226, 4000),
            _vol("X:\\", 5000, 8000, "network")]
    prof = _profile(tmp_path, media="C:\\Users\\f\\FromStunToKill\\work", data="C:\\Users\\f\\FromStunToKill\\data")
    adv = {a.what: a for a in advise(prof, vols)}
    assert adv["Working folder"].level == "block" and "30" in adv["Working folder"].text
    assert adv["Working folder"].suggestion.startswith("E:")             # the roomiest local drive, never X:
    assert adv["Downloads folder"].level == "ok"
    assert worst(list(adv.values())) == "block"


def test_network_and_removable_drives_are_never_suggested():
    from fstk.workspace.volumes import suggest_folder

    vols = [_vol("X:\\", 9000, 9000, "network"), _vol("F:\\", 5000, 5000, "removable"), _vol("C:\\", 50, 238)]
    assert suggest_folder(vols, 80, 80) is None


def test_a_downloads_folder_that_cannot_hold_an_episode_is_flagged(tmp_path):
    from fstk.workspace.volumes import advise

    vols = [_vol("D:\\", 300, 2000), _vol("E:\\", 2226, 4000)]
    prof = _profile(tmp_path, media="E:\\work", data="E:\\data", downloads="D:\\Captures")
    adv = {a.what: a for a in advise(prof, vols)}
    assert adv["Downloads folder"].level == "warn" and adv["Downloads folder"].suggestion.startswith("E:")
    assert adv["Working folder"].level == "ok"


def test_space_failure_names_a_drive_with_room(tmp_path, monkeypatch):
    from fstk.workspace import volumes

    monkeypatch.setattr(volumes, "list_volumes", lambda: [_vol("C:\\", 30), _vol("E:\\", 2000)])
    text = volumes.hint_for_space(80, "C:\\work\\x")
    assert "E:\\" in text and "Settings" in text
    monkeypatch.setattr(volumes, "list_volumes", lambda: [_vol("C:\\", 30)])
    assert "No other local drive" in volumes.hint_for_space(80, "C:\\work\\x")


def test_hint_never_raises(monkeypatch):
    from fstk.workspace import volumes

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(volumes, "list_volumes", boom)
    assert volumes.hint_for_space(80, "C:\\x") == ""


@pytest.mark.gui
def test_settings_folders_page_shows_advice_and_applies_the_suggestion(tmp_path, qtbot, monkeypatch):
    from fstk.ui.services import Services
    from fstk.ui.settings import SettingsDialog
    from fstk.workspace import volumes

    drive = tmp_path / "big"
    drive.mkdir()
    monkeypatch.setattr(volumes, "list_volumes",
                        lambda: [_vol(str(tmp_path / "small"), 10), _vol(str(drive), 5000, 6000)])
    from fstk.config.profile import SCHEMA, Profile

    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(tmp_path / "small" / "data"), "media_root": str(tmp_path / "small" / "work"),
                              "limits": {"min_free_gb": 80}})
    prof.save(tmp_path / "machine.json")
    svc = Services(prof)
    dlg = SettingsDialog(svc)
    qtbot.addWidget(dlg)
    assert "10 GiB free" in dlg.advice_label.text() and not dlg.use_suggested.isHidden()
    dlg._use_suggested()
    assert str(prof.media_root).startswith(str(drive))
    assert dlg.use_suggested.isHidden()


# ------------------------------------------------------------------------- B2 drift gate uncertainty
def test_slope_verdict_counts_the_measurements_own_uncertainty():
    from fstk.qc.sync import slope_verdict

    assert slope_verdict(5.0, 2.0, 30.0)["verdict"] == "pass"
    near = slope_verdict(26.0, 1.0, 30.0)                     # inside, but under 6 ppm of headroom
    assert near["verdict"] == "marginal" and not near["ambiguous"]
    noisy = slope_verdict(20.0, 6.0, 30.0)                    # 10 ppm of headroom, but 2σ = 12
    assert noisy["verdict"] == "marginal"
    over = slope_verdict(32.0, 3.0, 30.0)                     # fails, but within 2σ of the limit
    assert over["verdict"] == "fail" and over["ambiguous"]
    way = slope_verdict(45.0, 3.0, 30.0)
    assert way["verdict"] == "fail" and not way["ambiguous"]


def test_linear_fit_reports_the_slope_uncertainty():
    import numpy as np

    from fstk.qc.sync import linear_fit

    rng = np.random.default_rng(1)
    x = np.linspace(0, 2400, 7)
    y = 80e-3 + 20e-6 * x + rng.normal(0, 2e-3, 7)
    f = linear_fit(list(x), list(y), y_in_ms=False)
    assert f.slope_stderr_ppm is not None and 0 < f.slope_stderr_ppm < 10
    assert linear_fit([0, 1], [0, 1], y_in_ms=False).slope_stderr_ppm is None


def test_more_windows_are_added_without_overlapping_the_first_ones():
    from fstk.qc.sync import SYNC_WIN, more_sync_points, sync_sample_points

    gaps = [(590.0, 592.0), (1290.0, 1292.0)]
    have = sync_sample_points(gaps, 2000.0)
    extra = more_sync_points(gaps, 2000.0, have, want=6)
    assert extra and all(abs(e - h) >= SYNC_WIN for e in extra for h in have)
    assert all(not (g0 - SYNC_WIN < e < g1) for e in extra for g0, g1 in gaps)


def test_a_marginal_slope_is_measured_again_with_more_windows(monkeypatch):
    """The between-track gate re-measures when the slope sits at the limit, and decides on the larger fit."""
    import numpy as np

    from fstk.qc import sync

    rng = np.random.default_rng(5)
    source = np.abs(rng.normal(0, 1, 48000 * 700)).astype(np.float32) * (1 + np.sin(np.arange(48000 * 700) / 20000.0))

    def grab(sel, t):
        seg = source[int(t * sync.SYNC_SR): int(t * sync.SYNC_SR) + 60 * sync.SYNC_SR]
        if sel == "0:a:1":
            return seg
        k = round((0.05 + 27e-6 * t) * sync.SYNC_SR)
        return np.concatenate((np.zeros(k, np.float32), seg))[: len(seg)]

    r = sync.between_track_sync([20.0, 120.0, 220.0, 320.0], grab)
    assert r["slope"]["verdict"] in ("marginal", "pass") and r["pass"]
    more = sync.more_sync_points([], 700.0, [20.0, 120.0, 220.0, 320.0], want=3)
    r2 = sync.between_track_sync(sorted([20.0, 120.0, 220.0, 320.0, *more]), grab)
    assert len(r2["rows"]) > len(r["rows"])


def test_qc_stage_measures_more_windows_when_the_slope_is_marginal():
    import numpy as np

    from fstk.stages.qc import QcReport

    n = 48000 * 2000
    rng = np.random.default_rng(9)
    source = (np.abs(rng.normal(0, 1, n)) * (1 + np.sin(np.arange(n) / 30000.0))).astype(np.float32)
    calls: list[float] = []

    class FF:
        def pcm(self, path, sel, sr, start_s=0.0, duration_s=60.0, cancel=None):
            if sel == "0:a:0":
                calls.append(start_s)
            seg = source[int(start_s * sr): int(start_s * sr) + int(duration_s * sr)]
            if sel == "0:a:1":
                return seg
            k = round((0.05 + 25e-6 * start_s) * sr)               # 25 ppm: inside 30, but marginal
            return np.concatenate((np.zeros(k, np.float32), seg))[: len(seg)]

    class M:
        def require(self, key):
            return {"video.delta_s": 0.0, "video.duration_s": 2000.0}[key]

        def get(self, key):
            return [{"ld_black": [t, t + 2.0]} for t in (400.0, 800.0, 1200.0, 1600.0)]

    class Ctx:
        def tool(self, name):
            return FF()

        def report(self, *a, **k):
            pass

        cancel = None

    review: list = []
    measured: dict = {}
    gate = QcReport()._between_track_gate(Ctx(), M(), "x.mkv", measured, review)
    assert gate.status == "PASS", gate.detail
    assert "more windows" in gate.detail
    assert len(set(calls)) > 7 and len(gate.metrics["rows"]) == len(set(calls))
