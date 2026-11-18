from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from manifest_io import atomic_replace, exclusive_backup, manifest_lock  # noqa: E402


class ManifestIOTests(unittest.TestCase):
    def test_lock_is_exclusive_and_released_on_context_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "manifest.lock"
            with manifest_lock(lock):
                self.assertTrue(lock.is_file())
                with self.assertRaises(FileExistsError):
                    with manifest_lock(lock):
                        pass
            self.assertFalse(lock.exists())

    def test_backup_is_exclusive_labelled_and_byte_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "episode.json"
            manifest.write_bytes(b"original")
            backup = exclusive_backup(manifest, b"original", label="test",
                                      error_type=RuntimeError)
            self.assertEqual(backup.read_bytes(), b"original")
            self.assertIn(".bak-test-", backup.name)
            self.assertEqual(manifest.read_bytes(), b"original")

    def test_atomic_replace_preserves_exact_error_messages_and_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "episode.json"
            original = b'{"value":1}\n'
            manifest.write_bytes(original)
            atomic_replace(manifest, b'{"value":2}\n', original,
                           error_type=RuntimeError, changed_message="changed",
                           pre_replace_message="changed before replace")
            self.assertEqual(manifest.read_bytes(), b'{"value":2}\n')
            with self.assertRaisesRegex(RuntimeError, "changed"):
                atomic_replace(manifest, b'{"value":3}\n', original,
                               error_type=RuntimeError, changed_message="changed",
                               pre_replace_message="changed before replace")
            self.assertEqual(manifest.read_bytes(), b'{"value":2}\n')


if __name__ == "__main__":
    unittest.main()
