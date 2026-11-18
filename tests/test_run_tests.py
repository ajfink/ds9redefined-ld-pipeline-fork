from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_tests import configure_test_environment  # noqa: E402


class TestHarnessTests(unittest.TestCase):
    def test_temp_root_is_resolved_before_unittest_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary).resolve()
            alias = real.parent / (real.name + "-alias")
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            old_tempdir = tempfile.tempdir
            old_env = {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP")}
            try:
                with patch.object(tempfile, "gettempdir", return_value=str(alias)):
                    self.assertEqual(configure_test_environment(), real)
                    self.assertEqual(tempfile.tempdir, str(real))
                    for key in old_env:
                        self.assertEqual(os.environ[key], str(real))
            finally:
                tempfile.tempdir = old_tempdir
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                alias.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
