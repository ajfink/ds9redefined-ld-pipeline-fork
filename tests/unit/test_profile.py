from __future__ import annotations

import json
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile, convert_v38_profile, default_profile_path
from fstk.core.errors import ConfigError
from fstk.core.identity import EpisodeId

FIX = Path(__file__).parents[1] / "fixtures" / "v38" / "profiles"
DS9 = EpisodeId.parse("DS9:S01E20")
VOY = EpisodeId.parse("VOY:S01E01")


def make(tmp_path, **over) -> Profile:
    data = {"schema": SCHEMA, "machine": "TEST", "os": "linux",
            "data_root": str(tmp_path / "data"), "media_root": str(tmp_path / "media")}
    data.update(over)
    return Profile.from_dict(data)


def test_round_trip_save_load(tmp_path):
    p = make(tmp_path, tools={"ffmpeg": "/usr/bin/ffmpeg"}, limits={"min_free_gb": 40})
    path = p.save(tmp_path / "cfg" / "machine.json")
    again = Profile.load(path)
    assert again.to_dict() == p.to_dict() and again.limits.min_free_gb == 40
    assert again.limits.resources["compute"] == 1


def test_rejects_v38_profile_directly(tmp_path):
    with pytest.raises(ConfigError, match="profile import"):
        Profile.from_dict(json.loads((FIX / "windows-machine.json").read_text()))


@pytest.mark.parametrize("over,msg", [
    ({"data_root": "relative/path"}, "absolute"),
    ({"tools": {"ffmpg": "/x"}}, "unknown tool"),            # a typo is refused; a *retired* tool is dropped
    ({"roots": {"nope": "/x"}}, "unknown media root"),
    ({"limits": {"min_free_gb": 0}}, "positive reserve"),
    ({"limits": {"resources": {"compute": 0}}}, ">= 1"),
    ({"downloads_template": "fixed-name"}, "{episode}"),
])
def test_validation(tmp_path, over, msg):
    with pytest.raises(ConfigError, match=msg):
        make(tmp_path, **over)


def test_root_layout(tmp_path):
    p = make(tmp_path, roots={"stack": str(tmp_path / "fast")}, sources={"downloads_root": "/srv/dl"})
    assert p.root("manifest", DS9) == tmp_path / "data" / "episodes" / "DS9" / "S01E20" / "manifest"
    assert p.root("audio", DS9) == tmp_path / "media" / "ds9-s01e20" / "audio"
    assert p.root("avs", DS9) == p.root("scripts", DS9)
    assert p.root("stack", DS9) == tmp_path / "fast" / "S01E20"
    assert p.root("stack", VOY) == tmp_path / "fast" / "VOY" / "S01E01"
    assert p.downloads_dir(DS9) == Path("/srv/dl/DS9-S01E20-LaserDisc-Copies")
    with pytest.raises(ConfigError, match="dvd_root"):
        p.root("dvd_root", DS9)


def test_tool_resolution(tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_text("")
    p = make(tmp_path, tools={"ffmpeg": str(exe), "ffprobe": str(tmp_path / "missing")})
    assert p.tool("ffmpeg") == exe
    with pytest.raises(ConfigError, match="missing exe"):
        p.tool("ffprobe")
    with pytest.raises(ConfigError, match="not configured"):
        p.tool("orc", allow_path_lookup=False)


def test_windows_absolute_paths_accepted_on_any_host(tmp_path):
    p = Profile.from_dict({"schema": SCHEMA, "machine": "W", "os": "windows",
                           "data_root": "C:\\FSTK", "media_root": "\\\\nas\\media"})
    assert str(p.data_root).endswith("FSTK")


def test_convert_v38_windows_profile():
    v38 = json.loads((FIX / "windows-machine.json").read_text())
    new, notes = convert_v38_profile(v38, data_root="C:\\FSTK\\data")
    p = Profile.from_dict(new)
    assert p.os == "windows" and p.machine == "DEVICE-A" and p.instance_id == "D02C1DA9"
    assert str(p.media_root) == "Z:\\ld-pipeline-device" and p.limits.min_free_gb == 40
    assert "avisynth_plugin_dir" not in p.tools and p.tools["orc"].endswith("orc-cli.exe")
    assert p.sources["dvd_root"] == "Z:\\Video\\DS9S1D5"
    assert any("AviSynth is retired" in n for n in notes)
    assert any("episode-specific" in n for n in notes)  # 'DS9-S01E20-LaserDisc-Copies' had no {episode}


def test_convert_v38_macos_example():
    v38 = json.loads((FIX / "macos-example.json").read_text())
    new, notes = convert_v38_profile(v38, data_root="/Users/x/FSTK")
    p = Profile.from_dict(new)
    assert p.os == "macos" and p.tools["vspipe"].endswith("vspipe")
    assert p.downloads_template == "DS9 {episode} LaserDisc"
    assert not any("vspipe is new" in n for n in notes)


def test_default_profile_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FSTK_PROFILE", str(tmp_path / "p.json"))
    assert default_profile_path() == tmp_path / "p.json"


def test_a_retired_tool_is_dropped_from_an_old_profile_with_a_note(tmp_path):
    """vs_plugin_dir is gone (the plugin wheels autoload): an old profile must still load, not be refused."""
    from fstk.config.profile import KNOWN_TOOLS, RETIRED_TOOLS, Profile

    assert "vs_plugin_dir" not in KNOWN_TOOLS and "vs_plugin_dir" in RETIRED_TOOLS
    doc = {"schema": "fstk-machine-profile-v1", "machine": "mac", "os": "macos",
           "data_root": str(tmp_path / "d"), "media_root": str(tmp_path / "m"),
           "tools": {"vs_plugin_dir": str(tmp_path / "plugins"), "curl": "/usr/bin/curl"}}
    prof = Profile.from_dict(doc)
    assert "vs_plugin_dir" not in prof.tools and prof.tools["curl"] == "/usr/bin/curl"
    assert any("tools.vs_plugin_dir dropped" in n for n in prof.notes)
