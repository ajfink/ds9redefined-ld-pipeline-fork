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
    assert 0 < w.values.rowCount() < 114                                     # set fields only, by default
    w.details.show_empty.setChecked(True)
    assert w.values.rowCount() > 60                                          # every declared field
    w.details.search.setText("frames")
    assert 0 < w.values.rowCount() < 20
    w.start_plan(str(review_plan(tmp_path)))
    qtbot.waitUntil(lambda: w.thread is None, timeout=10000)
    assert w.tabs.tabText(w.tabs.indexOf(w.review_page)) == "Reviews (1)"
    assert "step-awaiting-review" in w.log.toPlainText()


@pytest.mark.gui
def test_progress_panel_follows_a_run_and_persists(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.progress_panel.table.rowCount() == 0 and w.progress_panel.runs.isHidden()
    w.start_plan(str(review_plan(tmp_path)))
    assert w.tracker is not None and w.progress_panel.table.rowCount() == 1
    assert w.pause_button.isEnabled() and "%" in w.windowTitle()
    qtbot.waitUntil(lambda: w.thread is None, timeout=10000)
    panel = w.progress_panel
    assert panel.table.item(0, 1).text() == "needs you"
    assert "waiting for you" in panel.headline.text().lower()
    assert not w.pause_button.isEnabled() and w.pause_button.isHidden()       # Pause/Cancel only while running
    assert "waiting for you" in w.banner.text().lower() and not w.banner.isHidden()
    # reopening the app later: the saved job state is shown without running anything
    w2 = MainWindow(services, actor="op")
    qtbot.addWidget(w2)
    assert "waiting for you" in w2.progress_panel.headline.text().lower()
    assert w2.progress_panel.table.rowCount() == 1
    assert "needs you" in w2.episodes.item(0).text()                         # the episode list says so too


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
def test_dead_runs_show_interrupted_not_running(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    plan = review_plan(tmp_path)
    services.run_plan(plan, actor="op")
    state_path = services.workspace("DS9:S01E20").job_dir("rv") / "state.json"
    state = json.loads(state_path.read_text())
    state["steps"]["r"]["status"] = "running"            # as left behind by a closed app
    state_path.write_text(json.dumps(state))
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.progress_panel.table.item(0, 1).text() == "interrupted"
    assert "interrupted" in w.progress_panel.headline.text()


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
    from fstk.core.timeutil import utc_now_iso

    lock.write_text(f"pid={os.getpid()} host={socket.gethostname()} started_utc={utc_now_iso()} purpose=x\n")
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
    monkeypatch.setattr(w, "start_plan", lambda path: started.append(path) or True)
    monkeypatch.setattr(w, "ask_redo", lambda name: False)
    w.decide("REJECTED")
    assert started == [] and "redone the next time" in w.banner.text()       # "Later": nothing restarts now
    w.reviews.setCurrentRow(0)                                                # the refresh cleared the selection
    w.decide("APPROVED")
    assert len(started) == 1 and started[0].endswith("plan.json")             # the saved plan of the job
    assert "continuing" in w.banner.text() and not w.banner.isHidden()             # the main window says so
    started.clear()
    w._running_plan = str(tmp_path / "plans" / "rv" / "plan.json")             # this job is running: it picks it up
    monkeypatch.setattr(w.services, "job_plan_path", lambda ep, job: tmp_path / "plans" / job / "plan.json")
    w.thread = object()
    assert w.continue_job("rv") is True and started == [] and "picks it up" in w.banner.text()
    w.thread = None


@pytest.mark.gui
def test_work_started_during_another_run_is_queued_not_dropped(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show_episode("S01E20")
    w.thread, w._running_plan = object(), "other/plan.json"                  # another job is running
    assert w.start_plan(str(review_plan(tmp_path))) is False
    assert w._queued_plans == [str(review_plan(tmp_path))]
    w.thread, w._running_plan = None, None
    w._done()                                                                 # the other run ends …
    qtbot.waitUntil(lambda: w.thread is None and not w._queued_plans and "rv" in services.jobs("S01E20"),
                    timeout=20000)                                            # … and the queued one ran
    assert services.job_state("S01E20", "rv")["steps"]["r"]["status"] == "awaiting-review"
    qtbot.waitUntil(lambda: "Waiting for you" in w.banner.text(), timeout=5000)
    assert w.tabs.currentWidget() is w.review_page and w.reviews.currentRow() == 0


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


@pytest.mark.gui
def test_delivery_window_approval_carries_the_job_on(services, tmp_path, qtbot, monkeypatch):
    """The delivery A/V window's own "Approve delivery" must continue the job like the Reviews-tab button."""
    import dataclasses
    import json

    from fstk.ui.app import MainWindow

    services.run_plan(review_plan(tmp_path), actor="op")
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show_episode("S01E20")
    waiting = [dataclasses.replace(p, review="delivery") for p in services.pending_reviews("S01E20")]
    assert waiting
    monkeypatch.setattr(services, "pending_reviews", lambda episode: waiting)
    started = []
    monkeypatch.setattr(w, "start_plan", lambda path: started.append(path) or True)
    for decision, expect in (("REJECTED", 0), ("APPROVED", 1)):
        f = tmp_path / f"{decision}.json"
        f.write_text(json.dumps({"decision": decision}), encoding="utf-8")
        w.on_delivery_decided(str(f))
        assert len(started) == expect
    assert started[0].endswith("plan.json")


def test_only_the_newest_job_s_review_is_offered(services, tmp_path):
    """Two jobs waiting on the same review: the older one's evidence is out of date, so only the newest is listed."""
    import json as _json

    old = review_plan(tmp_path)
    services.run_plan(old, actor="op")
    newer = tmp_path / "plan2.json"
    newer.write_text(_json.dumps({**_json.loads(old.read_text()), "job": "rw"}))
    services.run_plan(newer, actor="op")
    assert {j for j in services.jobs("S01E20")} >= {"rv", "rw"}
    pending = services.pending_reviews("S01E20")
    assert [p.job for p in pending] == ["rw"]


@pytest.mark.gui
def test_reviews_tab_shows_the_stage_pictures_not_a_file_list(services, tmp_path, qtbot):
    from PIL import Image

    from fstk.ui.app import MainWindow
    from fstk.ui.services import PendingReview

    sheet = tmp_path / "sheet.png"
    Image.new("RGB", (500, 400), (40, 90, 160)).save(sheet)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"review_summary": "two spots", "review_view": {
        "verdict": "LOOKS GOOD", "tone": "good", "headline": "All 2 picture spot checks agree.", "duration_s": 100.0,
        "timeline": [{"start_s": 0, "end_s": 100, "tone": "info", "label": "act 1"}], "sheet": "sheet.png",
        "how_to_look": "Press Blink.",
        "spots": [{"time_s": 10, "label": "act 1: start", "verdict": "agrees", "tone": "good",
                   "views": [{"name": "LaserDisc", "rect": [0, 0, 240, 180]}, {"name": "DVD", "rect": [250, 0, 240, 180]}]},
                  {"time_s": 60, "label": "act 1: middle", "verdict": "agrees", "tone": "good",
                   "views": [{"name": "LaserDisc", "rect": [0, 200, 240, 180]}, {"name": "DVD", "rect": [250, 200, 240, 180]}]}]}}))
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w._pending = [PendingReview("j", "dvd-alignment", "dvd-alignment", "dvd-alignment", (str(report), str(sheet)), "")]
    w.show_review(0)
    assert "LOOKS GOOD" in w.review_headline.text() and "DVD alignment" in w.review_headline.text()
    assert len(w.review_spots.spots) == 2 and w.review_spots.thumbs.count() == 2
    assert [t["time_s"] for t in w.review_timeline.ticks] == [10, 60] and not w.review_timeline.isHidden()
    assert w.review_detail.isHidden() and w.review_scroll.isHidden()        # no file list, no raw sheet
    w.review_timeline.tick_clicked.emit(1)                                    # clicking the marker shows that spot
    assert w.review_spots.index == 1 and w.review_timeline.selected_key == 1
    w.review_spots.blink.setChecked(True)
    view = w.review_spots.view
    w.review_spots._flip()
    assert w.review_spots.view != view


