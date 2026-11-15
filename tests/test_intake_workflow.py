from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_workflow  # noqa: E402
from run_workflow import PLAN_SCHEMA, run_plan  # noqa: E402


# This child process is a deterministic stage adapter for the intake integration
# test. It consumes a local Archive metadata fixture and never imports an HTTP or
# downloader library. The production orchestrator and its durable state/resume
# handling are exercised for real.
FAKE_INTAKE_STAGE = r'''from __future__ import annotations
import base64, csv, hashlib, json, os, pathlib, sys

argv = sys.argv[1:]
stage = argv[argv.index("--stage") + 1]
args = [value[len("--script-arg="):] for value in argv if value.startswith("--script-arg=")]
def option(name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None

fixture = json.loads(pathlib.Path(os.environ["LDP_TEST_ARCHIVE_FIXTURE"]).read_text(encoding="utf-8"))
calls = pathlib.Path(os.environ["LDP_TEST_CALLS"])
with calls.open("a", encoding="utf-8") as stream:
    stream.write(stage + "\n")

if stage == "download":
    url = option("-Url")
    destination = pathlib.Path(option("-Destination"))
    expected_url = "https://archive.org/details/" + fixture["identifier"]
    if url != expected_url:
        raise SystemExit("Archive URL did not match the mocked metadata item")
    files = fixture["files"]
    for row in files:
        content = base64.b64decode(row["content_b64"])
        if len(content) != row["size"] or hashlib.md5(content).hexdigest() != row["md5"]:
            raise SystemExit("bad test fixture metadata")
    destination.mkdir(parents=True, exist_ok=True)
    attempts_file = pathlib.Path(os.environ["LDP_TEST_ATTEMPTS"])
    attempts = int(attempts_file.read_text(encoding="utf-8")) + 1 if attempts_file.exists() else 1
    attempts_file.write_text(str(attempts), encoding="utf-8")
    first = destination / files[0]["name"]
    payload = base64.b64decode(files[0]["content_b64"])
    if attempts == 1:
        first.write_bytes(payload[:max(1, len(payload) // 2)])
        print("MOCK Archive metadata verified; simulated interrupted partial download", flush=True)
        raise SystemExit(1)
    for row in files:
        target = destination / row["name"]
        content = base64.b64decode(row["content_b64"])
        prior = target.read_bytes() if target.exists() else b""
        if prior != content[:len(prior)]:
            raise SystemExit("partial file does not match fixture; refusing unsafe resume")
        with target.open("ab") as stream:
            stream.write(content[len(prior):])
    with (destination / "download-manifest.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("Name", "Bytes", "MD5", "ArchiveId", "Status"))
        writer.writeheader()
        for row in files:
            writer.writerow({"Name": row["name"], "Bytes": row["size"], "MD5": row["md5"],
                             "ArchiveId": fixture["identifier"], "Status": "verified"})
    print("MOCK Archive response accepted; all fixture files verified", flush=True)

elif stage == "init-episode":
    url = option("--url")
    downloads = pathlib.Path(option("--downloads"))
    if url != "https://archive.org/details/" + fixture["identifier"]:
        raise SystemExit("initializer URL did not match fixture")
    if "--no-dvd" not in args or option("--cut-pad-images") != "skip":
        raise SystemExit("expected the explicitly selected no-DVD/skip fixture choices")
    with (downloads / "download-manifest.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != len(fixture["files"]):
        raise SystemExit("verified inventory count differs from fixture")
    for row, expected in zip(rows, fixture["files"]):
        target = downloads / row["Name"]
        if (row["Status"] != "verified" or row["ArchiveId"] != fixture["identifier"] or
                target.stat().st_size != int(expected["size"]) or
                hashlib.md5(target.read_bytes()).hexdigest() != expected["md5"]):
            raise SystemExit("input verification failed")
    manifest = pathlib.Path(os.environ["LDP_TEST_MANIFEST"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "episode": "S01E07", "title": fixture["title"],
        "archive_item": fixture["identifier"],
        "dvd_reference_available": False,
        "no_dvd_proceed_approved": True,
        "cut_pad_images_choice": "skip",
        "record_status": "draft",
    }, indent=2) + "\n", encoding="utf-8")
    print("MOCK episode manifest initialized", flush=True)

elif stage == "validate-inputs":
    downloads = pathlib.Path(os.environ["LDP_TEST_DOWNLOADS"])
    manifest = json.loads(pathlib.Path(os.environ["LDP_TEST_MANIFEST"]).read_text(encoding="utf-8"))
    if manifest["episode"] != "S01E07" or manifest["title"] != fixture["title"]:
        raise SystemExit("manifest identity check failed")
    with (downloads / "download-manifest.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != len(fixture["files"]) or any(row["Status"] != "verified" for row in rows):
        raise SystemExit("final intake validation failed")
    print("MOCK input validation passed", flush=True)
else:
    raise SystemExit("unexpected stage in mocked intake integration test: " + stage)

print("stage report: mocked-stage-report.json", flush=True)
'''


