"""Every real v38 manifest (current files and their .bak history) imports and round-trips."""
from __future__ import annotations

import json

import pytest

from fstk.core.hashing import sha256_bytes
from fstk.manifest.store import ManifestStore
from fstk.manifest.v38 import export_v38, import_v38, v38_equivalent
from tests.conftest import v38_manifest_paths

PATHS = v38_manifest_paths()


@pytest.mark.parametrize("path", PATHS, ids=[p.name for p in PATHS])
def test_round_trip(path):
    raw = path.read_bytes()
    doc = json.loads(raw)
    if "episode" not in doc or not str(doc.get("episode", "")).startswith("S"):
        pytest.skip("template without an episode code")
    manifest = import_v38(doc, source_sha256=sha256_bytes(raw))
    exported = export_v38(manifest)
    assert v38_equivalent(doc, exported)
    # and importing the export gives the same new-schema values
    again = import_v38(exported)
    assert again.doc["values"].keys() == manifest.doc["values"].keys()
    assert again.doc["artifacts"] == manifest.doc["artifacts"]


def test_template_has_no_values():
    doc = json.loads((PATHS[0].parent / "_template.json").read_text())
    doc["episode"], doc["identity"] = "S01E01", "DS9:S01E01"
    m = import_v38(doc)
    from fstk.manifest.registry import field_def

    assert [k for k in m.set_keys() if field_def(k).kind == "measurement"] == []
    assert v38_equivalent(doc, export_v38(m))


def test_s01e20_contents():
    raw = (PATHS[0].parent / "S01E20.json").read_bytes()
    m = import_v38(json.loads(raw), source_sha256=sha256_bytes(raw))
    assert str(m.episode) == "DS9:S01E20"
    assert len(m.capture_labels) == 12 and m.get("stack.reference") == "P6"
    assert m.get("audio.pad_frames") == 118 and m.get("audio.capture") == "P2"
    assert m.get("stack.frames") == 83335
    assert not m.is_set("video.head_trim")       # null in v38 → unset, never 0
    assert m.entry("audio.capture")["origin"] == "v38"
    assert m.capture("L1")["files"]["tbc"]["bytes"] == 79683487520
    assert m.artifact("ld_native") == {"root": "audio", "name": "S01E20/P2-efm.wav"}
    assert m.doc["sources"]["source_provenance"]["mode"] == "local-inventory"


def test_imported_manifest_can_be_stored(tmp_path):
    raw = (PATHS[0].parent / "S01E20.json").read_bytes()
    store = ManifestStore(tmp_path)
    store.create(import_v38(json.loads(raw)), by="import", reason="v38 import")
    assert store.load().get("stack.frames") == 83335 and store.verify() == []


def test_imported_episode_resolves_v38_files_and_still_exports_identically(fixtures):
    """--v38-profile: artifacts become absolute paths on the v38 machine; the v38 view is unchanged."""
    import json

    from fstk.manifest.v38 import export_v38, import_v38, resolve_v38_artifacts, v38_equivalent

    doc = json.loads((fixtures / "v38" / "manifests" / "S01E20.json").read_text(encoding="utf-8-sig"))
    m = import_v38(doc)
    roots = {"probe": "Z:\\ld-pipeline-device\\probe", "prores": "Z:\\ld-pipeline-device\\prores",
             "audio": "Z:\\ld-pipeline-device\\audio", "dvd_root": "Z:\\Video\\DS9S1D5"}
    out = resolve_v38_artifacts(m, roots)
    spec = m.artifact("stack_master")
    assert spec == {"path": "Z:\\ld-pipeline-device\\prores\\S01E20-stack-master.mov"}
    assert "stack_master" in out["resolved"]
    if m.artifact("ld_native"):
        assert m.artifact("ld_native")["path"].startswith("Z:\\ld-pipeline-device\\audio\\S01E20\\")
    assert v38_equivalent(doc, export_v38(m))


def test_real_v38_values_outside_the_old_registry_limits_import():
    """S01E18 recorded a negative retime gap-vs-black; S02E02 a cut-PAD choice of not-available."""
    from fstk.core.identity import EpisodeId
    from fstk.manifest.model import Manifest

    m = Manifest.new(EpisodeId.parse("DS9:S01E18"), title="t")
    m.set("retime.max_gap_vs_black_s", -0.719255, by="t")
    m.set("mode.cut_pad_images_choice", "not-available", by="op", note="v38")
