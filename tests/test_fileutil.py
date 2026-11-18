from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fileutil import hash_file, md5_file, sha256_file  # noqa: E402


class FileHashTests(unittest.TestCase):
    def test_shared_hash_matches_standard_sha256_and_md5(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            payload = (b"hash fixture" * 911) + b"end"
            path.write_bytes(payload)
            self.assertEqual(sha256_file(path), hashlib.sha256(payload).hexdigest())
            self.assertEqual(md5_file(path), hashlib.md5(payload).hexdigest())
            self.assertEqual(hash_file(path, chunk_size=97), hashlib.sha256(payload).hexdigest())

    def test_chunk_callback_runs_once_per_nonempty_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(b"x" * 10)
            calls = []
            hash_file(path, chunk_size=4, on_chunk=lambda: calls.append(True))
            self.assertEqual(len(calls), 3)

    def test_rejects_nonpositive_chunk_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(b"data")
            with self.assertRaisesRegex(ValueError, "chunk size must be positive"):
                hash_file(path, chunk_size=0)


if __name__ == "__main__":
    unittest.main()
