from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from fstk.catalog import build
from fstk.catalog.archive import CATALOG_SCHEMA
from fstk.catalog.index import CatalogIndex, default_catalog_path

IDS = [
    "DS9-S01E20-LaserDisc-Copies",
    "DS9-Season-01-LaserDisc-Stack-Part-1-of-2",
    "TNG-Extra-LaserDisc-Copies",
]


def _files(ident: str) -> list[dict]:
    if ident.startswith("DS9-S01E20"):
        names = [f"{lab}/DS9-S01E20-Title-{lab}.tbc" for lab in ("L1", "M1")]
        names += [n + ".json" for n in names] + [f"{lab}/DS9-S01E20-Title-{lab}.efm" for lab in ("L1", "M1")]
        return [{"name": n, "size": "10", "md5": "x", "format": "f"} for n in names]
    return [{"name": "a.txt", "size": "1"}]


def fake_fetch(url: str) -> dict:
    if "page_production" in url:
        rows = [
            {
                "fields": {
                    "identifier": i,
                    "title": i,
                    "mediatype": "data",
                    "item_size": "5",
                    "files_count": "4",
                    "publicdate": "2025-01-01",
                }
            }
            for i in IDS
        ]
        return {
            "response": {"body": {"page_elements": {"uploads": {"hits": {"total": len(rows), "hits": rows}}}}}
        }
    ident = url.split("/metadata/")[1].rsplit("/files", 1)[0]
    return {"result": _files(ident)}


def test_build_catalog_matches_schema_and_loads_in_index(tmp_path):
    cat = build.build_catalog(fake_fetch, workers=2)
    assert cat["schema"] == CATALOG_SCHEMA and cat["source"]["upload_count"] == 3
    assert cat["source"]["media_bytes_downloaded"] == 0
    sections = {s["key"]: len(s["items"]) for s in cat["children"]}
    assert sections["ds9-episode-copies"] == 1 and sections["tng-extra"] == 1
    item = cat["children"][0]["items"][0]
    assert item["capture_labels"] == ["L1", "M1"] and item["download_inventory"]["raw_efm_count"] == 2
    path = build.write_snapshot(cat, tmp_path)
    idx = CatalogIndex.load(path)
    assert idx.find("DS9", 1, 20) is not None


def test_incomplete_listing_is_rejected():
    def bad(url):
        resp = fake_fetch(url)
        if "page_production" in url:
            hits = resp["response"]["body"]["page_elements"]["uploads"]["hits"]
            hits["total"] = 9
            if "page=2" in url:
                hits["hits"] = []
        return resp

    with pytest.raises(RuntimeError, match=r"incomplete|changed"):
        build.build_catalog(bad)


def test_item_failure_aborts_without_partial_tree():
    def flaky(url):
        if "TNG-Extra" in url and "/files" in url:
            raise RuntimeError("boom")
        return fake_fetch(url)

    with pytest.raises(RuntimeError, match="TNG-Extra"):
        build.build_catalog(flaky)


def test_staleness_30_days():
    now = datetime(2026, 12, 1, tzinfo=UTC)
    mk = lambda d: {"source": {"generated_at_utc": (now - timedelta(days=d)).isoformat()}}  # noqa: E731
    assert not build.is_stale(mk(29), now=now)
    assert build.is_stale(mk(31), now=now)
    assert build.is_stale({}, now=now)


def test_snapshot_never_overwrites_and_newest_wins(tmp_path):
    cat = build.build_catalog(fake_fetch)
    a = build.write_snapshot(cat, tmp_path)
    b = build.write_snapshot(cat, tmp_path)
    assert a != b and a.exists() and b.exists()
    assert build.current_path(tmp_path, default_catalog_path()) == b
    assert build.current_path(tmp_path / "none", default_catalog_path()) == default_catalog_path()
    older = tmp_path / "catalog" / "section_31_tree-20000101T000000Z.json.gz"
    older.write_bytes(gzip.compress(b"{}"))
    assert build.current_path(tmp_path, default_catalog_path()) == b


def test_refuses_empty_or_foreign_catalog(tmp_path):
    with pytest.raises(RuntimeError):
        build.write_snapshot({"schema": "other", "children": []}, tmp_path)


def test_resolve_prefers_explicit_profile_path(tmp_path):
    cat = build.build_catalog(fake_fetch)
    build.write_snapshot(cat, tmp_path)
    explicit = SimpleNamespace(catalog_path="/x/y.json", data_root=tmp_path)
    assert str(build.resolve_path(explicit)).replace("\\", "/") == "/x/y.json"
    auto = SimpleNamespace(catalog_path="", data_root=tmp_path)
    assert build.resolve_path(auto).parent.name == "catalog"
    st = build.status(auto)
    assert st["stale"] is False and not st["explicit"]
    json.dumps(st)


def test_bundled_snapshot_status_is_reported(tmp_path):
    st = build.status(SimpleNamespace(catalog_path="", data_root=tmp_path))
    assert st["path"].endswith("section_31_tree.json.gz") and st["generated_at_utc"]
