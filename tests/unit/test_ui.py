from __future__ import annotations

import json
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.v38 import import_v38
from fstk.ui.services import Services
from fstk.workflow.engine import PLAN_SCHEMA, EpisodeWorkspace

FIX = Path(__file__).parents[1] / "fixtures" / "v38" / "manifests"
EP = EpisodeId.parse("DS9:S01E20")


@pytest.fixture
def services(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media"),
                              "limits": {"min_free_gb": 0.001}})
    EpisodeWorkspace(prof, EP).store.create(import_v38(json.loads((FIX / "S01E20.json").read_text())),
                                            by="t", reason="fixture")
    return Services(prof)


def review_plan(tmp_path) -> Path:
    import tests.unit.test_engine  # noqa: F401  (registers the t-* test stages)

    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"schema": PLAN_SCHEMA, "episode": "DS9:S01E20", "job": "rv",
                             "steps": [{"id": "r", "stage": "t-review", "params": {}, "after": []}]}))
    return p


def test_services_browse_and_review_flow(services, tmp_path):
    assert services.episodes() == [EP]
    rows = {r.key: r for r in services.manifest_rows("S01E20")}
    assert rows["stack.frames"].value == "83335" and rows["stack.frames"].origin == "v38"
    assert not rows["video.head_trim"].set
    _, state = services.run_plan(review_plan(tmp_path), actor="op")
    assert state["steps"]["r"]["status"] == "awaiting-review"
    pending = services.pending_reviews("S01E20")
    assert len(pending) == 1 and pending[0].review == "t-evidence" and pending[0].evidence
    services.decide("S01E20", pending[0], decision="APPROVED", note="checked the sheet", actor="op")
    _, state = services.run_plan(review_plan(tmp_path), actor="op")
    assert state["steps"]["r"]["status"] == "passed"
    assert services.pending_reviews("S01E20") == []


@pytest.mark.gui
def test_main_window_smoke(services, tmp_path, qtbot):
    from fstk.ui.app import MainWindow

    w = MainWindow(services, actor="op")
    qtbot.addWidget(w)
    assert w.episodes.count() == 1 and w.title.text().startswith("DS9:S01E20")
    assert w.values.rowCount() > 60
    w.start_plan(str(review_plan(tmp_path)))
    qtbot.waitUntil(lambda: w.thread is None, timeout=10000)
    assert w.tabs.tabText(2) == "Reviews (1)"
    assert "step-awaiting-review" in w.log.toPlainText()
