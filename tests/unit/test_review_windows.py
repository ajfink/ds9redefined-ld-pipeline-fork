from __future__ import annotations

import json
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.hashing import Fingerprint
from fstk.manifest.model import Manifest
from fstk.ui.services import Services
from fstk.workflow.engine import Engine, EpisodeWorkspace, load_plan
from tests.integration import test_efm_stages as efm

pytestmark = pytest.mark.gui


@pytest.fixture
def efm_env(tmp_path):
    return efm.make_env(tmp_path)


def test_efm_capture_review_manual_override(efm_env, qtbot, monkeypatch):
    from fstk.ui import reviews

    prof, ws, ledger, _ = efm_env
    efm._stack(efm_env)
    orc = efm.FakeOrc(quality={"L1": dict(concealed=5, muted=9), "P1": dict(concealed=99), "P6": dict(muted=4)})
    efm.run(efm_env, [efm.DECODE], "d1", orc=orc)
    services = Services(prof)
    dlg = reviews.EfmCaptureReview(services, "DS9:S01E20")
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 3 and dlg.data["auto"] == "P1"          # least-bad: no silenced samples
    assert dlg.auto_button.isEnabled() and dlg.choose_button.isEnabled()
    assert "least-audible-damage-v1" in dlg.summary.text()
    assert dlg.data["pads"] == {"P6": 0, "L1": 3, "P1": 12}
    warnings = []
    monkeypatch.setattr(reviews.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    dlg.table.selectRow(1)
    label = dlg.selected_capture()
    dlg.choose()                                            # no note → refused
    assert warnings and "at least 8" in warnings[0]
    plans = []
    dlg.plan_ready.connect(plans.append)
    dlg.note.setPlainText(f"{label} sounds cleanest across the act-two music cue")
    dlg.choose()
    (plan_path,) = plans
    state = Engine(prof, load_plan(plan_path), actor="op", ledger=ledger).run()
    assert state["steps"]["efm-select"]["status"] == "passed", state
    m = ws.store.load()
    assert m.get("audio.capture") == label and m.get("audio.capture_selection")["method"] == "operator"


def test_efm_capture_review_offers_automatic_choice(efm_env, qtbot):
    from fstk.ui import reviews

    prof, ws, ledger, _ = efm_env
    efm._stack(efm_env)
    efm.run(efm_env, [efm.DECODE], "d2", orc=efm.FakeOrc(quality={"L1": dict(concealed=50), "P1": dict(muted=3)}))
    dlg = reviews.EfmCaptureReview(Services(prof), "DS9:S01E20")
    qtbot.addWidget(dlg)
    assert dlg.data["auto"] == "P6" and dlg.auto_button.isEnabled()
    plans = []
    dlg.plan_ready.connect(plans.append)
    dlg.accept_auto()
    assert json.loads(Path(plans[0]).read_text())["steps"][0]["params"] == {}


def _delivery_env(tmp_path, summary, *, no_dvd=False):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    m = Manifest.new(efm.EP, title="t")
    if no_dvd:
        m.set("mode.dvd_reference_available", False, by="op")
    ws = EpisodeWorkspace(prof, efm.EP)
    ws.store.create(m, by="t", reason="setup")
    reports = prof.root("reports", efm.EP)
    reports.mkdir(parents=True)
    report = reports / "qc-20261121t000000z.json"
    report.write_text(json.dumps({
        "schema": "fstk-delivery-qc-v1", "episode": "DS9:S01E20", "deliverable_path": str(tmp_path / "x.mkv"),
        "results": [{"gate": "frame count", "status": "PASS", "detail": "1 == 1"},
                    {"gate": "full-decode + PAD black", "status": summary.get("pad", "PASS"), "detail": ""}],
        "summary": summary, "review_intervals": [{"id": "pad-black-f240", "gate": "full-decode + PAD black",
                                                  "start_s": 8.0, "end_s": 12.0, "reason": "PAD black"}],
        "av_check": {"chunks": [{"start_s": 30.0, "end_s": 45.0, "t_s": 37.5},
                                {"start_s": 600.0, "end_s": 615.0, "t_s": 607.5},
                                {"start_s": 1200.0, "end_s": 1215.0, "t_s": 1207.5}], "track": "ld"},
        "standing_warnings": ["validate artifacts, not exit codes"]}))
    job = ws.job_dir("qc-job")
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"steps": {"qc": {
        "status": "awaiting-review", "stage": "qc-report", "outputs": [Fingerprint.of(report).to_dict()]}}}))
    return prof, ws, report


