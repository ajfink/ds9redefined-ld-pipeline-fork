from __future__ import annotations

import sys
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from episode_dashboard import next_gate_summary, parse_gate_output, save_gate_evidence  # noqa: E402


class EpisodeDashboardTests(unittest.TestCase):
    def test_validator_summary_keeps_limit_distinct_from_pass(self):
        result = parse_gate_output(
            "retime", 0,
            "[PASS] retime.breaks: 7 adjudicated\n"
            "[WARN] retime.B8: 18 ms below resolution; LIMIT\n"
            "S01E11 stage=retime: 1 pass, 1 warn, 0 fail\n")
        self.assertEqual(result["status"], "WARN / LIMIT")
        self.assertIn("1 warn/limit", result["summary"])

    def test_nonzero_without_gate_rows_is_a_stop(self):
        result = parse_gate_output("audio", 2, "manifest missing or unsafe\n")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("manifest missing", result["summary"])

    def test_skipped_gate_and_empty_success_output_are_not_passes(self):
        skipped = parse_gate_output("retime", 0, "[SKIP] retime: no DVD supplied\n")
        self.assertEqual(skipped["status"], "WARN / LIMIT")
        self.assertIn("1 skipped", skipped["summary"])
        empty = parse_gate_output("manifest", 0, "")
        self.assertEqual(empty["status"], "FAIL")

    def test_next_gate_summary_stops_at_first_failure_or_warn(self):
        results = {"manifest": parse_gate_output("manifest", 0, "[PASS] manifest: ok\n"),
                   "inputs": parse_gate_output("inputs", 0, "[PASS] inputs: ok\n"),
                   "stack": parse_gate_output("stack", 0, "[WARN] stack: pair count is limited\n")}
        summary = next_gate_summary(results)
        self.assertIn("Review the warning/limit at Stack; it is not a PASS.", summary)
        self.assertIn("Build next stack plan", summary)
        results["stack"] = parse_gate_output("stack", 1, "[FAIL] stack-master: missing\n")
        summary = next_gate_summary(results)
        self.assertIn("STOP at Stack: resolve its first failed check before later stages.", summary)
        self.assertIn("Build next stack plan", summary)

    def test_gate_check_saves_unique_log_and_hash_bound_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            log, report_path = save_gate_evidence(
                Path(temporary), "DS9:S01E12",
                {"stage": "retime", "status": "WARN / LIMIT", "return_code": 0,
                 "summary": "1 warn/limit", "output": "[WARN] B8: LIMIT\n"})
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "WARN / LIMIT")
            self.assertEqual(report["log_sha256"], hashlib.sha256(log.read_bytes()).hexdigest())
            self.assertIn("episode-gate-DS9-S01E12-retime-", log.name)


if __name__ == "__main__":
    unittest.main()
