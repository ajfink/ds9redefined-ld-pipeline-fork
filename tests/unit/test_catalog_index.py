from __future__ import annotations

import pytest

from fstk.catalog.archive import CATALOG_SCHEMA, resolve_episode
from fstk.catalog.index import CatalogIndex, load, local_status
from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.manifest.store import ManifestStore


@pytest.fixture(scope="module")
def index():
    return CatalogIndex.load()


# ---- v38 tests that read the real snapshot (ported by hand: paths changed to the bundled copy)
def test_included_snapshot_has_full_collection_counts_and_known_gaps():
    catalog = load()
    assert catalog["schema"] == CATALOG_SCHEMA and catalog["source"]["upload_count"] == 275
    sections = {s["key"]: len(s["items"]) for s in catalog["children"]}
    assert sections == {"ds9-episode-copies": 124, "voy-episode-copies": 120, "ds9-season-stacks": 15,
                        "voy-season-stacks": 15, "tng-extra": 1}
    assert catalog["coverage"]["TNG"]["partial_episode_codes_in_extra"] == [
        "S01E24", "S04E01", "S04E21", "S05E15", "S05E19"]


def test_cross_series_and_emissary_and_capture_warnings():
    catalog = load()
    voy = resolve_episode(catalog, "VOY", 1, 1)
    assert voy["status"] == "matched" and voy["workflow_identity"] == "VOY:S01E01"
    for e in (1, 2):
        r = resolve_episode(catalog, "DS9", 1, e)
        assert any("Emissary" in w for w in r["catalog_item"]["workflow_warnings"])
    single = resolve_episode(catalog, "DS9", 4, 3)
    assert any("needs at least two" in w for w in single["catalog_item"]["workflow_warnings"])


# ---- picker index
def test_index_counts_titles_and_selectability(index):
    assert len(index.series("DS9")) == 124 and len(index.series("VOY")) == 120
    assert len(index.series("TNG")) == 5 and not any(e.selectable for e in index.series("TNG"))
    e20 = index.find("DS9", 1, 20)
    assert e20.title == "In the Hands of the Prophets" and e20.selectable
    assert len(e20.capture_labels) == 12 and e20.raw_efm_count == 12
    assert {f["name"].rsplit(".", 1)[-1] for f in e20.selected_files} >= {"tbc", "json", "efm"}
    emissary = index.find("DS9", 1, 1)
    assert not emissary.selectable and "two-part" in emissary.reason
    assert index.summary()["selectable"] == 124 - 2 + 120


def test_search_natural_language_and_title(index):
    assert [e.code for e in index.search("Download episode 18 of season 2 of DS9")] == ["S02E18"]
    assert index.search("Star Trek Voyager S01E01")[0].series == "VOY"
    hits = index.search("prophets")
    assert any(e.code == "S01E20" for e in hits)
    assert all(e.series == "VOY" for e in index.search("voy caretaker"))
    assert index.search("TNG S04E21")[0].kind == "combined-item"


def test_coverage_reports_gaps_without_inventing(index):
    cov = index.coverage()
    assert sorted(cov["DS9"].seasons) == [1, 2, 3, 4, 5] and "not in this snapshot" in cov["DS9"].note
    assert "lookup-only" in cov["TNG"].note


def test_local_status(index, tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "sources": {"downloads_root": str(tmp_path / "dl")}})
    e = index.find("DS9", 2, 18)
    assert local_status(e, prof).state == "remote"
    folder = prof.downloads_dir(e.episode_id)
    folder.mkdir(parents=True)
    tbcs = [f for f in e.selected_files if f["name"].endswith((".tbc", ".tbc.json"))]
    first = tbcs[0]
    with open(folder / first["name"], "wb") as fh:
        fh.truncate(first["size"])               # sparse file at the catalog size
    st = local_status(e, prof)
    assert st.state == "partial" and st.present_files == 1
    for f in tbcs[1:]:
        with open(folder / f["name"], "wb") as fh:
            fh.truncate(f["size"])
    assert local_status(e, prof).state == "downloaded"
    ManifestStore(prof.episode_state_dir(EpisodeId.parse("DS9:S02E18")) / "manifest").create(
        Manifest.new(EpisodeId.parse("DS9:S02E18")), by="t", reason="t")
    assert local_status(e, prof).state == "workspace"
    assert local_status(index.find("TNG", 4, 21), prof).state == "lookup-only"
