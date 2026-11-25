"""Windows: the app runs without a console, so a console tool started without CREATE_NO_WINDOW flashes a window of
its own (owner report 2026-11-24: "a lot of command windows pop up and close during these runs")."""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

from fstk.tools import process

SRC = Path(__file__).resolve().parents[2] / "src" / "fstk"
#: the setup wizard's package-manager installs run in the launcher's own console and may ask questions there
INTERACTIVE = {("install/wizard.py", "call")}


def test_every_tool_the_app_starts_is_started_without_a_window():
    missing = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "Popen", "call", "check_call", "check_output")):
                continue
            if (rel, node.func.attr) in INTERACTIVE:
                continue
            spread = [k.value for k in node.keywords if k.arg is None]
            ok = any(isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                     and v.func.attr in ("hidden", "_popen_kwargs") for v in spread)
            ok = ok or any(isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                           and v.func.id in ("hidden", "_popen_kwargs") for v in spread)
            if not ok:
                missing.append(f"{rel}:{node.lineno} subprocess.{node.func.attr}")
    assert not missing, "started with a console window on Windows:\n" + "\n".join(missing)


def test_the_flags_hide_the_window_and_keep_the_process_group(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    assert process.hidden() == {"creationflags": 0x08000000}
    flags = process._popen_kwargs()["creationflags"]
    assert flags & 0x08000000 and flags & 0x00000200 and flags & 0x00004000     # no window, group, below normal
