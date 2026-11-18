from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from opencode_help import (create_help_audit, finalize_help_audit,
                           _posix_launcher, _windows_launcher)  # noqa: E402


class OpenCodeHelpAuditTests(unittest.TestCase):
    def test_incident_bundle_logs_prompt_snapshot_source_fix_and_protected_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            for folder in (root / "scripts", root / "tests", root / "episodes", root / "Versioning"):
                folder.mkdir(parents=True, exist_ok=True)
            source = root / "scripts" / "repair.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tests" / "test_repair.py").write_text("assert True\n", encoding="utf-8")
            retrospective = ("Versioning/WORKFLOW_VERSIONING.md",
                             "Versioning/V15-RELEASE.md", "Versioning/V32-APP.md")
            for name in retrospective:
                (root / name).write_text("retrospective placeholder\n", encoding="utf-8")
            (root / "episodes" / "S01E01.json").write_text("{}\n", encoding="utf-8")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({"episode": "S01E01"}), encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "add", "scripts", "tests", "episodes", "plan.json",
                            *retrospective],
                           cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "-m", "baseline"], cwd=root, capture_output=True, check=True)

            protected = Path(temporary) / "private-episode"
            protected.mkdir()
            manifest = protected / "S01E01.json"
            manifest.write_text('{"stack":{"frames":100}}\n', encoding="utf-8")
            workflow_log = protected / "workflow.log"
            workflow_log.write_text("REVIEW REQUIRED [review-stack]\n", encoding="utf-8")
            audit = create_help_audit(
                root, Path(temporary) / "app-logs", plan_path=plan_path,
                plan={"episode": "S01E01", "job_id": "audit-test"},
                step={"id": "review-stack", "for_stage": "render-stack",
                      "prompt": "Review stack evidence.", "inputs": [str(manifest)]},
                app_console="workflow output excerpt\n", workflow_log=workflow_log,
                workflow_state=None, protected_paths=[manifest])
            audit_dir = Path(audit["directory"])
            self.assertIn("fresh OpenCode session", audit["prompt"].read_text(encoding="utf-8"))
            prompt = audit["prompt"].read_text(encoding="utf-8")
            self.assertIn("retrospective release and app-integration records only", prompt)
            self.assertNotIn("--model", _windows_launcher())
            self.assertNotIn("--model", _posix_launcher(Path("audit"), Path("prompt.md"),
                                                          "opencode", "python", Path("helper.py")))
            self.assertTrue((audit_dir / "workflow.log").is_file())
            before_index = json.loads((audit_dir / "source-before-index.json").read_text(encoding="utf-8"))
            self.assertNotIn("episodes/S01E01.json", before_index)
            for name in retrospective:
                self.assertNotIn(name, before_index)

            source.write_text("VALUE = 2\n", encoding="utf-8")
            (root / "scripts" / "new_fix.py").write_text("FIXED = True\n", encoding="utf-8")
            manifest.write_text('{"stack":{"frames":999}}\n', encoding="utf-8")
            Path(audit["done"]).write_text(json.dumps({
                "exit_code": 0, "session_id": "ses-test", "session_export": "exported"}),
                encoding="utf-8")

            result = finalize_help_audit(root, audit, audit["protected_paths"])
            self.assertEqual(result["source_changes"], ["scripts/new_fix.py", "scripts/repair.py"])
            self.assertIn(str(manifest.resolve()), result["protected_evidence_changed"])
            self.assertTrue(result["requires_new_plan"])
            diff = (audit_dir / "AI-source-changes.diff").read_text(encoding="utf-8")
            self.assertIn("+VALUE = 2", diff)
            self.assertIn("+FIXED = True", diff)


if __name__ == "__main__":
    unittest.main()
