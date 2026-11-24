from __future__ import annotations

import json
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.v38 import import_v38
from fstk.ui.services import Services
from fstk.workflow.engine import PLAN_SCHEMA, EpisodeWorkspace

FIX = Path(__file__).parents[1] / "fixtures" / "v38" / "manifests"
EP = EpisodeId.parse("DS9:S01E20")


@pytest.fixture
def services(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media"),
                              "limits": {"min_free_gb": 0.001}})
    EpisodeWorkspace(prof, EP).store.create(import_v38(json.loads((FIX / "S01E20.json").read_text())),
                                            by="t", reason="fixture")
    return Services(prof)


def review_plan(tmp_path) -> Path:
    import tests.unit.test_engine  # noqa: F401  (registers the t-* test stages)

    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"schema": PLAN_SCHEMA, "episode": "DS9:S01E20", "job": "rv",
                             "steps": [{"id": "r", "stage": "t-review", "params": {}, "after": []}]}))
    return p


def test_services_browse_and_review_flow(services, tmp_path):
    assert services.episodes() == [EP]
    rows = {r.key: r for r in services.manifest_rows("S01E20")}
    assert rows["stack.frames"].value == "83335" and rows["stack.frames"].origin == "v38"
    assert not rows["video.head_trim"].set
    _, state = services.run_plan(review_plan(tmp_path), actor="op")
    assert state["steps"]["r"]["status"] == "awaiting-review"
    pending = services.pending_reviews("S01E20")
    assert len(pending) == 1 and pending[0].review == "t-evidence" and pending[0].evidence
    services.decide("S01E20", pending[0], decision="APPROVED", note="checked the sheet", actor="op")
    _, state = services.run_plan(review_plan(tmp_path), actor="op")
    assert state["steps"]["r"]["status"] == "passed"
    assert services.pending_reviews("S01E20") == []


