from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import record_orc_reference_measurements as record  # noqa: E402

from support import temporary_directory  # noqa: E402


class OrcReferenceMeasurementRecordTests(unittest.TestCase):
    def _fixtures(self, root: Path):
        probe, logs = root / "probe", root / "logs"
        probe.mkdir()
        logs.mkdir()
        report = probe / "selection.md"
        report.write_text(
            "- Status: **SELECTED**\n"
            "| Pass | Sources | Input order | Suggested reference | Runner-up | Stack sigma | Coverage 1/2/3+ | Fit trusted | Saved/completed |\n"
            "|---:|---:|---|---|---|---:|---|---|---|\n"
            "| 1 | 3 | `L1, M1, P1` | `L1` (input 1) | `M1` | 1.2 | 5/19/83110 | True | True/True |\n"
            "Measured alignment map: `1+0, 2+5, 3+61`\n", encoding="utf-8")
        project = root / "stack.orcprj"
        project.write_text("verified\n", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "episode": "S01E18", "series": "DS9", "reference": "L1",
            "captures": [{"label": "L1"}, {"label": "M1"}, {"label": "P1"}],
            "alignment_map": "", "stack": {"frames": None},
        }, indent=2) + "\n", encoding="utf-8")
        ld = SimpleNamespace(root=lambda key: probe if key == "probe" else logs)
        episode = SimpleNamespace(
            code="S01E18", series="DS9", capture_set=["L1", "M1", "P1"], reference="L1")
        patches = (
            patch.object(record, "Ld", return_value=ld),
            patch.object(record, "load_episode", return_value=episode),
            patch.object(record, "episode_manifest_path", return_value=manifest),
            patch.object(record, "assert_writable_path", side_effect=lambda path, _ld: Path(path)),
        )
        return manifest, report, project, patches

    def test_records_report_measurements_with_verified_manifest_backup(self):
        with temporary_directory() as temporary:
            manifest, report, _project, patches = self._fixtures(Path(temporary))
            original = manifest.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            with patches[0], patches[1], patches[2], patches[3]:
                output, backup = record.record_measurements("S01E18", report, digest)

            updated = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(updated["alignment_map"], "1+0, 2+5, 3+61")
            self.assertEqual(updated["stack"]["frames"], 83134)
            self.assertIsNotNone(backup)
            self.assertEqual(backup.read_bytes(), original)

    def test_refuses_manifest_changed_since_render_plan_was_built(self):
        with temporary_directory() as temporary:
            manifest, report, _project, patches = self._fixtures(Path(temporary))
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaisesRegex(record.PipelineError, "changed after"):
                    record.record_measurements("S01E18", report, "0" * 64)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["alignment_map"], "")

    def test_refuses_report_reference_order_mismatch(self):
        with temporary_directory() as temporary:
            manifest, report, _project, patches = self._fixtures(Path(temporary))
            report.write_text(report.read_text(encoding="utf-8").replace(
                "`L1, M1, P1`", "`M1, L1, P1`"), encoding="utf-8")
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaisesRegex(record.PipelineError, "does not match"):
                    record.record_measurements("S01E18", report, digest)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["alignment_map"], "")


if __name__ == "__main__":
    unittest.main()
