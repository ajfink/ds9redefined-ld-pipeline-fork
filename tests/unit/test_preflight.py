"""A plan does not start when a tool it needs is set up but missing or broken (Legion 2026-11-23)."""
from __future__ import annotations

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import PreflightError
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.workflow import preflight
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

EP = EpisodeId.parse("DS9:S01E20")


def test_stage_tools_are_found_from_the_stage_modules():
    assert "vapoursynth" in preflight.stage_tools("cadence-evidence")
    assert "ffmpeg" in preflight.stage_tools("dvd-alignment")
    assert "orc" in preflight.stage_tools("stack-render")
    plan = {"steps": [{"stage": "stack-render"}, {"stage": "cadence-evidence"}]}
    assert {"orc", "ffmpeg", "vapoursynth"} <= preflight.plan_tools(plan)


def _profile(tmp_path, tools):
    return Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "tools": tools})


def test_broken_or_missing_configured_tools_are_problems_unconfigured_ones_only_warnings(tmp_path, monkeypatch):
    import shutil

    vspipe = tmp_path / "venv" / "bin" / "vspipe"
    vspipe.parent.mkdir(parents=True)
    vspipe.write_text("")
    prof = _profile(tmp_path, {"vspipe": str(vspipe), "orc": str(tmp_path / "gone" / "orc-cli")})
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    warnings: list[str] = []
    problems = preflight.tool_problems(prof, {"vapoursynth", "orc", "sevenzip"}, warnings=warnings,
                                       runner=lambda argv: 1)              # vspipe --version exits 1
    assert any("vspipe" in p and "exits 1" in p for p in problems)
    assert any("tools.orc points at a missing" in p for p in problems)
    assert any("sevenzip" in w for w in warnings) and not any("sevenzip" in p for p in problems)
    assert preflight.tool_problems(prof, {"vapoursynth"}, runner=lambda argv: 0) == []
    assert preflight.tool_problems(prof, {"vapoursynth", "orc"}, skip={"orc"}, runner=lambda argv: 0) == []


def test_engine_refuses_to_start_a_plan_whose_tool_is_broken(tmp_path, monkeypatch):
    prof = _profile(tmp_path, {"vspipe": str(tmp_path / "old" / "venv" / "bin" / "vspipe-gone")})
    ws = EpisodeWorkspace(prof, EP)
    m = Manifest.new(EP, title="t")
    ws.store.create(m, by="t", reason="setup")
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "pf",
            "steps": [{"id": "cad", "stage": "cadence-evidence", "params": {}, "after": []}]}
    events = []
    with pytest.raises(PreflightError, match="Not started") as err:
        Engine(prof, plan, actor="op", events=events.append, tool_overrides={"ffmpeg": object()}).run()
    assert "vspipe" in str(err.value) and "launcher" in str(err.value)
    assert any(e["kind"] == "preflight-failed" for e in events)
    assert not ws.job_dir("pf").joinpath("state.json").exists()          # nothing ran, nothing recorded


def test_a_missing_vapoursynth_plugin_stops_the_plan():
    rows = [("vs plugin mv", "PASS", "loaded"), ("vs plugin mvu", "FAIL", "missing"),
            ("vs plugin resize2", "WARN", "missing — `repair=qtgmc` will fail; `pp2` still works")]
    assert preflight.vapoursynth_problems(lambda: rows) == ["VapourSynth vs plugin mvu: missing"]


def test_the_pinned_plugin_set_includes_mvutensils_for_qtgmc():
    import json
    import tomllib
    from pathlib import Path

    from fstk.tools.requirements import VS_NAMESPACES

    root = Path(__file__).resolve().parents[2]
    media = tomllib.loads((root / "pyproject.toml").read_text())["project"]["optional-dependencies"]["media"]
    lock = json.loads((root / "vendor" / "vapoursynth-plugins" / "LOCK.json").read_text())["packages"]
    assert any(r.startswith("vapoursynth-mvutensils") for r in media) and "mvu" in VS_NAMESPACES
    assert lock["vapoursynth-mvutensils"]["version"] == "9"
    # Legion 2026-11-24 13:48: vsrgtools' median blur inside QTempGaussMC needs zsmooth
    assert any(r.startswith("vapoursynth-zsmooth") for r in media) and "zsmooth" in VS_NAMESPACES
    assert lock["vapoursynth-zsmooth"]["version"] == "0.20.0"
    wheel = root / "vendor" / "vapoursynth-plugins" / lock["vapoursynth-zsmooth"]["file"]
    import hashlib
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == lock["vapoursynth-zsmooth"]["sha256"]


def test_a_qtgmc_that_cannot_render_stops_the_plan_at_the_start():
    rows = [("vs plugin zsmooth", "PASS", "loaded"),
            ("QTempGaussMC renders", "FAIL", "the scripts' QTGMC call fails: AttributeError: no zsmooth")]
    assert preflight.vapoursynth_problems(lambda: rows) == [
        "VapourSynth QTempGaussMC renders: the scripts' QTGMC call fails: AttributeError: no zsmooth"]


def test_the_qtgmc_probe_runs_the_scripts_own_lines():
    import pytest

    from fstk.media.cadence_map import QTGMC_LINES, ivtc_vpy_script
    from fstk.tools.requirements import probe_qtgmc

    script = ivtc_vpy_script("m.mkv", 3, 0, 0, [(10, 40)], "qtgmc")
    assert "\n".join(QTGMC_LINES) in script                  # the probe runs exactly this text
    pytest.importorskip("vapoursynth")
    pytest.importorskip("vsdeinterlace")
    label, status, note = probe_qtgmc()
    assert status == "PASS", note
