"""Saving a DVD's opening pack: `opening_pack.export`, the service, and the Save icon beside dvd-probe."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.v38 import import_v38
from fstk.media import alignment as al
from fstk.media import opening_pack as op
from fstk.ui.services import Services
from fstk.workflow.engine import EpisodeWorkspace
from tests.unit.test_opening_pack import dvd, pictures

EP = EpisodeId.parse("DS9:S01E20")
FIX = Path(__file__).parents[1] / "fixtures" / "v38" / "manifests"


class FakeFF:
    def __init__(self, desc):
        self.desc, self.decodes = desc, 0

    def gray_thumbnails(self, path, w, h, *, cancel=None, max_frames=None):
        self.decodes += 1
        return self.desc[:max_frames].copy()

    def version(self):
        return "ffmpeg version test"


def test_export_reuses_descriptors_or_decodes(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"x")
    d = dvd()
    ff = FakeFF(d)
    res = op.export(ff, rip, series="DS9", code="S01E20", descriptors=d, out=tmp_path / "a.npz")
    assert res["written"] and not res["decoded"] and ff.decodes == 0
    assert op.load_pack(res["path"]).meta["source"] == {"file": "rip.mkv", "bytes": 1}
    res = op.export(ff, rip, series="DS9", code="S01E20", descriptors=d[:100], out=tmp_path / "b.npz")
    assert res["written"] and res["decoded"] and ff.decodes == 1
    assert op.load_pack(res["path"]).meta["ffmpeg"] == "ffmpeg version test"


def test_export_refuses_an_opening_that_repeats_unless_forced(tmp_path):
    rip = tmp_path / "rip.mkv"
    rip.write_bytes(b"x")
    run = pictures(round(14 * al.FPS), 3)
    d = np.vstack((np.full((40, al.FEATURES), 16, np.uint8), run, run, pictures(round(120 * al.FPS), 4)))
    res = op.export(FakeFF(d), rip, series="DS9", code="S01E20", out=tmp_path / "p.npz")
    assert not res["written"] and "would not pick out one place" in res["why"]
    assert not (tmp_path / "p.npz").exists()
    res = op.export(FakeFF(d), rip, series="DS9", code="S01E20", out=tmp_path / "p.npz", force=True)
    assert res["written"] and (tmp_path / "p.npz").is_file()


@pytest.fixture
def services(tmp_path, monkeypatch):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media"),
                              "limits": {"min_free_gb": 0.001}})
    EpisodeWorkspace(prof, EP).store.create(import_v38(json.loads((FIX / "S01E20.json").read_text())),
                                            by="t", reason="fixture")
    monkeypatch.setattr(op, "PACK_ROOT", tmp_path / "packs")
    from fstk.tools import ffmpeg

    ff = FakeFF(dvd())
    monkeypatch.setattr(ffmpeg.FFmpeg, "from_profile", classmethod(lambda cls, *a, **k: ff))
    svc = Services(prof)
    rip = tmp_path / "DS9 S01E20.mkv"
    rip.write_bytes(b"dvd")
    svc.set_dvd(EP, rip, "op")
    return svc


def test_service_saves_the_pack_where_the_target_says(services, tmp_path):
    target = services.opening_pack_target(EP)
    assert target == tmp_path / "packs" / "DS9" / "S01E20.npz"
    res = services.save_opening_pack(EP)
    assert res["written"] and res["path"] == target and res["decoded"]
    pack = op.load_pack(target)
    assert pack.opening_dvd_frame == 40 and pack.meta["code"] == "S01E20"


@pytest.mark.gui
def test_save_icon_beside_dvd_probe(services, tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from fstk.ui.app import MainWindow
    from fstk.workflow.progress import ProgressTracker

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    w.select_episode(str(EP))
    w.show_episode(str(EP))
    assert w.episode == str(EP)
    plan = {"job": "jx", "episode": str(EP),
            "steps": [{"id": "dvd-probe", "stage": "dvd-probe"}, {"id": "stack-render", "stage": "stack-render"}]}
    t = ProgressTracker(plan)
    w.tracker = t
    p = w.progress_panel
    p.set_tracker(t)
    btn = p.save_buttons["dvd-probe"]
    assert list(p.save_buttons) == ["dvd-probe"] and btn.isHidden()          # not probed yet
    t.on_event({"kind": "step-start", "step": "dvd-probe"})
    t.on_event({"kind": "step-passed", "step": "dvd-probe"})
    p.refresh()
    assert not btn.isHidden()
    target = tmp_path / "packs" / "DS9" / "S01E20.npz"
    tip = btn.toolTip()
    assert "Save this DVD's opening pack" in tip and str(target) in tip and "not watchable video" in tip
    btn.click()
    qtbot.waitUntil(lambda: w.pack_thread is None, timeout=10000)
    assert target.is_file()
    assert "replaces the pack already there" in btn.toolTip()           # the tip now says a pack is there
