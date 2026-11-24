"""Every interactive control in every window has a hover tooltip, and the episode override flag tells the truth."""
from __future__ import annotations

import json

import pytest

from fstk.ui.tips import missing_tips
from tests.unit.test_review_windows import _delivery_env, efm_env  # noqa: F401  (fixture)
from tests.unit.test_ui import _settings_dialog, services  # noqa: F401  (fixture)

pytestmark = pytest.mark.gui


def _none_missing(widget, name):
    left = missing_tips(widget)
    assert not left, f"{name}: controls without a tooltip: {left}"


def test_main_window_and_dialogs_have_tooltips(services, qtbot):  # noqa: F811
    from fstk.ui.app import MainWindow
    from fstk.ui.folders import FoldersDialog
    from fstk.ui.remove import RemoveDialog

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show_episode("DS9:S01E20")
    _none_missing(w, "main window")
    for scope in (0, 1):
        dlg = _settings_dialog(services, "S01E20")
        qtbot.addWidget(dlg)
        dlg.scope.setCurrentIndex(scope)
        _none_missing(dlg, f"settings scope {scope}")
    f = FoldersDialog(services.profile)
    qtbot.addWidget(f)
    _none_missing(f, "folders")
    r = RemoveDialog(services, "DS9:S01E20")
    qtbot.addWidget(r)
    _none_missing(r, "remove")


def test_queue_and_picker_dialogs_have_tooltips(services, qtbot):  # noqa: F811
    from fstk.ui.localfolder import LocalFolderDialog
    from fstk.ui.queue_panel import AddSeveralDialog

    d = AddSeveralDialog([str(e) for e in services.episodes()], None)
    qtbot.addWidget(d)
    _none_missing(d, "add several")
    lf = LocalFolderDialog("", None)
    qtbot.addWidget(lf)
    _none_missing(lf, "local folder")


def test_review_windows_have_tooltips(efm_env, tmp_path, qtbot):  # noqa: F811
    from fstk.ui import reviews
    from fstk.ui.services import Services
    from tests.integration import test_efm_stages as efm

    prof, ws, ledger, _ = efm_env
    efm._stack(efm_env)
    efm.run(efm_env, [efm.DECODE], "d1", orc=efm.FakeOrc(quality={"L1": dict(concealed=5), "P1": dict(concealed=9)}))
    dlg = reviews.EfmCaptureReview(Services(prof), "DS9:S01E20")
    qtbot.addWidget(dlg)
    _none_missing(dlg, "EFM capture review")


def test_delivery_review_has_tooltips_including_row_widgets(tmp_path, qtbot):
    from fstk.ui import reviews
    from fstk.ui.services import Services
    from tests.integration import test_efm_stages as efm
    from tests.unit.test_review_windows import _with_stretches

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []},
                                     no_dvd=True)
    _with_stretches(prof, ws, tmp_path)
    reports = prof.root("reports", efm.EP)
    (reports / "episode-quality-20261122t000000.json").write_text(json.dumps(
        {"episode": "DS9:S01E20", "verdict": "REVIEW", "counts": {"PASS": 0, "NOTE": 0, "FLAG": 1, "FAIL": 0},
         "rows": [], "moments": [{"time_s": 10.01, "reason": "dropout", "score": 100}]}))
    with ws.store.transaction(by="t", reason="setup") as m:
        m.set("video.frames", 50000, by="t")
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    dlg._add_fix_from_moment(dlg.moments.item(0))
    assert len(dlg.stretch_cards) == 1
    _none_missing(dlg, "delivery review")
    assert dlg.fix_table.cellWidget(0, 2).toolTip() and dlg.stretch_cards[0].toolTip()


def test_override_flag_shows_only_for_an_episode_with_its_own_settings(services, qtbot):  # noqa: F811
    from fstk.config.output import override_lines
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.show()
    w.show_episode("DS9:S01E20")
    assert services.output_override_info("S01E20") is None and w.override_flag.isHidden()
    services.save_project_output("S01E20", "audio", {"stereo_codec": "opus", "stereo_bitrate": 192}, actor="op")
    info = services.output_override_info("S01E20")
    assert info and "audio" in info["groups"] and info["by"] == "op"
    w.show_episode("DS9:S01E20")
    assert not w.override_flag.isHidden() and "differ from the defaults" in w.override_flag.text()
    tip = w.override_flag.toolTip()
    assert "own output settings" in tip and "Audio:" in tip and "in the defaults" in tip
    assert override_lines(info)[0].startswith("This episode has its own output settings")
    services.save_project_output("S01E20", "audio", None, actor="op")             # back to the defaults
    w.show_episode("DS9:S01E20")
    assert w.override_flag.isHidden()
