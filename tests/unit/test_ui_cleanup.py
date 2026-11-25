"""The UI clean-up (owner 2026-11-25, project doc UI-ASSESSMENT): nothing lost, said once, in plain words."""
from __future__ import annotations

import json

import pytest

from fstk.workflow.progress import STATUS_WORDS, ProgressTracker, describe_job_state
from tests.unit.test_ui import EP, review_plan, services  # noqa: F401

PLAN = {"job": "delivery-ds9-s02e02-20261124t213752", "episode": "DS9:S02E02",
        "steps": [{"id": "ld-audio", "stage": "dvd-ld-audio", "after": []},
                  {"id": "retime", "stage": "dvd-retime", "after": ["ld-audio"]},
                  {"id": "encode", "stage": "encode-final", "after": ["retime"]}]}
STATE = {"job": PLAN["job"], "steps": {
    "ld-audio": {"status": "stale", "started_utc": "2026-11-25T07:52:00+00:00",
                 "finished_utc": "2026-11-25T07:53:04+00:00"},
    "retime": {"status": "failed", "issues": ["retimed 5.1 does not line up; see C:/x/report.json"],
               "started_utc": "2026-11-25T07:53:04+00:00", "finished_utc": "2026-11-25T07:55:24+00:00"},
    "encode": {"status": "cancelled"}}}


# ------------------------------------------------------------------ the tracker (no Qt)
def test_the_headline_names_the_step_that_failed_not_one_merely_out_of_date():
    t = ProgressTracker(PLAN)
    t.load_state(STATE)
    snap = t.snapshot()
    assert [s.id for s in snap.failed] == ["retime", "ld-audio"]              # failed before out of date
    assert snap.headline() == "Stopped at retime: it failed"
    assert "retime failed" in describe_job_state(STATE)


def test_saved_durations_and_steps_that_never_began():
    t = ProgressTracker(PLAN)
    t.load_state(STATE)
    assert t.steps["retime"].elapsed(0) == pytest.approx(140.0)               # from the saved start and finish
    assert t.steps["encode"].shown_status == "not-started" and STATUS_WORDS["not-started"] == "not started"
    t.on_event({"kind": "step-start", "step": "encode"})
    t.on_event({"kind": "step-cancelled", "step": "encode", "issues": ["cancelled by operator"]})
    assert t.steps["encode"].shown_status == "cancelled"                       # it did begin this time