def test_delivery_review_records_approval_only_for_a_clean_pending_report(tmp_path, qtbot):
    from fstk.ui import reviews

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []})
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    assert dlg.gates.rowCount() == 2 and dlg.intervals.count() == 1 and len(dlg.chunk_buttons) == 3
    assert not dlg.approve_button.isEnabled()                      # chunks not checked yet
    dlg.set_verdict(0, "ok")
    dlg.set_verdict(1, "ok")
    assert not dlg.approve_button.isEnabled() and not dlg.rerun_button.isEnabled()
    dlg.set_verdict(2, "ok")
    assert dlg.approve_button.isEnabled() and not dlg.rerun_button.isEnabled()
    path = dlg.decide("APPROVED")
    assert path and ws.approvals.status("delivery", manifest=ws.store.load()).state == "approved"
    assert "every chunk in sync" in json.loads(Path(path).read_text())["note"]


def test_delivery_review_rerun_writes_a_correction_plan(tmp_path, qtbot):
    from fstk.ui import reviews

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []},
                                     no_dvd=True)
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    plans = []
    dlg.plan_ready.connect(plans.append)
    dlg.set_verdict(0, "ok")
    dlg.set_verdict(1, "early")
    assert dlg.rerun_button.isEnabled() and not dlg.approve_button.isEnabled()
    dlg.rerun()
    (plan_path,) = plans
    steps = json.loads(Path(plan_path).read_text())["steps"]
    assert [s["stage"] for s in steps] == ["av-correction", "dvd-free-audio", "remux", "qc-report"]
    assert steps[0]["params"]["verdicts"] == [{"t_s": 37.5, "verdict": "ok"}, {"t_s": 607.5, "verdict": "early"}]
    status = ws.approvals.status("delivery", manifest=ws.store.load())
    assert status.state == "rejected"                              # this file is not delivered


def test_delivery_review_blocks_approval_with_unverified_gates(tmp_path, qtbot):
    from fstk.ui import reviews

    prof, _, _ = _delivery_env(tmp_path, {"pass": 1, "fail": 1, "failed_gates": [],
                                          "unverified_gates": ["replacement identity"]})
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    assert not dlg.approve_button.isEnabled() and dlg.reject_button.isEnabled()
    assert "cannot be approved" in dlg.summary.text() and "replacement identity" in dlg.summary.text()


def test_main_window_opens_review_windows(efm_env, qtbot):
    from fstk.ui.app import MainWindow

    prof, *_ = efm_env
    win = MainWindow(Services(prof), actor="op")
    qtbot.addWidget(win)
    win.select_episode("DS9:S01E20")
    efm_dlg = win.open_efm_review()
    assert efm_dlg is not None and "Not ready" in efm_dlg.summary.text()
    delivery = win.open_delivery_review()
    assert "No QC report yet" in delivery.summary.text()



def test_picker_shows_other_devices_and_adopts_a_handoff(tmp_path, qtbot, monkeypatch):
    from fstk.catalog.index import CatalogIndex
    from fstk.ui import picker as picker_mod
    from fstk.ui.app import MainWindow
    from tests.integration.test_shared_workspace import machine

    shared = tmp_path / "share"
    shared.mkdir()
    legion = machine(tmp_path, "LENOVO-LEGION-5", shared, tmp_path)
    mac = machine(tmp_path, "MAC-M5", shared, tmp_path)
    index = CatalogIndex.load()
    entry = index.find("VOY", 1, 1)
    lsvc, msvc = Services(legion), Services(mac)
    msvc.shared_tree()                                          # the Mac joins the shared folder
    lsvc.create_from_catalog(entry, actor="andrew@legion")
    lwin = MainWindow(lsvc, actor="andrew@legion")
    qtbot.addWidget(lwin)
    lwin.select_episode("VOY:S01E01")
    mac_device = msvc.shared_tree().device
    assert lwin.handoff_to(mac_device, "Mac continues with EFM")["to"] == mac_device
    assert "handed off" in lwin.title.text() and not lwin.run_button.isEnabled()
    dlg = picker_mod.EpisodePicker(msvc, actor="andrew@mac")
    qtbot.addWidget(dlg)
    dlg.search.setText("voyager S01E01")
    item = next(it for it in _items(dlg.tree) if it.text(0) == "S01E01" and it.data(0, picker_mod.ROLE_ENTRY))
    assert "LENOVO-LEGION-5 (handed-off)" in item.text(6)
    dlg.tree.setCurrentItem(item)
    assert dlg.adopt_btn.isEnabled() and "handed to this device" in dlg.detail.toPlainText()
    monkeypatch.setattr(picker_mod.QMessageBox, "information", lambda *a: None)
    dlg.adopt_selection()
    assert str(dlg.chosen) == "VOY:S01E01"
    assert EpisodeWorkspace(mac, dlg.chosen).store.load().title == entry.title


