from __future__ import annotations

import json
import logging
import sys
import zipfile
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core import applog
from fstk.core.identity import EpisodeId
from fstk.diagnostics import build_bundle, redactor
from fstk.manifest.v38 import import_v38
from fstk.workflow.engine import EpisodeWorkspace

FIX = Path(__file__).parents[1] / "fixtures" / "v38" / "manifests" / "S01E20.json"
EP = EpisodeId.parse("DS9:S01E20")


@pytest.fixture
def fresh_logging(tmp_path):
    saved = (sys.excepthook, applog._state.copy())
    applog._state.clear()
    yield tmp_path / "logs"
    root = logging.getLogger("fstk")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    sys.excepthook = saved[0]
    applog._state.clear()
    applog._state.update(saved[1])


def test_setup_logging_writes_session_events_and_uncaught_exceptions(fresh_logging):
    path = applog.setup_logging(directory=fresh_logging)
    logging.getLogger("fstk.ui").info("action run-intake episode=DS9:S01E20")
    try:
        raise ValueError("boom in a slot")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    for h in logging.getLogger("fstk").handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert "session start" in text and "action run-intake" in text
    assert "uncaught exception" in text and "ValueError: boom in a slot" in text and "Traceback" in text
    assert (fresh_logging / "fstk-crash.log").is_file()
    assert applog.setup_logging(directory=fresh_logging) == path           # idempotent


def test_engine_events_and_tool_runs_reach_the_log(fresh_logging, tmp_path):
    from fstk.tools import process
    from fstk.workflow.engine import Engine

    path = applog.setup_logging(directory=fresh_logging)
    process.run([sys.executable, "-c", "print('hi')"])
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    EpisodeWorkspace(prof, EP).store.create(import_v38(json.loads(FIX.read_text())), by="t", reason="f")
    import tests.unit.test_engine  # noqa: F401  (registers the t-* stages)
    Engine(prof, {"schema": "fstk-workflow-plan-v1", "episode": "DS9:S01E20", "job": "lg",
                  "steps": [{"id": "n", "stage": "t-needs-frames", "after": [], "params": {}}]}, actor="t").run()
    for h in logging.getLogger("fstk").handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert "RUN " in text and "EXIT [0]" in text
    assert "step-start job=lg step=n" in text and "step-blocked job=lg step=n" in text


def test_redaction():
    redact = redactor()
    home = str(Path.home())
    assert redact(f"{home}/x/y").startswith("~/x/y")
    assert home not in redact(f"opened {home}\\file")


def test_bundle_contents_redaction_and_no_media(fresh_logging, tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(import_v38(json.loads(FIX.read_text())), by="t", reason="f")
    job_dir = ws.job_dir("j1")
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(json.dumps({"steps": {"a": {"status": "failed", "issues": [f"{Path.home()}/x"]}}}))
    log = ws.log_dir("j1") / "a"
    log.mkdir(parents=True)
    (log / "encode.log").write_text("x" * 3_000_000 + "THE-END")
    media = prof.episode_media_dir(EP)
    media.mkdir(parents=True)
    (media / "master.mkv").write_bytes(b"\0" * 100)
    applog.setup_logging(directory=fresh_logging)
    logging.getLogger("fstk.ui").info("hello bundle")
    for h in logging.getLogger("fstk").handlers:
        h.flush()
    res = build_bundle(prof, tmp_path / "out" / "b.zip", episodes=["DS9:S01E20"], logs_dir=fresh_logging)
    z = zipfile.ZipFile(res["path"])
    names = z.namelist()
    assert "README.txt" in names and "environment.json" in names and "profile.json" in names
    assert any(n.startswith("logs/fstk.log") for n in names)
    assert "episodes/DS9-S01E20/plans/j1/state.json" in names
    assert not any("master.mkv" in n for n in names)
    state = z.read("episodes/DS9-S01E20/plans/j1/state.json").decode()
    assert str(Path.home()) not in state and "~/x" in state
    big = z.read("episodes/DS9-S01E20/logs/j1/a/encode.log").decode()
    assert big.endswith("THE-END") and big.startswith("[... cut") and len(big) < 2_100_000
    assert any(n.startswith("episodes/DS9-S01E20/manifest/") for n in names)
    # another episode filter excludes it
    res2 = build_bundle(prof, tmp_path / "b2.zip", episodes=["VOY:S01E01"], logs_dir=fresh_logging)
    assert not any(n.startswith("episodes/") for n in zipfile.ZipFile(res2["path"]).namelist())


def test_cli_bundle(fresh_logging, tmp_path, monkeypatch, capsys):
    from fstk.cli.main import main

    Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                       "media_root": str(tmp_path / "m")}).save(tmp_path / "machine.json")
    monkeypatch.setenv("FSTK_PROFILE", str(tmp_path / "machine.json"))
    assert main(["bundle", "--out", str(tmp_path / "x.zip")]) == 0
    assert "wrote" in capsys.readouterr().out and zipfile.is_zipfile(tmp_path / "x.zip")


@pytest.mark.gui
def test_gui_has_log_and_bundle_buttons(tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from fstk.ui.app import MainWindow
    from fstk.ui.services import Services

    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    w = MainWindow(Services(prof), actor="op")
    qtbot.addWidget(w)
    assert w.bundle_button.text().startswith("Save debug bundle") and w.logs_button.text() == "Open logs"
    out = tmp_path / "gui.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))
    from PySide6.QtGui import QDesktopServices

    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda *a, **k: True))
    w.save_bundle()
    assert zipfile.is_zipfile(out) and "debug bundle saved" in w.log.toPlainText()


def test_log_is_on_by_default_with_environment_block_and_level_override(fresh_logging, tmp_path, monkeypatch):
    monkeypatch.setenv("FSTK_LOG_LEVEL", "DEBUG")
    path = applog.setup_logging(directory=fresh_logging)
    assert logging.getLogger("fstk").level == logging.DEBUG
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    applog.log_environment(prof)
    for h in logging.getLogger("fstk").handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert "packages {" in text and "free space media:" in text and "folders data=" in text
