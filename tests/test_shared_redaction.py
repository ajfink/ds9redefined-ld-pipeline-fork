from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from dependency_lock import redact_shared_text as redact_stage_text  # noqa: E402
from ldcommon import redact_shared_text as redact_report_text  # noqa: E402


class SharedRedactionTests(unittest.TestCase):
    def test_stage_redaction_keeps_json_valid_for_drive_root_and_nested_paths(self):
        report = {
            "detail": r"download folder not staged yet: D:\DS9 S01E09 LaserDisc",
            "work": r"C:\Swap Folder\work\logs\stage.log",
        }
        redactions = [
            (r"C:\Swap Folder\work", "<roots.work>"),
            ("D:\\", "<roots.downloads_root>"),
        ]

        redacted = redact_stage_text(json.dumps(report), redactions)
        decoded = json.loads(redacted)

        self.assertEqual(decoded["detail"],
                         r"download folder not staged yet: <roots.downloads_root>\DS9 S01E09 LaserDisc")
        self.assertEqual(decoded["work"], r"<roots.work>\logs\stage.log")

    def test_stage_redaction_handles_plain_text_without_losing_root_separator(self):
        source = r"download folder not staged yet: D:\DS9 S01E09 LaserDisc"
        self.assertEqual(
            redact_stage_text(source, [("D:\\", "<roots.downloads_root>")]),
            r"download folder not staged yet: <roots.downloads_root>\DS9 S01E09 LaserDisc")

    def test_ldcommon_report_redaction_round_trips_json_with_drive_root(self):
        with tempfile.TemporaryDirectory() as temp:
            ld = SimpleNamespace(
                profile_path=Path(temp) / "machine.json",
                profile={
                    "shared": {"enabled": True, "release_root": temp},
                    "roots": {"downloads_root": "D:\\"},
                },
            )
            report = {"error": r"not staged: D:\DS9 S01E09 LaserDisc"}

            redacted = redact_report_text(json.dumps(report), ld)
            decoded = json.loads(redacted)

        self.assertEqual(decoded["error"], r"not staged: <roots.downloads_root>\DS9 S01E09 LaserDisc")


if __name__ == "__main__":
    unittest.main()
