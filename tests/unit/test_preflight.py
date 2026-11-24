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