# ------------------------------------------------------------------ the Progress panel
@pytest.mark.gui
def test_progress_panel_names_bars_times_and_row_actions(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from fstk.ui.progress_panel import ProgressPanel

    panel = ProgressPanel()
    qtbot.addWidget(panel)
    t = ProgressTracker(PLAN)
    t.load_state(STATE)
    panel.set_tracker(t)
    assert [panel.table.item(r, 0).text() for r in range(3)] == ["LD audio", "Retime 5.1", "Encode"]
    assert "(dvd-retime)" in panel.table.item(1, 0).toolTip()
    assert [panel.table.item(r, 1).text() for r in range(3)] == ["out of date", "failed", "not started"]
    assert panel.table.item(1, 3).text() == "2m 20s"
    assert all(b.isHidden() for b in panel.bars.values())                    # nothing running: no bars
    assert "Retime 5.1" in panel.headline.text() and "failed" in panel.headline.text()
    assert panel.pause_button.isHidden() and panel.cancel_button.isHidden()
    t.on_event({"kind": "step-start", "step": "encode"})
    t.on_event({"kind": "progress", "step": "encode", "fraction": 0.25})
    panel.set_running(True)
    panel.refresh()
    assert not panel.bars["encode"].isHidden() and panel.bars["encode"].value() == 250
    assert not panel.pause_button.isHidden() and "elapsed" in panel.detail.text()
    # right-click menu and double-click
    menu = panel.row_menu(1)
    texts = [a.text() for a in menu.actions() if a.text()]
    assert texts[:2] == ["Restart from here…", "Show the full message…"] and "Copy the message" in texts
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    panel.show_message(1)
    assert "does not line up" in shown[-1]
    panel.set_runs([("j2", "Delivery · 25 Nov"), ("j1", "Delivery · 24 Nov")], "j1")
    assert not panel.runs.isHidden() and panel.runs.currentData() == "j1"
    panel.set_runs([("j2", "Delivery · 25 Nov")], "j2")
    assert panel.runs.isHidden()                                              # one run: no picker


# ------------------------------------------------------------------ the main window
@pytest.mark.gui
def test_menus_run_button_and_theme(services, qtbot):  # noqa: F811
    from fstk.ui.app import MainWindow
    from fstk.ui.theme import PLUM

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    menus = [a.text().replace("&", "") for a in w.menuBar().actions()]
    assert menus == ["File", "Episode", "Run", "Help"]
    episode_menu = w.menuBar().actions()[1].menu()
    assert {a.text() for a in episode_menu.actions() if a.text()} >= {
        "Find or add episode…", "Choose DVD…", "EFM capture choice…", "Delivery A/V check…",
        "Hand off to another machine…", "Remove episode…"}
    # the fixture has a stack reference (imported from v38): the Run button offers delivery
    assert w.main_run.defaultAction() is w.deliver_button
    services.intake_done = lambda ep: False
    w.show_episode(str(EP))
    assert w.main_run.defaultAction() is w.intake_button
    assert PLUM in w.window().styleSheet() or PLUM in __import__("PySide6.QtWidgets").QtWidgets.QApplication \
        .instance().styleSheet()
    assert w.main_run.objectName() == "primary"


@pytest.mark.gui
def test_log_strip_banner_actions_and_episode_status(services, tmp_path, qtbot):  # noqa: F811
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.log.isHidden() and not w.log_strip.isHidden()                  # the log starts as one line
    w.append_log("hello")
    assert w.log_last.text() == "hello"
    w.append_log("WARNING: something odd")
    w.log_problems.setChecked(True)
    assert "hello" not in w.log.toPlainText() and "something odd" in w.log.toPlainText()
    w.log_problems.setChecked(False)
    # a failed step: the banner says which and offers to show it / restart it; the log opens
    w.on_failed("the worker stopped")
    assert not w.log.isHidden()
    w.set_log_open(False)
    ws = services.workspace(EP)
    job_dir = ws.job_dir("delivery-ds9-s01e20-20261125t000000")
    job_dir.mkdir(parents=True)
    plan = {"job": job_dir.name, "episode": str(EP), "schema": "fstk-plan-v1",
            "steps": [{"id": "retime", "stage": "dvd-retime", "after": [], "params": {}}]}
    (job_dir / "plan.json").write_text(json.dumps(plan))
    (job_dir / "state.json").write_text(json.dumps({"job": job_dir.name, "created_utc": "2026-11-25T00:00:00+00:00",
                                                    "steps": {"retime": STATE["steps"]["retime"]}}))
    w.banner.setVisible(False)
    w.show_episode(str(EP))
    assert w.banner.text().startswith("Retime 5.1 failed: retimed 5.1 does not line up")
    assert "see C:" not in w.banner.text()                                    # the path is in Show details
    labels = [w.banner.buttons.itemAt(i).widget().text() for i in range(w.banner.buttons.count())]
    assert labels == ["Show details", "Restart from here"]
    assert "Retime 5.1 failed" in w.episodes.item(0).text()


@pytest.mark.gui
def test_details_tab_groups_and_hides_empty_fields(services, qtbot):  # noqa: F811
    from fstk.ui.app import MainWindow
    from fstk.ui.details import readable

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    d = w.details
    assert readable("stack.fell_back_to_mean") == "Fell back to mean" and readable("audio.dvd_clock") == "DVD clock"
    shown = d.rowCount()
    d.show_empty.setChecked(True)
    assert d.rowCount() > shown
    heads = [d.table.item(r, 0).text() for r in range(d.table.rowCount()) if d.table.columnSpan(r, 0) > 1]
    assert "Stack" in heads and "Audio" in heads
    assert "stack.frames" in next(d.table.item(r, 0).toolTip() for r in range(d.table.rowCount())
                                  if d.table.item(r, 0).text() == "Frames")


@pytest.mark.gui
def test_reviews_empty_state_and_queue_hint(services, qtbot):  # noqa: F811
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.tabs.tabText(w.tabs.indexOf(w.review_page)) == "Reviews"
    assert w.review_controls.isHidden() and not w.review_empty.isHidden()
    assert "EFM capture choice" in w.review_other.text()
    assert w.queue_panel.table.isHidden() and not w.queue_panel.empty_hint.isHidden()


@pytest.mark.gui
def test_dvd_dialog_leads_with_the_recorded_file(services, tmp_path, qtbot):  # noqa: F811
    from fstk.ui.dvd import DvdDialog

    rip = tmp_path / "DS9 S01E20.mkv"
    rip.write_bytes(b"x" * 2048)
    services.set_dvd(EP, rip, "op")
    dlg = DvdDialog(services, str(EP), "op")
    qtbot.addWidget(dlg)
    assert str(rip) in dlg.state.text() and "reachable" in dlg.facts.text()
    assert dlg.chooser.isHidden() and dlg.change_button.text() == "Change DVD…"
    dlg.change_button.click()
    assert not dlg.chooser.isHidden()


# ------------------------------------------------------------------ the finished file (owner, 2026-11-25)
DELIVERY = {"job": "delivery-ds9-s01e20-20261125t120000", "episode": "DS9:S01E20",
            "steps": [{"id": "remux", "stage": "remux", "after": []},
                      {"id": "qc", "stage": "qc-report", "after": ["remux"]}]}


@pytest.mark.gui
def test_a_delivery_run_ends_with_a_finished_file_row_linking_the_file(qtbot, tmp_path):
    from fstk.ui.progress_panel import ProgressPanel

    final = tmp_path / "S01E20-23.976-final-output.mkv"
    panel = ProgressPanel()
    qtbot.addWidget(panel)
    panel.final_file_source = lambda: final
    t = ProgressTracker(DELIVERY)
    t.load_state({"job": DELIVERY["job"], "steps": {"remux": {"status": "pending"}, "qc": {"status": "pending"}}})
    panel.set_tracker(t)
    row = panel.final_row
    assert row == 2 and panel.table.item(row, 0).text() == "Finished file"
    assert panel.table.item(row, 1).text() == "not made yet" and panel.final_link.text() == ""
    assert panel.step_at(row) is None and panel.row_menu(row) is None      # not a step: no step actions
    final.write_bytes(b"x")
    panel.update_final_file()
    assert panel.table.item(row, 1).text() == "made"
    assert final.name in panel.final_link.text() and "show in folder" in panel.final_link.text()
    assert str(final) in panel.final_link.toolTip()
    shown = []
    panel.reveal_file.connect(shown.append)
    panel._final_link_clicked("folder")
    assert shown == [str(final)]
    t.load_state({"job": DELIVERY["job"], "steps": {"remux": {"status": "passed"}, "qc": {"status": "passed"}}})
    panel.refresh()
    assert panel.table.item(row, 1).text() == "✓ ready"
    # a run without a remux (intake) has no such row
    panel.set_tracker(ProgressTracker(PLAN))
    assert panel.final_row is None and panel.table.rowCount() == 3


@pytest.mark.gui
def test_the_end_of_a_delivery_logs_and_opens_the_final_file(services, qtbot, tmp_path, monkeypatch):  # noqa: F811
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    final = tmp_path / "S01E20-23.976-final-output.mkv"
    final.write_bytes(b"x")
    monkeypatch.setattr(type(w.services), "final_file", lambda self, e: final)
    opened = []
    monkeypatch.setattr(MainWindow, "reveal_file", lambda self, p: opened.append(p))
    monkeypatch.setattr(MainWindow, "episode", property(lambda self: "DS9:S01E20"))
    w.tracker = ProgressTracker(DELIVERY)
    w.on_finished({"job": DELIVERY["job"], "steps": {"remux": {"status": "passed"}, "qc": {"status": "passed"}}})
    assert opened == [final]
    assert any(f"final output: {final}" in line for line in w.log.toPlainText().splitlines())
    assert "final-output.mkv" in w.banner.text()
