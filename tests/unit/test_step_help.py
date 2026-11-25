"""Progress tab: a tooltip for every step and columns the person can size (owner request 2026-11-24)."""
from __future__ import annotations

import json

import pytest

import fstk.stages  # noqa: F401
from fstk.stages.base import REGISTRY
from fstk.ui.step_help import STATUS_HELP, STEP_HELP, step_tooltip
from fstk.workflow.progress import ProgressTracker


def test_every_stage_has_plain_language_help():
    real = {k for k in REGISTRY if not k.startswith("t-")}           # t-* are the engine tests' stages
    assert real <= set(STEP_HELP), sorted(real - set(STEP_HELP))
    for what, result, _review in STEP_HELP.values():
        assert len(what) > 40 and result
    tip = step_tooltip("dvd-alignment", "DVD picture alignment")
    assert "<b>DVD alignment</b>" in tip and "(dvd-alignment)" in tip and "Your part:" in tip and "review" in tip
    assert "none, it runs on its own" in step_tooltip("remux", "Remux")


def test_retired_pad_steps_are_not_described():
    assert not {"pad-audit", "pad-repair"} & set(STEP_HELP)
    assert "RIFE" not in json.dumps(STEP_HELP)


PLAN = {"steps": [{"id": "stack-render", "stage": "stack-render"}, {"id": "cadence", "stage": "cadence-evidence"},
                  {"id": "video-quality", "stage": "video-quality"}]}


@pytest.mark.gui
def test_each_row_explains_its_step_and_the_columns_can_be_resized_and_remembered(qtbot):
    from PySide6.QtWidgets import QHeaderView

    from fstk.ui.progress_panel import ProgressPanel

    panel = ProgressPanel()
    qtbot.addWidget(panel)
    panel.set_tracker(ProgressTracker(PLAN))
    assert "Every step of the plan" not in panel.table.toolTip()   # cells carry their own tips now
    name = panel.table.item(1, 0)
    assert name.text() == "Film cadence" and "film cadence" in name.toolTip().lower()
    assert "Render weighted stack" in panel.table.item(0, 0).toolTip()
    assert panel.bars["video-quality"].toolTip() == panel.table.item(2, 0).toolTip()
    assert panel.table.item(0, 1).toolTip() == STATUS_HELP["pending"]
    head = panel.table.horizontalHeader()
    assert all(head.sectionResizeMode(c) == QHeaderView.ResizeMode.Interactive for c in range(len(panel.COLUMNS)))
    assert "Drag" in panel.table.horizontalHeaderItem(0).toolTip()

    got = []
    panel.columns_resized.connect(got.append)
    panel.table.setColumnWidth(0, 210)                   # as if the person dragged the edge
    qtbot.waitUntil(lambda: bool(got), timeout=3000)
    assert got[-1]["Step"] == 210 and "What it is doing" not in got[-1]   # the last column fills the rest

    again = ProgressPanel()
    qtbot.addWidget(again)
    again.set_column_widths({"Step": 210, "Status": 5, "Nope": 99})
    assert again.table.columnWidth(0) == 210 and again.table.columnWidth(1) == ProgressPanel.WIDTHS["Status"]


def test_column_widths_are_an_app_setting(tmp_path):
    from fstk.config.profile import SCHEMA, Profile
    from fstk.ui.services import Services

    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 1}})
    prof.path = tmp_path / "machine.json"
    services = Services(prof)
    services.save_app_prefs({"progress_columns": {"Step": 210}})
    assert services.app_prefs()["progress_columns"] == {"Step": 210}
    with pytest.raises(Exception, match="mapping"):
        services.save_app_prefs({"progress_columns": [1, 2]})