def test_run_again_resumes_the_unfinished_job_furthest_along(services, tmp_path, monkeypatch):
    """PC-A 2026-11-23: Run to delivery after a failure must not start a new job that redoes finished steps."""
    from fstk.core.fsio import atomic_write_json

    ws = services.workspace("S01E20")
    steps = [{"id": "a", "stage": "t-measure", "after": [], "params": {"planned_gib": 1.0}},
             {"id": "b", "stage": "t-review", "after": ["a"], "params": {}}]

    def job(name, statuses, gib=1.0):
        plan = {"schema": "fstk-workflow-plan-v1", "episode": "DS9:S01E20", "job": name,
                "steps": [{**steps[0], "params": {"planned_gib": gib}}, steps[1]]}
        atomic_write_json(ws.job_dir(name) / "plan.json", plan)
        atomic_write_json(ws.job_dir(name) / "state.json",
                          {"steps": {sid: {"status": st} for sid, st in zip(("a", "b"), statuses, strict=True)}})

    job("delivery-ds9-s01e20-20261123t212519", ["passed", "failed"])          # furthest along
    job("delivery-ds9-s01e20-20261124t052905", ["interrupted", "pending"])    # newer, less done
    job("delivery-ds9-s01e20-20261120t000000", ["passed", "passed"])          # finished: never resumed
    new = {"job": "delivery-ds9-s01e20-20261124t060000",
           "steps": [{**steps[0], "params": {"planned_gib": 2.5}}, steps[1]]}  # estimate moved: still the same job
    found = services.resumable_job("S01E20", new)
    assert found is not None and found.parent.name == "delivery-ds9-s01e20-20261123t212519"
    monkeypatch.setattr(services, "job_runner", lambda ep, j: 4242 if j.endswith("212519") else None)
    assert services.resumable_job("S01E20", new).parent.name == "delivery-ds9-s01e20-20261124t052905"
    other = {"job": "delivery-ds9-s01e20-20261124t060000", "steps": steps[:1]}
    assert services.resumable_job("S01E20", other) is None                     # different steps: start fresh


