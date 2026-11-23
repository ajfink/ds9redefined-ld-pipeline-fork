"""Per-episode DVD choice: the shared workspace function, the Services wrappers and the GUI dialog."""
from __future__ import annotations

import pytest

from fstk.core.errors import FstkError
from fstk.ui.tips import missing_tips
from fstk.workspace.dvd import dvd_status, find_dvd, record_dvd
from tests.unit.test_ui import EP, services  # noqa: F401  (fixture)


def _rips(tmp_path):
    root = tmp_path / "Video"
    (root / "D5").mkdir(parents=True)
    hit = root / "D5" / "DS9 S01E20 - In The Hands of the Prophets.mkv"
    other = root / "D5" / "DS9 S01E17 - The Forsaken.mkv"
    odd = root / "D5" / "Title 7.m2ts"
    for f in (hit, other, odd):
        f.write_bytes(b"x")
    (root / "D5" / "notes.txt").write_text("not video")
    return root, hit, other, odd


def test_find_dvd_by_code_and_by_any_video(tmp_path):
    root, hit, other, odd = _rips(tmp_path)
    assert find_dvd(root, "S01E20") == [hit]
    assert find_dvd(root, "s01 e20") == [hit]
    assert sorted(find_dvd(root, "S01E20", any_video=True)) == sorted([hit, other, odd])
    assert find_dvd(tmp_path / "missing", "S01E20") == []


def test_record_dvd_states(services, tmp_path):  # noqa: F811
    root, hit, *_ = _rips(tmp_path)
    store = services.workspace(EP).store
    assert dvd_status(store.load())["state"] == "unfiled"          # the v38 import says "available", names no file
    assert record_dvd(store, "op", hit) == str(hit)
    assert dvd_status(store.load()) == {"state": "set", "path": str(hit)}
    assert record_dvd(store, "op", None) is None
    assert dvd_status(store.load())["state"] == "none"
    with pytest.raises(FstkError):
        record_dvd(store, "op", tmp_path / "nope.mkv")
    assert dvd_status(store.load())["state"] == "none"          # a failed record changes nothing


def test_services_dvd_search_uses_profile_root(services, tmp_path):  # noqa: F811
    root, hit, *_ = _rips(tmp_path)
    assert services.dvd_search(EP) == []                        # no dvd_root configured
    services.profile.sources = {"dvd_root": str(root)}
    assert services.dvd_search(EP) == [hit]
    assert services.dvd_search("DS9:S01E20", tmp_path / "elsewhere") == []
    services.set_dvd(EP, hit, "op")
    assert services.dvd_status(EP)["state"] == "set"


@pytest.mark.gui
def test_dvd_dialog_finds_records_and_declines(services, tmp_path, qtbot):  # noqa: F811
    from fstk.ui.dvd import DvdDialog

    root, hit, other, odd = _rips(tmp_path)
    services.profile.sources = {"dvd_root": str(root)}
    dlg = DvdDialog(services, "DS9:S01E20", "op")
    qtbot.addWidget(dlg)
    assert not missing_tips(dlg)
    assert "no file is recorded" in dlg.state.text()
    assert dlg.found.count() == 1 and dlg.found.item(0).text() == str(hit)
    dlg.use_selected()
    assert dlg.changed and str(hit) in dlg.state.text()
    assert services.dvd_status(EP) == {"state": "set", "path": str(hit)}
    dlg.every.setChecked(True)
    assert dlg.found.count() == 3
    dlg.no_dvd()
    assert "no DVD" in dlg.state.text() and services.dvd_status(EP)["state"] == "none"
    dlg.folder.setText(str(tmp_path / "gone"))
    dlg.search()
    assert "cannot be reached" in dlg.status.text() and dlg.found.count() == 0


@pytest.mark.gui
def test_dvd_dialog_without_a_folder_and_main_window_button(services, qtbot):  # noqa: F811
    from fstk.ui.app import MainWindow
    from fstk.ui.dvd import DvdDialog

    dlg = DvdDialog(services, "DS9:S01E20", "op")
    qtbot.addWidget(dlg)
    assert "No DVD folder is set" in dlg.status.text()
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.dvd_button.text() == "DVD…" and w.dvd_button.toolTip()