def _items(tree):
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop()
        yield it
        stack += [it.child(i) for i in range(it.childCount())]


def test_delivery_review_plays_the_h264_proxy_when_a_chunk_has_one(tmp_path, qtbot):
    from fstk.ui import reviews

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []})
    proxy = tmp_path / "chunk-01.mp4"
    proxy.write_bytes(b"x")
    doc = json.loads(report.read_text())
    doc["av_check"]["chunks"][0]["proxy"] = str(proxy)
    report.write_text(json.dumps(doc))
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    calls = []

    class FakeMedia:
        source = None

        def load(self, path, keep_position=True):
            calls.append(("load", path))
            self.source = path

        def seek_s(self, s):
            calls.append(("seek", s))

        def play(self):
            calls.append(("play",))

    dlg.media = FakeMedia()
    dlg.play_chunk(0)                      # has a proxy: that file, from its start
    assert calls[:2] == [("load", str(proxy)), ("seek", 0.0)]
    calls.clear()
    dlg.play_chunk(1)                      # no proxy: the deliverable at the chunk start
    assert ("seek", 600.0) in calls


def _with_stretches(prof, ws, tmp_path):
    from fstk.media import hybrid

    probe = prof.root("probe", efm.EP)
    probe.mkdir(parents=True, exist_ok=True)
    segs = [hybrid.Segment(hybrid.FILM, 0, 100), hybrid.Segment(hybrid.VIDEO_I, 100, 130),
            hybrid.Segment(hybrid.VIDEO_I, 200, 220, plate=True)]
    (probe / "hybrid-plan.json").write_text(json.dumps({"segments": [s.as_dict() for s in segs]}))
    reports = prof.root("reports", efm.EP)
    (reports / "video-trim-20261122t000000.json").write_text(json.dumps(
        {"video_stretches": hybrid.summary(segs), "repair": "qtgmc"}))
    clip = tmp_path / "stretch-100.mkv"
    clip.write_bytes(b"x")
    (reports / "stretch-preview-20261122t000001.json").write_text(json.dumps(
        {"previews": [{"first_cycle": 100, "clip": str(clip)}]}))
    with ws.store.transaction(by="t", reason="setup") as m:
        m.set_artifact("hybrid_plan", root="probe", name="hybrid-plan.json")
        m.set("video.head_trim", 40, by="t")
        m.set("video.frames", 50000, by="t")
    return clip


def test_delivery_review_stretch_choice_writes_a_fresh_encode_plan(tmp_path, qtbot, monkeypatch):
    from fstk.ui import reviews

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []},
                                     no_dvd=True)
    clip = _with_stretches(prof, ws, tmp_path)
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    assert len(dlg.stretch_cards) == 1                             # the film plate is not offered
    assert dlg.stretch_rows[0]["encoded"] == "nearest" and not dlg.stretch_button.isEnabled()
    assert dlg.stretch_rows[0]["start_s"] == pytest.approx((400 - 40) * 1001 / 24000, abs=0.01)
    loaded = []
    monkeypatch.setattr(dlg.media, "load", lambda p, keep_position=True: loaded.append(p))
    monkeypatch.setattr(dlg.media, "play", lambda: None)
    dlg.play_stretch(0)
    assert loaded == [str(clip)]
    dlg.stretch_cards[0].setCurrentIndex(1)
    assert dlg.stretch_button.isEnabled()
    plans = []
    dlg.plan_ready.connect(plans.append)
    dlg.apply_stretches(note="the phaser sweep judders on nearest; interpolation is clean")
    plan = json.loads(Path(plans[0]).read_text())
    assert [s["stage"] for s in plan["steps"]][:2] == ["video-trim", "frame-fixes"]
    assert "encode-final" in [s["stage"] for s in plan["steps"]]
    assert ws.store.load().get("video.hybrid_choices") == {"100": "interpolate"}