@pytest.mark.gui
def test_an_update_while_the_app_is_open_is_noticed(services, qtbot, monkeypatch):
    """PC-A 2026-11-23: files synced under a running app; Folders… then crashed silently on mixed code."""
    from fstk.ui import app as app_mod
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.check_code_updated() is False
    monkeypatch.setattr(app_mod, "code_stamp", lambda: (999, 1))
    assert w.check_code_updated() is True
    assert "updated while it was open" in w.banner.text() and not w.code_watch.isActive()


@pytest.mark.gui
def test_rejecting_and_redoing_runs_the_step_again_and_asks_again(services, tmp_path, qtbot, monkeypatch):
    """Mac 2026-11-23: the alignment review had no pictures (extraction bug); after the fix the step must be redone,
    and a rejected step used to stay stuck for good."""
    from PySide6.QtWidgets import QInputDialog

    import tests.unit.test_engine as te
    from fstk.ui.app import MainWindow

    services.run_plan(review_plan(tmp_path), actor="op")
    runs = te.CALLS.get("review", 0)
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show_episode("S01E20")
    w.reviews.setCurrentRow(0)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("no pictures", True)))
    monkeypatch.setattr(w, "ask_redo", lambda name: True)
    w.decide("REJECTED")
    qtbot.waitUntil(lambda: w.thread is None and te.CALLS.get("review", 0) == runs + 1, timeout=20000)
    assert "redoing" in w.log.toPlainText() or "rejected" in w.banner.text()
    state = services.job_state("S01E20", "rv")
    assert state["steps"]["r"]["status"] == "awaiting-review"                 # redone once, then asks again
    _, state = services.run_plan(review_plan(tmp_path), actor="op")
    assert te.CALLS.get("review", 0) == runs + 1                             # not redone a second time


@pytest.mark.gui
def test_restart_from_here_icon_confirms_then_reruns(services, tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from fstk.ui.app import MainWindow
    from fstk.workflow.progress import ProgressTracker

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.select_episode(str(EP))
    plan = {"job": "jr", "episode": str(EP),
            "steps": [{"id": "a", "stage": "efm-decode"}, {"id": "b", "stage": "efm-select", "after": ["a"]},
                      {"id": "c", "stage": "stack-reference", "after": ["b"]}]}
    t = ProgressTracker(plan)
    w.tracker = t
    p = w.progress_panel
    p.set_tracker(t)
    for sid in ("a", "b"):
        t.on_event({"kind": "step-start", "step": sid})
    t.on_event({"kind": "step-passed", "step": "a"})
    t.on_event({"kind": "step-failed", "step": "b", "issues": ["x"]})
    t.on_event({"kind": "step-blocked", "step": "c", "issues": ["upstream b has not passed"]})
    p.refresh()
    assert not p.restart_buttons["a"].isHidden() and not p.restart_buttons["b"].isHidden()
    assert p.restart_buttons["c"].isHidden()              # c cannot start: b, which it needs, has not passed
    assert "Restart from here" in p.restart_buttons["a"].toolTip()
    p.set_allow_restart(False)                            # a run is going: no restarts
    assert p.restart_buttons["a"].isHidden()
    p.set_allow_restart(True)

    seen, calls, started = [], [], []

    def answer(button_text):
        def exec_(box):
            seen.append((box.text(), [b.text() for b in box.buttons()]))
            box.clickedButton = lambda: next(b for b in box.buttons() if b.text() == button_text)
            return 0
        return exec_

    services.restart_from = lambda ep, job, sid, actor: calls.append((ep, job, sid, actor)) or (Path("p.json"), [sid])
    w.start_plan = lambda path: started.append(path)
    monkeypatch.setattr(QMessageBox, "exec", answer("Cancel"))
    p.restart_buttons["a"].click()
    assert seen[-1][0] == "Are you sure?  This will overwrite previous work."
    assert sorted(seen[-1][1]) == ["Cancel", "Continue"] and calls == [] and started == []
    monkeypatch.setattr(QMessageBox, "exec", answer("Continue"))
    p.restart_buttons["a"].click()
    assert calls == [(str(EP), "jr", "a", "op")] and started == ["p.json"]