@pytest.mark.gui
def test_main_window_smoke(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.episodes.count() == 1 and w.title.text().startswith("DS9:S01E20")
    assert w.values.rowCount() > 60
    w.start_plan(str(review_plan(tmp_path)))
    qtbot.waitUntil(lambda: w.thread is None, timeout=10000)
    assert w.tabs.tabText(3) == "Reviews (1)"
    assert "step-awaiting-review" in w.log.toPlainText()


@pytest.mark.gui
def test_progress_panel_follows_a_run_and_persists(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.status_line.text() == "" and w.progress_panel.table.rowCount() == 0
    w.start_plan(str(review_plan(tmp_path)))
    assert w.tracker is not None and w.progress_panel.table.rowCount() == 1
    assert w.pause_button.isEnabled() and "%" in w.windowTitle()
    qtbot.waitUntil(lambda: w.thread is None, timeout=10000)
    panel = w.progress_panel
    assert panel.table.item(0, 1).text() == "needs your review"
    assert "waiting for you" in panel.headline.text().lower()
    assert not w.pause_button.isEnabled()
    # reopening the app later: the saved job state is shown without running anything
    w2 = MainWindow(services, actor="op")
    qtbot.addWidget(w2)
    assert "waiting for your review" in w2.status_line.text()
    assert w2.progress_panel.table.rowCount() == 1


@pytest.mark.gui
def test_pause_button_drives_the_engine(services, tmp_path, qtbot):
    from fstk.tools.process import PAUSE
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.start_plan(str(review_plan(tmp_path)))
    qtbot.waitUntil(lambda: (w.worker is not None and w.worker.engine is not None) or w.thread is None,
                    timeout=5000)
    if w.worker is not None and w.worker.engine is not None:
        w.toggle_pause()
        assert PAUSE.is_paused and w.pause_button.text() == "Resume"
        w.toggle_pause()
        assert not PAUSE.is_paused
    qtbot.waitUntil(lambda: w.thread is None, timeout=10000)
    assert not PAUSE.is_paused


@pytest.mark.gui
def test_dead_runs_show_pending_not_running(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    plan = review_plan(tmp_path)
    services.run_plan(plan, actor="op")
    state_path = services.workspace("DS9:S01E20").job_dir("rv") / "state.json"
    state = json.loads(state_path.read_text())
    state["steps"]["r"]["status"] = "running"            # as left behind by a closed app
    state_path.write_text(json.dumps(state))
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.jobs.item(0, 2).text() == "pending"
    assert w.progress_panel.table.item(0, 1).text() == "pending"


@pytest.mark.gui
def test_refresh_icon_appears_for_space_blocked_steps_and_applies_to_all(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow
    from fstk.workflow.progress import ProgressTracker

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    plan = {"job": "jx", "episode": "DS9:S01E20",
            "steps": [{"id": "a", "stage": "efm-decode"}, {"id": "b", "stage": "stack-reference"},
                      {"id": "c", "stage": "efm-select"}]}
    t = ProgressTracker(plan)
    calls = []
    services.recheck_space = lambda ep, job, **kw: calls.append((ep, job)) or {}
    w.tracker = t
    w.progress_panel.set_tracker(t)
    space = "SPACE FAIL | target=Z:\\x | free=88.7 GiB"
    t.on_event({"kind": "step-blocked", "step": "a", "issues": [space]})
    t.on_event({"kind": "step-blocked", "step": "b", "issues": [space]})
    t.on_event({"kind": "step-blocked", "step": "c", "issues": ["needs video.frames"]})
    w.progress_panel.refresh()
    p = w.progress_panel
    assert p.table.columnCount() == 6 and p.table.horizontalHeaderItem(5).text() == "What it is doing"
    assert not p.refresh_buttons["a"].isHidden() and not p.refresh_buttons["b"].isHidden()
    assert p.refresh_buttons["c"].isHidden()
    got = []
    p.space_refresh.connect(lambda: got.append(1))
    p.refresh_buttons["b"].click()
    assert got == [1] and calls == [("DS9:S01E20", "jx")]     # one recheck covers every blocked step
    p.set_tracker(t)                    # (the app reloads saved state after an offline recheck)
    t.on_event({"kind": "step-unblocked", "step": "a"})
    t.on_event({"kind": "step-unblocked", "step": "b"})
    p.refresh()
    assert p.refresh_buttons["a"].isHidden() and p.refresh_buttons["b"].isHidden()
    assert p.table.item(0, 1).text() == "waiting"


def test_a_job_running_in_another_process_is_reported_not_edited(services, tmp_path):
    import os
    import socket

    import pytest as _pytest

    from fstk.core.errors import FstkError

    services.run_plan(review_plan(tmp_path), actor="op")
    lock = services.workspace("DS9:S01E20").job_dir("rv") / ".job.lock"
    assert services.job_runner("DS9:S01E20", "rv") is None
    lock.write_text(f"pid={os.getpid()} host={socket.gethostname()} started_utc=2026-01-01T00:00:00+00:00 purpose=x\n")
    assert services.job_runner("DS9:S01E20", "rv") == os.getpid()
    with _pytest.raises(FstkError, match="another FromStunToKill window"):
        services.recheck_space("DS9:S01E20", "rv")


def test_live_foreign_run_keeps_running_steps_running():
    from fstk.workflow.progress import ProgressTracker

    t = ProgressTracker({"steps": [{"id": "a", "stage": "capture-inventory"}]})
    t.load_state({"steps": {"a": {"status": "running"}}}, live=True)
    assert t.steps["a"].status == "running"
    t.load_state({"steps": {"a": {"status": "running"}}})
    assert t.steps["a"].status == "interrupted"


@pytest.mark.gui
def test_refresh_icons_stay_hidden_on_unblocked_rows_when_the_window_is_shown(services, qtbot):
    from fstk.ui.app import MainWindow
    from fstk.workflow.progress import ProgressTracker

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show()
    plan = {"job": "jx", "episode": "DS9:S01E20",
            "steps": [{"id": "a", "stage": "efm-decode"}, {"id": "b", "stage": "capture-inventory"}]}
    t = ProgressTracker(plan)
    w.tracker = t
    w.progress_panel.set_tracker(t)
    t.on_event({"kind": "step-blocked", "step": "a", "issues": ["SPACE FAIL | x"]})
    t.on_event({"kind": "step-passed", "step": "b"})
    w.progress_panel.refresh()
    qtbot.wait(200)                                     # let the table run its layout passes
    p = w.progress_panel
    assert p.refresh_buttons["a"].isVisible() and not p.refresh_buttons["b"].isVisible()


def test_closing_the_window_mid_run_asks_then_stops_the_run(services, monkeypatch):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QMessageBox

    from fstk.ui.app import MainWindow

    QApplication.instance() or QApplication([])
    win = MainWindow(services)
    idle = QCloseEvent()
    win.closeEvent(idle)
    assert idle.isAccepted()                                  # nothing running: closes straight away

    class FakeThread:
        def __init__(self):
            self.calls = []

        def quit(self):
            self.calls.append("quit")

        def wait(self, ms):
            self.calls.append("wait")
            return True

    class FakeEngine:
        def __init__(self):
            import threading

            self.cancel = threading.Event()
            self.resumed = False

        def resume(self):
            self.resumed = True

    win.thread, win.worker = FakeThread(), type("W", (), {"engine": FakeEngine()})()
    answers = iter(["Keep running", "Stop and close"])
    monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: next(b for b in self.buttons() if b.text() == next_answer[0]))
    next_answer = [next(answers)]
    keep = QCloseEvent()
    win.closeEvent(keep)
    assert not keep.isAccepted() and not win.worker.engine.cancel.is_set()
    next_answer[0] = next(answers)
    stop = QCloseEvent()
    win.closeEvent(stop)
    assert stop.isAccepted() and win.worker.engine.cancel.is_set() and win.thread.calls == ["quit", "wait"]
    assert QEvent  # (import kept for Qt type clarity)


def _settings_dialog(services, episode=None):
    from PySide6.QtWidgets import QApplication

    from fstk.ui.settings import SettingsDialog

    QApplication.instance() or QApplication([])
    services._encoders = {"libx265", "libx264", "libsvtav1", "ffv1"}
    return SettingsDialog(services, episode, None, actor="op")


def test_settings_defaults_scope_saves_to_the_profile(services, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(services.profile, "save", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"warning: {a[2]}"))
    dlg = _settings_dialog(services)
    v = dlg.video
    assert v.values()["encoder"] == "x265-10" and v.values()["quality"] == 14 and v.values()["tune"] == "grain"
    assert not v.tune.isEnabled()                                    # x265: grain is permanent
    v.encoder.setCurrentIndex(v.encoder.findData("av1-10"))
    assert v.values()["chroma"] == "420" and v.chroma.count() == 1      # SVT-AV1 is 4:2:0 only
    v.quality.setValue(v.quality.value() + 2)                          # two steps towards higher quality
    assert v.values()["quality"] == 18
    dlg.audio.compat.setChecked(False)
    dlg.operator.setText("Andy")
    dlg.save()
    assert services.profile.output["video"]["encoder"] == "av1-10"
    assert services.profile.output["audio"]["compat"] is False
    assert services.app_prefs()["operator"] == "Andy"


def test_settings_episode_scope_overrides_then_returns_to_defaults(services, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"warning: {a[2]}"))
    dlg = _settings_dialog(services, "S01E20")
    dlg.scope.setCurrentIndex(1)
    assert dlg.video.use_defaults.isChecked() and not dlg.pages["General"].isEnabled()
    dlg.audio.use_defaults.setChecked(False)
    codec, rate = dlg.audio.rows["stereo"]
    codec.setCurrentIndex(codec.findData("opus"))
    assert rate.isEnabled() and rate.currentData() == 192
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.No)
    dlg.save()
    own = services.project_output("S01E20")
    assert own["audio"]["stereo_codec"] == "opus" and "video" not in own
    assert asked == [1] and services.has_encode("S01E20")             # the imported episode is already encoded
    assert "Opus 192 kb/s" in services.output_summary("S01E20")["audio"]
    dlg = _settings_dialog(services, "S01E20")
    dlg.scope.setCurrentIndex(1)
    assert not dlg.audio.use_defaults.isChecked()
    dlg.audio.use_defaults.setChecked(True)
    dlg.save()
    assert services.project_output("S01E20") == {}


def test_reencode_plan_runs_only_encode_onwards(services):
    ws = services.workspace("S01E20")
    if not ws.store.load().artifact("padfixed_video"):
        with ws.store.transaction(by="t", reason="fixture") as m:
            m.set_artifact("padfixed_video", root="out", name="S01E20-23.976-padfixed-video.mkv")
    plan = json.loads(services.write_reencode_plan("S01E20").read_text())
    assert [s["stage"] for s in plan["steps"]] == ["encode-final", "remux", "qc-report", "episode-report"]


@pytest.mark.gui
def test_approving_a_review_carries_the_job_on(services, tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from fstk.ui.app import MainWindow

    services.run_plan(review_plan(tmp_path), actor="op")                     # stops at the review
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show_episode("S01E20")
    assert w.reviews.count() == 1
    w.reviews.setCurrentRow(0)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("looked fine", True)))
    started = []
    monkeypatch.setattr(w, "start_plan", lambda path: started.append(path))
    w.decide("REJECTED")
    assert started == []                                                      # a rejection never restarts anything
    w.reviews.setCurrentRow(0)                                                # the refresh cleared the selection
    w.decide("APPROVED")
    assert len(started) == 1 and started[0].endswith("plan.json")             # the saved plan of the job
    started.clear()
    w.thread = object()                                                       # a run is going: it picks the approval up
    assert w.continue_job("rv") is False and started == []
    w.thread = None


@pytest.mark.gui
def test_approval_really_resumes_and_finishes_the_job(services, tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from fstk.ui.app import MainWindow

    services.run_plan(review_plan(tmp_path), actor="op")
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show_episode("S01E20")
    w.reviews.setCurrentRow(0)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("looked fine", True)))
    w.decide("APPROVED")
    qtbot.waitUntil(lambda: w.thread is None, timeout=20000)
    assert services.job_state("S01E20", "rv")["steps"]["r"]["status"] == "passed"
    assert services.pending_reviews("S01E20") == []