def test_delivery_review_frame_fix_tab_previews_and_applies(tmp_path, qtbot, monkeypatch):
    from fstk.ui import reviews

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []},
                                     no_dvd=True)
    reports = prof.root("reports", efm.EP)
    (reports / "episode-quality-20261122t000000.json").write_text(json.dumps(
        {"episode": "DS9:S01E20", "verdict": "REVIEW", "counts": {"PASS": 0, "NOTE": 0, "FLAG": 1, "FAIL": 0},
         "rows": [], "moments": [{"time_s": 10.01, "reason": "dropout left as captured", "score": 100}]}))
    with ws.store.transaction(by="t", reason="setup") as m:
        m.set("video.frames", 50000, by="t")
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    assert dlg.moments.count() == 1
    dlg._add_fix_from_moment(dlg.moments.item(0))
    combo = dlg.fix_table.cellWidget(0, 2)
    combo_keys = [combo.itemData(i) for i in range(combo.count())]
    assert "dvd" not in combo_keys                                  # no DVD route: the DVD option is not offered
    assert dlg.fix_table.item(0, 0).text() == "240"
    warnings = []
    monkeypatch.setattr(reviews.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    plans = []
    dlg.plan_ready.connect(plans.append)
    dlg.preview_fix()                                              # no note yet → refused
    assert warnings and "8 characters" in warnings[0] and not plans
    dlg.fix_table.item(0, 5).setText("dropout streak across the console")
    dlg.preview_fix()
    step = json.loads(Path(plans[0]).read_text())["steps"][0]
    assert step["stage"] == "fix-preview" and step["params"]["fix"]["first"] == 240
    work = tmp_path / "pv"
    work.mkdir()
    from PIL import Image
    for k in ("captured", "fixed"):
        Image.new("RGB", (40, 20)).save(work / f"{k}.png")
    (reports / "fix-preview-20261122t000002.json").write_text(json.dumps(
        {"fix": step["params"]["fix"], "detail": {"grain_ratio_vs_neighbour": 0.62, "grain_flag": True},
         "strips": {k: str(work / f"{k}.png") for k in ("captured", "fixed")},
         "clips": {"captured": "a.mkv", "fixed": "b.mkv"}}))
    dlg.show_fix_preview()
    assert "0.62" in dlg.fix_preview_text.text() and dlg.fix_strips.pixmap().height() == 40
    dlg.apply_fixes()
    plan = json.loads(Path(plans[-1]).read_text())
    assert [s["stage"] for s in plan["steps"]][:3] == ["frame-fixes", "video-quality", "encode-final"]
    assert ws.store.load().get("video.manual_fixes")[0]["first"] == 240


def test_efm_window_shows_a_waveform_strip_per_capture_and_click_listens(efm_env, qtbot, monkeypatch):
    from fstk.ui import reviews

    prof, _ws, _ledger, _ = efm_env
    efm._stack(efm_env)
    efm.run(efm_env, [efm.DECODE], "d3", orc=efm.FakeOrc(quality={"L1": dict(concealed=50), "P1": dict(muted=3)}))
    dlg = reviews.EfmCaptureReview(Services(prof), "DS9:S01E20")
    qtbot.addWidget(dlg)
    assert set(dlg.strips) == {r["capture"] for r in dlg.data["rows"]}
    assert "AUTOMATIC CHOICE" in dlg.headline.text() and dlg.details.isHidden()        # numbers hidden by default
    with_audio = [k for k, v in dlg.data["wavs"].items() if Path(v).is_file()]
    assert with_audio
    qtbot.waitUntil(lambda: all(k in dlg._overviews for k in with_audio), timeout=20000)
    assert dlg.strips[with_audio[0]][1].peaks and "drop-outs" in dlg.strips[with_audio[0]][2].text()
    played = []
    monkeypatch.setattr(dlg.media, "load", lambda path, keep_position=True: played.append(path))
    monkeypatch.setattr(dlg.media, "play", lambda: None)
    label = dlg.data["rows"][1]["capture"]
    dlg._listen(1, label, 12.0)
    assert dlg.selected_capture() == label and dlg.strips[label][0].isChecked()
    assert played == [dlg.data["wavs"][label]]


def test_delivery_marks_move_on_to_the_next_chunk_and_show_on_the_timeline(tmp_path, qtbot, monkeypatch):
    from fstk.ui import reviews

    prof, ws, report = _delivery_env(tmp_path, {"pass": 2, "fail": 0, "failed_gates": [], "unverified_gates": []})
    dlg = reviews.DeliveryReview(Services(prof), "DS9:S01E20", actor="op")
    qtbot.addWidget(dlg)
    played = []
    monkeypatch.setattr(dlg, "play_chunk", played.append)
    assert dlg.current == 0 and dlg.details.isHidden() and "WATCH AND LISTEN" in dlg.headline.text()
    assert [t["tone"] for t in dlg.timeline.ticks] == ["neutral"] * 3 and dlg.timeline.marks   # + the flagged place
    dlg.mark("ok")
    assert dlg.current == 1 and played == [1]                      # next chunk plays by itself
    dlg.mark("ok")
    dlg.mark("ok")
    assert [t["tone"] for t in dlg.timeline.ticks] == ["good"] * 3 and dlg.approve_button.isEnabled()
    dlg.show()
    assert dlg.decide("APPROVED") and not dlg.isVisible()          # approving closes the window
