from __future__ import annotations

import json
from pathlib import Path

from fstk.cli.main import main

FIX = Path(__file__).parents[1] / "fixtures" / "v38"


def test_profile_and_manifest_import_flow(tmp_path, capsys):
    prof = tmp_path / "cfg" / "machine.json"
    assert main(["--profile", str(prof), "profile", "import", str(FIX / "profiles" / "macos-example.json"),
                 "--data-root", str(tmp_path / "data"), "--media-root", str(tmp_path / "media")]) == 0
    assert json.loads(prof.read_text())["schema"] == "fstk-machine-profile-v1"
    assert main(["--profile", str(prof), "profile", "import", str(FIX / "profiles" / "macos-example.json"),
                 "--data-root", str(tmp_path / "data")]) == 2  # exists, no --force
    capsys.readouterr()
    assert main(["--profile", str(prof), "--actor", "op", "manifest", "import",
                 str(FIX / "manifests" / "S01E20.json")]) == 0
    assert "DS9:S01E20" in capsys.readouterr().out
    assert main(["--profile", str(prof), "manifest", "import", str(FIX / "manifests" / "S01E20.json")]) == 2
    assert main(["--profile", str(prof), "manifest", "verify", "S01E20"]) == 0
    assert main(["--profile", str(prof), "manifest", "show", "DS9:S01E20"]) == 0
    out = capsys.readouterr().out
    assert "stack.frames" in out and "83335" in out
    assert main(["--profile", str(prof), "--json", "manifest", "history", "S01E20"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["origin"] == "v38"


def test_stages_and_docs(tmp_path, capsys):
    assert main(["stages"]) == 0
    assert "dvd-probe" in capsys.readouterr().out
    out = tmp_path / "ref.md"
    assert main(["docs", "stages", "--out", str(out)]) == 0
    assert "## `dvd-probe`" in out.read_text()


def test_doctor_without_profile(capsys):
    rc = main(["--json", "doctor", "--no-profile"])
    report = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "python" for c in report["checks"]) and rc in (0, 1)


def test_openings_list_shows_the_packaged_packs(capsys):
    assert main(["openings", "list"]) == 0
    out = capsys.readouterr().out
    assert "DS9 S01E20  opening at DVD frame   61  (12.012 s)  In The Hands of the Prophets" in out
    assert "DS9 S01E04" in out and "(29.997 s)" in out