class MockedIntakeWorkflowTests(unittest.TestCase):
    def test_intake_order_mock_archive_interruption_resume_and_no_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            downloads = root / "downloads"
            manifest = root / "workspace" / "manifest" / "S01E07.json"
            profile = root / "machine.json"
            profile.write_text(json.dumps({
                "machine": "TEST-INTAKE", "os": "windows",
                "shared": {"enabled": False},
                "roots": {"logs": str(logs)},
            }), encoding="utf-8")
            profile_data = json.loads(profile.read_text(encoding="utf-8"))

            fixture_files = [
                ("DS9-S01E07-Q-Less-L1.tbc", b"small deterministic TBC fixture\n"),
                ("DS9-S01E07-Q-Less-L1.tbc.json", b'{"fixture":true}\n'),
                ("DS9-S01E07-Q-Less-L1.efm", b"small EFM fixture\n"),
            ]
            fixture = {
                "identifier": "DS9-S01E07-LaserDisc-Copies",
                "title": "Q-Less",
                "files": [{"name": name, "content_b64": base64.b64encode(content).decode("ascii"),
                           "size": len(content), "md5": hashlib.md5(content).hexdigest()}
                          for name, content in fixture_files],
            }
            fixture_path = root / "mock-archive-response.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            fake_stage = root / "mock_run_stage.py"
            fake_stage.write_text(FAKE_INTAKE_STAGE, encoding="utf-8")
            calls = root / "stage-calls.txt"
            attempts = root / "download-attempts.txt"

            test_env = {
                "LDP_TEST_ARCHIVE_FIXTURE": str(fixture_path),
                "LDP_TEST_CALLS": str(calls),
                "LDP_TEST_ATTEMPTS": str(attempts),
                "LDP_TEST_MANIFEST": str(manifest),
                "LDP_TEST_DOWNLOADS": str(downloads),
            }
            plan_path = root / "intake-plan.json"
            plan = {
                "schema": PLAN_SCHEMA,
                "episode": "S01E07",
                "episode_title": "Q-Less",
                "profile": str(profile),
                "decisions": [{"id": "dvd-mode", "value": "no-dvd"},
                              {"id": "cut-pad-images", "value": "skip"}],
                "steps": [
                    {"id": "download", "stage": "download", "resume_policy": "download",
                     "args": ["-Url", "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
                              "-Destination", str(downloads)],
                     "inputs": [], "outputs": [str(downloads)]},
                    {"id": "initialize", "stage": "init-episode",
                     "args": ["--url", "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
                              "--downloads", str(downloads), "--no-dvd", "--cut-pad-images", "skip"],
                     "inputs": [str(downloads)], "outputs": [str(manifest)]},
                    {"id": "validate-inputs", "stage": "validate-inputs",
                     "args": [], "inputs": [str(downloads), str(manifest)], "outputs": []},
                ],
            }
            plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            with patch.dict(os.environ, test_env):
                with patch.object(run_workflow, "load_profile",
                                  return_value=(profile, profile_data)):
                    with patch.object(run_workflow, "RUN_STAGE", fake_stage):
                        # The first transfer writes one partial fixture file and stops.
                        self.assertEqual(run_plan(plan_path, execute=True, resume=False), 1)
                        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["download"])
                        self.assertFalse(manifest.exists())

                        # Resume uses the same immutable plan and locally mocked Archive data.
                        self.assertEqual(run_plan(plan_path, execute=True, resume=True), 0)
                        self.assertEqual(run_plan(plan_path, execute=True, resume=True), 0)

            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(),
                             ["download", "download", "init-episode", "validate-inputs"])
            self.assertEqual(attempts.read_text(encoding="utf-8"), "2")
            self.assertTrue(manifest.is_file())
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["title"], "Q-Less")
            state_files = list(logs.glob("workflow-S01E07-q-less-*.json"))
            self.assertEqual(len(state_files), 1)
            state = json.loads(state_files[0].read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "PASS")
            self.assertEqual([row["status"] for row in state["steps"]], ["PASS", "PASS", "PASS"])
            self.assertIn("MOCK Archive response accepted", next(logs.glob("workflow-*.log")).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
