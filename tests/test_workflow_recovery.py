from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workflow_recovery import discover_incomplete_workflows  # noqa: E402


class IncompleteWorkflowDiscoveryTests(unittest.TestCase):
    def test_finds_incomplete_jobs_with_matching_plans_and_sorts_last_completed_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            device = bundle / "devices" / "DEVICE-1234ABCD"
            profile = device / "local device config" / "machine.json"
            profile.parent.mkdir(parents=True)
            profile.write_text('{"machine":"DEVICE"}\n', encoding="utf-8")
            episode = device / "episodes" / "S01E18"
            plans, logs = episode / "plans", episode / "logs"
            plans.mkdir(parents=True)
            logs.mkdir()
            profile_digest = hashlib.sha256(profile.read_bytes()).hexdigest()

            def save_run(job_id: str, status: str, timestamp: str,
                         stage_status: str = "PASS", safe_retry: bool = False) -> None:
                plan = {
                    "schema": "ld-pipeline-workflow-plan-v1", "job_id": job_id,
                    "episode": "S01E18", "series": "DS9", "profile": str(profile),
                    "steps": [{"id": "select-reference", "stage": "stack-reference",
                               "inputs": [], "outputs": []},
                              {"id": "review-reference", "kind": "review",
                               "for_stage": "stack-reference", "prompt": "Review.",
                               "inputs": [], "outputs": []}],
                }
                plan_path = plans / f"{job_id}.json"
                raw = (json.dumps(plan, indent=2) + "\n").encode("utf-8")
                plan_path.write_bytes(raw)
                state = {
                    "schema": "ld-pipeline-workflow-state-v2", "job_id": job_id,
                    "episode": "S01E18", "series": "DS9", "status": status,
                    "fingerprint_policy": "balanced",
                    "plan_sha256": hashlib.sha256(raw).hexdigest(),
                    "profile_sha256": profile_digest,
                    "updated_utc": timestamp,
                    "steps": [
                        {"id": "select-reference", "kind": "stage", "stage": "stack-reference",
                         "status": stage_status, "finished_utc": timestamp},
                        {"id": "review-reference", "kind": "review", "stage": None,
                         "status": "WAITING_APPROVAL"},
                    ],
                }
                if safe_retry:
                    state["steps"][0]["input_fingerprints"] = []
                    state["steps"][0]["output_fingerprints_before"] = []
                (logs / f"workflow-{job_id}.json").write_text(
                    json.dumps(state, indent=2) + "\n", encoding="utf-8")

            save_run("old-run", "WAITING_APPROVAL", "2026-11-17T20:00:00+00:00")
            save_run("new-run", "BLOCKED", "2026-11-17T21:00:00+00:00")
            save_run("finished-run", "PASS", "2026-11-17T22:00:00+00:00")
            save_run("rejected-run", "REJECTED", "2026-11-17T23:00:00+00:00")
            save_run("failed-stage", "BLOCKED", "2026-11-17T23:30:00+00:00",
                     stage_status="FAILED")
            save_run("safe-failed-stage", "FAILED", "2026-11-17T23:45:00+00:00",
                     stage_status="FAILED", safe_retry=True)

            runs = discover_incomplete_workflows(profile, bundle)

            self.assertEqual([row["job_id"] for row in runs],
                             ["new-run", "old-run", "safe-failed-stage"])
            self.assertTrue(runs[0]["resumable"])
            self.assertTrue(runs[2]["resumable"])
            self.assertEqual(runs[0]["last_completed_step"], "stack-reference")
            self.assertEqual(runs[0]["last_completed_utc"], "2026-11-17T21:00:00+00:00")
            self.assertTrue(runs[0]["profile_matches"])
            self.assertEqual(runs[0]["plan_path"], plans / "new-run.json")

    def test_ignores_a_state_without_an_exact_saved_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            device = bundle / "devices" / "DEVICE-1234ABCD"
            profile = device / "local device config" / "machine.json"
            profile.parent.mkdir(parents=True)
            profile.write_text("{}\n", encoding="utf-8")
            logs = device / "episodes" / "S01E18" / "logs"
            logs.mkdir(parents=True)
            (logs / "workflow-orphan.json").write_text(json.dumps({
                "schema": "ld-pipeline-workflow-state-v2", "job_id": "orphan",
                "episode": "S01E18", "series": "DS9", "status": "BLOCKED",
                "plan_sha256": "missing", "steps": [],
            }), encoding="utf-8")

            self.assertEqual(discover_incomplete_workflows(profile, bundle), [])


if __name__ == "__main__":
    unittest.main()
