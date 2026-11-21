from __future__ import annotations

import json

import pytest

from fstk.catalog.index import CatalogIndex
from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.manifest.v38 import export_v38
from fstk.workflow.engine import Engine
from fstk.workflow.plans import intake_plan
from fstk.workspace.episodes import create_from_catalog, create_from_local
from fstk.workspace.local_source import LocalSourceError, inspect_local_source
from tests.integration.test_archive_http import FILES, Server

EP = EpisodeId.parse("DS9:S02E18")


@pytest.fixture
def profile(tmp_path):
    (tmp_path / "dl").mkdir()
    return Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "sources": {"downloads_root": str(tmp_path / "dl")},
                              "limits": {"min_free_gb": 0.001}})


def test_catalog_to_inventory(profile):
    index = CatalogIndex.load()
    entry = index.find("DS9", 2, 18)
    store = create_from_catalog(profile, entry, actor="op", catalog_summary=index.summary())
    with pytest.raises(FstkError, match="already has a workspace"):
        create_from_catalog(profile, entry, actor="op")
    with pytest.raises(FstkError, match="two-part"):
        create_from_catalog(profile, index.find("DS9", 1, 1), actor="op")
    server = Server()
    try:
        from fstk.tools.archive_http import ArchiveClient

        # the catalog identifier is the real one; serve the fake item under it
        client = ArchiveClient(base_url=server.url, retry_delay=0.01)
        plan = intake_plan(EP, download=True, job="intake-test", include_stack_reference=False)
        events = []
        state = Engine(profile, plan, actor="op", events=events.append, tool_overrides={"archive": client}).run()
    finally:
        server.close()
    assert [state["steps"][s]["status"] for s in ("download", "inventory")] == ["passed", "passed"], state
    m = store.load()
    src = m.doc["sources"]
    assert src["archive_item_verified"] is True and src["source_provenance"]["mode"] == "archive-download"
    assert m.capture_labels == ["L1", "P1"]
    tbc = m.capture("L1")["files"]["tbc"]
    assert tbc["name"].endswith("L1.tbc") and len(tbc["md5"]) == 32 and tbc["bytes"] == len(FILES[tbc["name"]])
    v38 = export_v38(m)                                    # v38 shape stays exportable
    assert v38["captures"][0]["tbc_md5"] == tbc["md5"]
    assert any(e["kind"] == "progress" for e in events)
    assert store.verify() == []


def _local_folder(tmp_path, labels=("L1", "P2"), series="VOY", code="S01E01", title="Caretaker"):
    folder = tmp_path / f"{series} {code} rip"
    folder.mkdir(parents=True)
    for x in labels:
        base = folder / f"{series}-{code}-{title}-{x}.tbc"
        base.write_bytes(b"t" * 100)
        (folder / (base.name + ".json")).write_text("{}")
    return folder


def test_local_folder_workspace_and_inventory(profile, tmp_path):
    folder = _local_folder(tmp_path)
    store = create_from_local(profile, folder, actor="op")
    m = store.load()
    assert str(m.episode) == "VOY:S01E01" and m.title == "Caretaker"
    plan = intake_plan("VOY:S01E01", download=False, job="local", include_stack_reference=False)
    state = Engine(profile, plan, actor="op").run()
    assert state["steps"]["inventory"]["status"] == "passed", state
    m = store.load()
    assert m.capture_labels == ["L1", "P2"] and m.doc["sources"]["source_provenance"]["mode"] == "local-inventory"
    report = json.loads((profile.root("reports", m.episode) / "capture-inventory.json").read_text())
    assert report["captures"][0]["files"]["tbc"]["hash_scope"] == "full"


def test_local_source_rules(tmp_path):
    with pytest.raises(LocalSourceError, match="at least two TBC"):
        inspect_local_source(_local_folder(tmp_path / "a", labels=("L1",)))
    f = _local_folder(tmp_path / "b")
    (f / "VOY-S01E01-Caretaker-L1.tbc.json").unlink()
    with pytest.raises(LocalSourceError, match="no matching JSON sidecar"):
        inspect_local_source(f)
    g = _local_folder(tmp_path / "c")
    (g / "VOY-S01E02-Parallax-P9.tbc").write_bytes(b"x")
    (g / "VOY-S01E02-Parallax-P9.tbc.json").write_text("{}")
    with pytest.raises(LocalSourceError, match="multiple codes"):
        inspect_local_source(g)
    h = _local_folder(tmp_path / "d")
    (h / "capture.lds").write_bytes(b"rf")
    assert inspect_local_source(h)["raw_rf_files"] == ["capture.lds"]


def test_inventory_refuses_folder_for_another_episode(profile, tmp_path):
    folder = _local_folder(tmp_path, series="VOY", code="S01E01")
    store = create_from_local(profile, folder, actor="op")
    with store.transaction(by="op", reason="point at wrong folder") as m:
        other = _local_folder(tmp_path / "x", series="VOY", code="S01E02", title="Parallax")
        m.set_sources({**m.doc["sources"], "downloads_override": str(other)})
    state = Engine(profile, intake_plan("VOY:S01E01", download=False, job="wrong", include_stack_reference=False),
                   actor="op").run()
    st = state["steps"]["inventory"]
    assert st["status"] == "blocked" and "VOY:S01E02" in st["issues"][0]
