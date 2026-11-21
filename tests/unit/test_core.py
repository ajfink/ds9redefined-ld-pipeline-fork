from __future__ import annotations

import hashlib
import json
import os
import threading

import pytest

from fstk.core.errors import FstkError, IdentityError
from fstk.core.fsio import (
    LockHeldError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    decode_text,
    exclusive_lock,
    read_lock,
)
from fstk.core.hashing import HEAD_TAIL_BYTES, SCOPE_FULL, SCOPE_HEAD_TAIL, Fingerprint, head_tail_sha256
from fstk.core.identity import EpisodeId
from fstk.core.redaction import Redactor


class TestIdentity:
    def test_bare_code_is_ds9(self):
        ep = EpisodeId.parse("s01e20")
        assert ep.canonical == "DS9:S01E20" and ep.artifact_component is None
        assert (ep.season, ep.number) == (1, 20)

    def test_series_embedded_and_explicit(self):
        assert EpisodeId.parse("VOY:S02E03").artifact_component == "VOY"
        assert EpisodeId.parse("S02E03", "voy").series == "VOY"
        with pytest.raises(IdentityError):
            EpisodeId.parse("VOY:S02E03", "DS9")

    @pytest.mark.parametrize("bad", ["", "S1E2", "XYZ:S01E01", "S01E01x", "DS9:"])
    def test_rejects(self, bad):
        with pytest.raises(IdentityError):
            EpisodeId.parse(bad)

    def test_slug(self):
        assert EpisodeId.parse("DS9:S01E20").slug == "ds9-s01e20"


class TestHashing:
    def test_small_file_is_full_scope(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x" * 1000)
        digest, scope = head_tail_sha256(p)
        assert scope == SCOPE_FULL and digest == hashlib.sha256(b"x" * 1000).hexdigest()

    def test_large_file_matches_v38_balanced_policy(self, tmp_path):
        p = tmp_path / "big.bin"
        size = 2 * HEAD_TAIL_BYTES + 12345
        data = os.urandom(size)
        p.write_bytes(data)
        digest, scope = head_tail_sha256(p)
        expected = hashlib.sha256(data[:HEAD_TAIL_BYTES] + data[-HEAD_TAIL_BYTES:]).hexdigest()
        assert scope == SCOPE_HEAD_TAIL and digest == expected

    def test_fingerprint_detects_change(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("one")
        fp = Fingerprint.of(p)
        assert fp.matches()
        p.write_text("two")
        assert not fp.matches()
        p.unlink()
        assert not fp.matches()

    def test_fingerprint_accepts_v38_scope_names(self):
        fp = Fingerprint.from_dict({"name": "x.tbc", "size_bytes": 5, "mtime_ns": 1, "sha256": "ab",
                                    "hash_scope": "first-last-4MiB"})
        assert fp.scope == SCOPE_HEAD_TAIL


class TestFsio:
    def test_atomic_write_and_lost_update_guard(self, tmp_path):
        p = tmp_path / "m.json"
        atomic_write_bytes(p, b"v1")
        atomic_write_bytes(p, b"v2", expected_original=b"v1")
        with pytest.raises(FstkError, match="changed on disk"):
            atomic_write_bytes(p, b"v3", expected_original=b"v1")
        assert p.read_bytes() == b"v2"
        assert not [x for x in tmp_path.iterdir() if x.name.endswith(".tmp")]

    def test_exclusive_write(self, tmp_path):
        p = tmp_path / "once"
        atomic_write_bytes(p, b"a", exclusive=True)
        with pytest.raises(FileExistsError):
            atomic_write_bytes(p, b"b", exclusive=True)
        assert p.read_bytes() == b"a"

    def test_canonical_json_is_stable_and_rejects_nan(self, tmp_path):
        assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})
        payload = atomic_write_json(tmp_path / "x.json", {"k": "é"})
        assert json.loads(payload) == {"k": "é"} and payload.endswith(b"\n")

    def test_decode_text_handles_utf16_tee_output(self):
        text = "frame=  123 fps=0\n"
        assert decode_text(text.encode("utf-16")) == text
        assert decode_text(text.encode("utf-16-le")) == text
        assert decode_text(b"\xef\xbb\xbf" + text.encode()) == text

    def test_lock_is_exclusive_and_reports_owner(self, tmp_path):
        lock = tmp_path / ".lock"
        with exclusive_lock(lock, purpose="unit test"):
            info = read_lock(lock)
            assert info.pid == os.getpid() and info.purpose == "unit_test"
            assert info.local_process_alive is True
            with pytest.raises(LockHeldError, match="owner is running"), exclusive_lock(lock):
                pass
        assert not lock.exists()

    def test_lock_contention_across_threads(self, tmp_path):
        lock = tmp_path / ".lock"
        wins, errors = [], []

        def attempt():
            try:
                with exclusive_lock(lock):
                    wins.append(1)
                    threading.Event().wait(0.05)
            except LockHeldError:
                errors.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(wins) >= 1 and len(wins) + len(errors) == 8


def test_redactor_longest_first_and_both_separators():
    r = Redactor({"Z:\\ld\\work": "<work>", "Z:\\ld": "<root>"})
    text = "wrote Z:\\ld\\work\\a.txt and Z:/ld/other and Z:\\ld\\work"
    assert r(text) == "wrote <work>\\a.txt and <root>/other and <work>"
