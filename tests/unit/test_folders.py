from __future__ import annotations

import json
import os

import pytest

from fstk.cli.main import main
from fstk.config.profile import (
    SCHEMA,
    Profile,
    change_folders,
    default_app_home,
    default_data_root,
    default_media_root,
)
from fstk.core.errors import ConfigError


def make(tmp_path) -> Profile:
    p = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                           "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media")})
    p.save(tmp_path / "machine.json")
    return p


def test_defaults_follow_fstk_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FSTK_HOME", str(tmp_path / "home"))
    assert default_app_home() == tmp_path / "home"
    assert default_data_root() == tmp_path / "home" / "data"
    assert default_media_root() == tmp_path / "home" / "work"
    monkeypatch.delenv("FSTK_HOME")
    assert default_app_home().name == "FromStunToKill"


def test_change_folders_moves_empty_roots_and_saves(tmp_path):
    p = make(tmp_path)
    new = tmp_path / "elsewhere"
    changes = change_folders(p, media_root=new / "work", roots={"out": str(new / "out")})
    assert {c[0] for c in changes} == {"media_root", "roots.out"}
    assert (new / "work").is_dir() and (new / "out").is_dir()
    again = Profile.load(tmp_path / "machine.json")
    assert again.media_root == new / "work" and again.roots["out"] == str(new / "out")
    # reset the per-kind override
    change_folders(again, roots={"out": None})
    assert "out" not in Profile.load(tmp_path / "machine.json").roots


def test_change_folders_refuses_when_work_exists(tmp_path):
    p = make(tmp_path)
    (tmp_path / "media" / "ds9-s01e20").mkdir(parents=True)
    (tmp_path / "media" / "ds9-s01e20" / "x.bin").write_bytes(b"1")
    with pytest.raises(ConfigError, match="work already exists"):
        change_folders(p, media_root=tmp_path / "new")
    assert p.media_root == tmp_path / "media"
    change_folders(p, media_root=tmp_path / "new", force=True)
    assert p.media_root == tmp_path / "new"


def test_change_folders_rejects_unknown_kind(tmp_path):
    with pytest.raises(ConfigError, match="unknown working-folder kind"):
        change_folders(make(tmp_path), roots={"nope": "/x"})


def test_cli_profile_folders(tmp_path, capsys, monkeypatch):
    make(tmp_path)
    monkeypatch.setenv("FSTK_PROFILE", str(tmp_path / "machine.json"))
    assert main(["profile", "folders"]) == 0
    assert str(tmp_path / "media") in capsys.readouterr().out
    assert main(["profile", "folders", "--media-root", str(tmp_path / "m2"),
                 "--root", f"out={tmp_path / 'fin'}"]) == 0
    doc = json.loads((tmp_path / "machine.json").read_text())
    assert doc["media_root"] == str(tmp_path / "m2") and doc["roots"]["out"] == str(tmp_path / "fin")


def test_profile_init_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FSTK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FSTK_PROFILE", str(tmp_path / "machine.json"))
    assert main(["profile", "init", "--machine", "T"]) == 0
    doc = json.loads((tmp_path / "machine.json").read_text())
    assert doc["data_root"] == str(tmp_path / "home" / "data")
    assert doc["media_root"] == str(tmp_path / "home" / "work")


def test_folders_dialog_saves(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from fstk.ui.folders import FoldersDialog

    QApplication.instance() or QApplication([])
    p = make(tmp_path)
    dlg = FoldersDialog(p)
    dlg.media.setText(str(tmp_path / "fast"))
    dlg.out.setText(str(tmp_path / "done"))
    dlg.save()
    assert p.media_root == tmp_path / "fast" and p.roots["out"] == str(tmp_path / "done")
    assert {c[0] for c in dlg.changed} == {"media_root", "roots.out"}
