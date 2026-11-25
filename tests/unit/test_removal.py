from __future__ import annotations

import json
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.manifest.v38 import import_v38
from fstk.ui.services import Services
from fstk.workflow.engine import EpisodeWorkspace

FIX = Path(__file__).parents[1] / "fixtures" / "v38" / "manifests" / "S01E20.json"
EP = EpisodeId.parse("DS9:S01E20")


def build(tmp_path, *, override_downloads: str | None = None, roots=None):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "data"),
                              "media_root": str(tmp_path / "media"), "roots": roots or {},
                              "sources": {"downloads_root": str(tmp_path / "dl")},
                              "limits": {"min_free_gb": 0.001}})
    m = import_v38(json.loads(FIX.read_text()))
    if override_downloads:
        m.doc["sources"]["downloads_override"] = override_downloads
    EpisodeWorkspace(prof, EP).store.create(m, by="t", reason="fixture")
    media = prof.episode_media_dir(EP)
    (media / "out").mkdir(parents=True)
    (media / "out" / "final.mkv").write_bytes(b"x" * 1000)
    dl = prof.downloads_dir(EP)
    dl.mkdir(parents=True)
    (dl / "a.tbc").write_bytes(b"y" * 500)
    return prof, Services(prof), media, dl


def test_project_only_keeps_every_file(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    svc.remove_episode("DS9:S01E20", "project")
    assert not prof.episode_state_dir(EP).exists() and svc.episodes() == []
    assert (media / "out" / "final.mkv").exists() and (dl / "a.tbc").exists()


def test_files_scope_deletes_working_media_but_not_the_download(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    plan = svc.removal_plan("DS9:S01E20")
    assert plan.total_bytes("files") >= 1000 and plan.total_bytes("all") >= plan.total_bytes("files") + 500
    svc.remove_episode("DS9:S01E20", "files")
    assert not media.exists() and not prof.episode_state_dir(EP).exists()
    assert (dl / "a.tbc").exists()


def test_everything_deletes_the_downloaded_episode(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    svc.remove_episode("DS9:S01E20", "all")
    assert not media.exists() and not dl.exists() and (tmp_path / "dl").exists()


def test_captures_from_a_chosen_folder_are_never_deleted(tmp_path):
    mine = tmp_path / "X" / "DS9-S01E20-LaserDisc-Copies"
    mine.mkdir(parents=True)
    (mine / "a.tbc").write_bytes(b"orig")
    prof, svc, media, dl = build(tmp_path, override_downloads=str(mine))
    plan = svc.removal_plan("DS9:S01E20")
    assert plan.captures == [] and "never deletes" in plan.captures_note
    svc.remove_episode("DS9:S01E20", "all")
    assert (mine / "a.tbc").read_bytes() == b"orig" and not media.exists()


def test_output_folder_override_is_included_and_safety_check_refuses_strays(tmp_path):
    prof, svc, media, dl = build(tmp_path, roots={"out": str(tmp_path / "deliveries")})
    out = prof.root("out", EP)
    out.mkdir(parents=True)
    (out / "final.mkv").write_bytes(b"z")
    svc.remove_episode("DS9:S01E20", "files")
    assert not out.exists() and (tmp_path / "deliveries").exists()      # only the episode's own folder goes


def test_safety_check_refuses_a_path_outside_its_base_and_deletes_nothing(tmp_path, monkeypatch):
    from fstk.workspace import removal

    prof, svc, media, dl = build(tmp_path)
    stray = tmp_path / "precious"
    stray.mkdir()
    real = removal.plan_removal

    def tampered(profile, episode, manifest=None):
        plan = real(profile, episode, manifest)
        plan.files.append(removal.Item("stray", stray, 0))
        return plan

    monkeypatch.setattr(removal, "plan_removal", tampered)
    with pytest.raises(FstkError, match="refusing"):
        svc.remove_episode("DS9:S01E20", "files")
    assert stray.exists() and media.exists() and svc.episodes() == [EP]


@pytest.mark.gui
def test_remove_dialog_flow(tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from fstk.ui.app import MainWindow
    from fstk.ui.remove import RemoveDialog

    prof, svc, media, dl = build(tmp_path)
    w = MainWindow(svc, actor="op")
    qtbot.addWidget(w)
    assert w.remove_button.text() == "Remove episode…"
    dlg = RemoveDialog(svc, "DS9:S01E20")
    qtbot.addWidget(dlg)
    assert dlg.parts == frozenset() and not dlg.remove_button.isEnabled()     # nothing ticked at first
    assert str(prof.episode_state_dir(EP)) in dlg.notes["project"].text()          # where, and how big
    assert str(media) in dlg.notes["files"].text() and str(dl) in dlg.notes["captures"].text()
    dlg.boxes["project"].setChecked(True)
    assert dlg.boxes["entry"].isChecked() and not dlg.boxes["entry"].isEnabled()  # the entry goes with the project
    dlg.boxes["files"].setChecked(True)
    assert dlg.parts == {"entry", "project", "files"} and "Total to free" in dlg.total.text()
    dlg.everything.setChecked(True)
    assert all(not b.isEnabled() and b.isChecked() for b in dlg.boxes.values())
    assert dlg.parts == {"entry", "project", "files", "captures"}
    dlg.everything.setChecked(False)
    dlg.boxes["captures"].setChecked(False)
    # Cancel at the "Are you sure?" box: nothing is deleted
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.escapeButton())
    dlg.confirm()
    assert media.exists() and svc.episodes() == [EP]
    # Confirm: deleted
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: next(b for b in self.buttons() if b.text() == "Remove"))
    dlg.confirm()
    assert not media.exists() and svc.episodes() == [] and dl.exists()


def test_parts_are_chosen_independently(tmp_path):
    prof, svc, media, dl = build(tmp_path)
    svc.remove_episode("DS9:S01E20", {"captures"})                  # only the download
    assert not dl.exists() and media.exists() and svc.episodes() == [EP]
    prof2, svc2, media2, dl2 = build(tmp_path / "b")
    svc2.remove_episode("DS9:S01E20", {"files", "captures"})        # files, but the project stays listed
    assert not media2.exists() and not dl2.exists() and svc2.episodes() == [EP]
    with pytest.raises(FstkError):
        svc2.remove_episode("DS9:S01E20", {"bogus"})
    with pytest.raises(FstkError):
        svc2.remove_episode("DS9:S01E20", set())


def test_removing_only_the_entry_keeps_everything_and_adding_it_again_brings_it_back(tmp_path, monkeypatch):
    from fstk.workspace.episodes import create_from_local, is_hidden

    prof, svc, media, dl = build(tmp_path)
    svc.remove_episode("DS9:S01E20", {"entry"})
    assert svc.episodes() == [] and is_hidden(prof, EP)
    assert prof.episode_state_dir(EP).exists() and media.exists() and dl.exists()
    import fstk.workspace.episodes as episodes

    monkeypatch.setattr(episodes, "inspect_local_source", lambda folder: {"series": "DS9", "episode": "S01E20"})
    create_from_local(prof, tmp_path, actor="t")                      # "add from folder" again
    assert svc.episodes() == [EP] and not is_hidden(prof, EP)


def test_a_chosen_capture_folder_is_shown_but_cannot_be_ticked(tmp_path, qtbot):
    from fstk.ui.remove import RemoveDialog

    mine = tmp_path / "X" / "DS9-S01E20-LaserDisc-Copies"
    mine.mkdir(parents=True)
    (mine / "a.tbc").write_bytes(b"orig")
    prof, svc, media, dl = build(tmp_path, override_downloads=str(mine))
    dlg = RemoveDialog(svc, "DS9:S01E20")
    qtbot.addWidget(dlg)
    assert not dlg.boxes["captures"].isEnabled()
    assert str(mine) in dlg.notes["captures"].text() and "never deletes" in dlg.notes["captures"].text()
    dlg.everything.setChecked(True)
    assert "captures" not in dlg.parts
