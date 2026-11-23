from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
V38_MANIFESTS = FIXTURES / "v38" / "manifests"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def v38_manifest_paths() -> list[Path]:
    return sorted(V38_MANIFESTS.iterdir())


def requires_tool(name: str):
    return pytest.mark.skipif(shutil.which(name) is None, reason=f"{name} not on PATH")


import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_collection_modifyitems(config, items):
    try:
        import PySide6  # noqa: F401
    except ImportError:
        skip = pytest.mark.skip(reason="PySide6 not installed (pip install -e .[gui])")
        for item in items:
            if "gui" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_modal_volume_advice(monkeypatch):
    """The sandbox's small disks would make the start-up drive check open a modal box and hang a GUI test."""
    try:
        from fstk.ui.app import MainWindow
    except ImportError:                                   # no Qt in this environment
        return
    monkeypatch.setattr(MainWindow, "show_volume_advice", lambda self, advice: None)
