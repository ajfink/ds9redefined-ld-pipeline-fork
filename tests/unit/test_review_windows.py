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


def _delivery_env(tmp_path, summary):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    m = Manifest.new(efm.EP, title="t")
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
    assert dlg.gates.rowCount() == 2 and dlg.intervals.count() == 1
    assert dlg.approve_button.isEnabled() and dlg.reject_button.isEnabled()
    path = dlg.decide("APPROVED", note="watched all flagged intervals; clean")
    assert path and ws.approvals.status("delivery", manifest=ws.store.load()).state == "approved"


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
