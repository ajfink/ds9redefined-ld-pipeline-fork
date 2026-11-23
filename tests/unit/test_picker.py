from __future__ import annotations

from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.ui.services import Services
from tests.integration.test_intake_flow import _local_folder

pytestmark = pytest.mark.gui


@pytest.fixture
def services(tmp_path):
    (tmp_path / "dl").mkdir()
    return Services(Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                                       "data_root": str(tmp_path / "d"), "media_root": str(tmp_path / "m"),
                                       "sources": {"downloads_root": str(tmp_path / "dl")}}))


def _find(tree, code, series="DS9"):
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if top.text(0) != series:
            continue
        for j in range(top.childCount()):
            season = top.child(j)
            for k in range(season.childCount()):
                if season.child(k).text(0) == code:
                    return season.child(k)
    return None


def test_picker_tree_search_and_create(services, qtbot):
    from fstk.ui.picker import EpisodePicker

    dlg = EpisodePicker(services, actor="op")
    qtbot.addWidget(dlg)
    assert dlg.tree.topLevelItemCount() == 3
    dlg.search.setText("DS9 season 1 episode 20")
    item = _find(dlg.tree, "S01E20")
    assert item is not None and item.text(1) == "In the Hands of the Prophets" and item.text(5) == "remote"
    dlg.tree.setCurrentItem(item)
    assert dlg.create_btn.isEnabled() and "918.5 GiB" in dlg.detail.toPlainText()
    dlg.accept_selection()
    assert dlg.chosen.canonical == "DS9:S01E20"
    assert services.episodes()[0].canonical == "DS9:S01E20"


def test_picker_blocks_lookup_only_and_emissary(services, qtbot):
    from fstk.ui.picker import EpisodePicker

    dlg = EpisodePicker(services, actor="op")
    qtbot.addWidget(dlg)
    dlg.search.setText("TNG S04E21")
    item = _find(dlg.tree, "S04E21", "TNG")
    dlg.tree.setCurrentItem(item)
    assert not dlg.create_btn.isEnabled() and "lookup-only" in dlg.detail.toPlainText()
    dlg.search.setText("DS9 S01E01")
    dlg.tree.setCurrentItem(_find(dlg.tree, "S01E01"))
    assert not dlg.create_btn.isEnabled() and "two-part" in dlg.detail.toPlainText()


def test_picker_local_folder_and_main_window_integration(services, qtbot, tmp_path):
    from fstk.ui.app import MainWindow
    from fstk.ui.picker import EpisodePicker

    dlg = EpisodePicker(services, actor="op")
    qtbot.addWidget(dlg)
    dlg.create_local(str(_local_folder(tmp_path)))
    assert dlg.chosen.canonical == "VOY:S01E01"
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.select_episode("VOY:S01E01")
    assert w.episode == "VOY:S01E01"
    plan_path = services.write_intake_plan("VOY:S01E01", include_stack_reference=False)
    w.start_plan(str(plan_path))
    qtbot.waitUntil(lambda: w.thread is None, timeout=15000)
    assert "inventory=passed" in w.log.toPlainText()
    dlg2 = EpisodePicker(services, actor="op")
    qtbot.addWidget(dlg2)
    dlg2.only_local.setChecked(True)
    dlg2.search.setText("VOY S01E01")
    assert _find(dlg2.tree, "S01E01", "VOY").text(5) == "workspace"


def test_local_folder_dialog_finds_episode_folders_below_a_share(tmp_path, qtbot):
    from fstk.ui.localfolder import LocalFolderDialog
    from fstk.workspace.local_source import find_capture_folders

    root = tmp_path / "share"
    ep = _local_folder(root)
    assert find_capture_folders(root) == [ep] or ep in find_capture_folders(root)
    dlg = LocalFolderDialog(str(root))
    qtbot.addWidget(dlg)
    dlg.scan()
    assert dlg.found.count() >= 1
    dlg.accept_selection()
    assert Path(dlg.chosen).is_dir()
    dlg2 = LocalFolderDialog(str(tmp_path / "nope"))
    qtbot.addWidget(dlg2)
    dlg2.scan()
    assert "cannot be reached" in dlg2.status.text()
