from __future__ import annotations

import json

import pytest

from fstk.core.errors import ConcurrentModificationError, ManifestError
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.manifest.registry import FIELDS, field_def
from fstk.manifest.store import ManifestStore

EP = EpisodeId.parse("DS9:S01E20")


def new_store(tmp_path) -> ManifestStore:
    store = ManifestStore(tmp_path / "manifest")
    store.create(Manifest.new(EP, title="In the Hands of the Prophets"), by="test", reason="unit")
    return store


class TestRegistry:
    def test_every_field_has_description_and_valid_type(self):
        for fd in FIELDS:
            assert fd.description and fd.kind and fd.type

    def test_none_is_never_a_value(self):
        with pytest.raises(ManifestError, match="None is not a value"):
            field_def("video.head_trim").validate(None)

    def test_types_are_strict(self):
        with pytest.raises(ManifestError):
            field_def("video.head_trim").validate(True)  # bool is not an int here
        with pytest.raises(ManifestError):
            field_def("video.head_trim").validate(1.5)
        field_def("video.delta_s").validate(3)  # ints are numbers
        with pytest.raises(ManifestError):
            field_def("video.delta_s").validate(float("nan"))

    def test_minimum_and_uniqueness(self):
        with pytest.raises(ManifestError, match="below"):
            field_def("video.frames").validate(0)
        with pytest.raises(ManifestError, match="unique"):
            field_def("pads.cut_pad_frames").validate([5, 5])

    def test_track_rules(self):
        field_def("deliverable.tracks").validate(["ld", "51", "srt"])
        for bad in (["51"], ["ld", "ld"], ["ld", "dts"]):
            with pytest.raises(ManifestError):
                field_def("deliverable.tracks").validate(bad)

    def test_unknown_keys_are_rejected(self):
        with pytest.raises(ManifestError, match="not a declared manifest field"):
            Manifest.new(EP).set("video.made_up", 1, by="x")


class TestManifest:
    def test_require_fails_loudly_for_unmeasured(self):
        m = Manifest.new(EP)
        with pytest.raises(ManifestError, match="measure and record it"):
            m.require("video.head_trim")
        assert m.get("video.head_trim") is None and not m.is_set("video.head_trim")

    def test_set_records_provenance(self):
        m = Manifest.new(EP)
        m.set("video.head_trim", 0, by="stage:trim-review", stage="trim-review",
              evidence=[{"path": "r.json", "sha256": "ab"}])
        entry = m.entry("video.head_trim")
        assert entry["value"] == 0 and entry["stage"] == "trim-review" and entry["evidence"][0]["sha256"] == "ab"

    def test_automated_choice_needs_note(self):
        with pytest.raises(ManifestError, match="must carry a note"):
            Manifest.new(EP).set("audio.capture", "P2", by="stage:auto-select")

    def test_artifact_names_cannot_escape_root(self):
        m = Manifest.new(EP)
        with pytest.raises(ManifestError):
            m.set_artifact("stack_master", root="prores", name="../../etc/x")
        m.set_artifact("stack_master", root="prores", name="S01E20-stack-master.mov")
        assert m.artifact("stack_master") == {"root": "prores", "name": "S01E20-stack-master.mov"}

    def test_duplicate_capture_labels_rejected(self):
        with pytest.raises(ManifestError):
            Manifest.new(EP).set_sources({"captures": [{"label": "A"}, {"label": "A"}]})


class TestStore:
    def test_create_refuses_overwrite(self, tmp_path):
        store = new_store(tmp_path)
        with pytest.raises(ManifestError, match="refusing to overwrite"):
            store.create(Manifest.new(EP), by="t", reason="again")

    def test_transaction_writes_journal_with_old_and_new(self, tmp_path):
        store = new_store(tmp_path)
        with store.transaction(by="op", reason="measured", stage="cadence") as m:
            m.set("video.head_trim", 12, by="op")
        with store.transaction(by="op", reason="re-measured") as m:
            m.set("video.head_trim", 13, by="op")
        assert store.load().get("video.head_trim") == 13
        rows = store.journal()
        assert [r["op"] for r in rows] == ["create", "update", "update"]
        assert rows[2]["changes"] == [{"op": "set", "key": "video.head_trim", "old": 12, "new": 13}]
        assert store.verify() == []

    def test_noop_transaction_does_not_journal(self, tmp_path):
        store = new_store(tmp_path)
        with store.transaction(by="op", reason="nothing"):
            pass
        assert len(store.journal()) == 1

    def test_exception_leaves_manifest_untouched(self, tmp_path):
        store = new_store(tmp_path)
        before = store.read().raw
        with pytest.raises(RuntimeError), store.transaction(by="op", reason="boom") as m:
            m.set("video.head_trim", 1, by="op")
            raise RuntimeError("abort")
        assert store.read().raw == before and not store.lock_path.exists()

    def test_expect_sha_refuses_stale_decision(self, tmp_path):
        store = new_store(tmp_path)
        reviewed = store.read().sha256
        with store.transaction(by="op", reason="other change") as m:
            m.set("video.tail_trim", 4, by="op")
        with pytest.raises(ConcurrentModificationError, match="re-review"):
            with store.transaction(by="op", reason="decision", expect_sha256=reviewed) as m:
                m.set("audio.capture", "P2", by="op")

    def test_verify_detects_outside_edit(self, tmp_path):
        store = new_store(tmp_path)
        doc = json.loads(store.path.read_text())
        doc["title"] = "edited by hand"
        store.path.write_text(json.dumps(doc))
        assert any("outside the store" in p for p in store.verify())

    def test_invalid_document_is_not_written(self, tmp_path):
        store = new_store(tmp_path)
        before = store.read().raw
        with pytest.raises(ManifestError), store.transaction(by="op", reason="bad") as m:
            m.doc["values"]["video.frames"] = {"value": "many"}
        assert store.read().raw == before
