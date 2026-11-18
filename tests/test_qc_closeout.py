from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from qc_report import qc_closeout_text, write_device_qc_closeout  # noqa: E402


class QcCloseoutTests(unittest.TestCase):
    def test_closeout_records_qc_pointer_and_source_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            retained = root / "ld-synced.flac"
            retained.write_bytes(b"audio")
            report = root / "qc.md"
            report.write_text("14 pass\n", encoding="utf-8")
            paths = {"ld_synced": retained, "dvd": root / "missing-dvd.mkv"}
            ep = SimpleNamespace(
                identity="DS9:S01E18",
                optional_path=lambda key: paths.get(key),
            )
            text = qc_closeout_text(ep, report, "a" * 64, "20261117T220000Z",
                                    (14, 0, 0, 0))
            self.assertIn("14 pass / 0 warn / 0 skip / 0 fail", text)
            self.assertIn("Detailed report SHA-256", text)
            self.assertIn("| `ld_synced` | present | 5 |", text)
            self.assertIn("| `dvd` | missing at QC closeout | - |", text)

    def test_shared_qc_closeout_is_unique_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            ep = SimpleNamespace(identity="DS9:S01E18", code="S01E18",
                                 optional_path=lambda _key: None)
            ld = SimpleNamespace(profile={"shared": {"enabled": True}},
                                 root=lambda name: logs,
                                 profile_path=root / "machine.json")
            report = root / "qc.md"
            report.write_text("report", encoding="utf-8")
            with patch("qc_report.assert_writable_path", side_effect=lambda path, _ld: Path(path)):
                result = write_device_qc_closeout(ld, ep, report, "b" * 64,
                                                  "20261117T220000Z", (14, 0, 0, 0))
            self.assertTrue(result.is_file())
            self.assertIn("qc-closeout-20261117T220000Z.md", result.name)
            self.assertIn("QC result", result.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
