from __future__ import annotations

import json
import hashlib
import io
import sys
import tempfile
import time
import unittest
import scripts.select_stack_reference as selector  # noqa: E402
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.select_stack_reference import (  # noqa: E402
    PipelineError,
    CHECKPOINT_SCHEMA,
    _run_and_log,
    load_checkpoint,
    parse_refresh_report,
    restore_checkpoint_decisions,
    source_alignment_decision,
    validate_checkpoint,
    write_checkpoint,
)


def e05_l1_first_report() -> dict:
    """Minimal source-alignment excerpt from the preserved S01E05 Orc refresh."""
    return {
        "project": "stack-full-s01e05-l1first-refresh.orcprj",
        "completed": True,
        "saved": True,
        "nodes": [
            {
                "node": "Source Align",
                "stage": "source_align",
                "tool": "source_alignment",
                "status": "success",
                "statistics": {
                    "sourceCount": 4,
                    "suggestedReference": 2,
                    "referenceRunnerUp": 1,
                    "stackSigma": 1.489456721,
                    "equalSigma": 1.601192541,
                    "bestSingleSigma": 2.23750634,
                    "fitTrusted": True,
                    "fellBackToMean": False,
                    "pressingGroups": 2,
                    "worstMisfit": 0.3688925713,
                },
                "parameter_changes": {"alignmentMap": "1+2, 2+0, 3+35, 4+29"},
                "sources": [
                    {"id": 1, "label": "TBC Source L1", "stage": "tbc_source"},
                    {"id": 2, "label": "TBC Source M1", "stage": "tbc_source"},
                    {"id": 3, "label": "TBC Source P1", "stage": "tbc_source"},
                    {"id": 4, "label": "TBC Source P2", "stage": "tbc_source"},
                ],
            }
        ],
    }


class SelectStackReferenceTests(unittest.TestCase):
    def test_parses_json_report_after_orc_progress_text(self):
        payload = "refresh progress...\n" + json.dumps(e05_l1_first_report(), indent=2)
        self.assertTrue(parse_refresh_report([payload])["completed"])

    def test_maps_orc_suggestion_to_capture_label(self):
        decision = source_alignment_decision(
            e05_l1_first_report(), ["L1", "M1", "P1", "P2"]
        )
        self.assertEqual(decision["suggested_label"], "M1")
        self.assertEqual(decision["runner_up_label"], "L1")
        self.assertEqual(decision["alignment_map"], "1+2, 2+0, 3+35, 4+29")

    def test_rejects_incomplete_refresh(self):
        report = e05_l1_first_report()
        report["saved"] = False
        with self.assertRaises(PipelineError):
            source_alignment_decision(report, ["L1", "M1", "P1", "P2"])

    def test_rejects_source_count_mismatch(self):
        with self.assertRaises(PipelineError):
            source_alignment_decision(e05_l1_first_report(), ["L1", "M1", "P1"])

    def test_rejects_bad_recommendation_index(self):
        report = e05_l1_first_report()
        report["nodes"][0]["statistics"]["suggestedReference"] = 5
        with self.assertRaises(PipelineError):
            source_alignment_decision(report, ["L1", "M1", "P1", "P2"])

    def test_orc_output_is_streamed_to_log_and_heartbeat_is_visible(self):
        class SlowLines:
            def __init__(self):
                self.items = iter(["[1/7] source_alignment 10%\n"])

            def __iter__(self):
                return self

            def __next__(self):
                time.sleep(0.02)
                return next(self.items)

        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                self.stdout = SlowLines()
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "orc.log"
            fake_ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                      profile_path=Path(temp) / "machine.json")
            captured = io.StringIO()
            with patch("scripts.select_stack_reference.Ld", return_value=fake_ld), \
                    patch("scripts.select_stack_reference.subprocess.Popen", side_effect=FakeProcess), \
                    patch("scripts.select_stack_reference.ORC_HEARTBEAT_SECONDS", 0.001), \
                    redirect_stdout(captured):
                result = _run_and_log(["orc", "refresh"], log)
            self.assertEqual(result.returncode, 0)
            self.assertIn("source_alignment 10%", result.stdout)
            self.assertIn("source_alignment 10%", log.read_text(encoding="utf-8"))
            self.assertIn("[heartbeat]", captured.getvalue())

    def test_checkpoint_roundtrip_and_identity_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                 profile_path=root / "machine.json")
            checkpoint = root / "selection.checkpoint.json"
            state = {
                "schema": CHECKPOINT_SCHEMA,
                "selection_id": "20261116T120000000000Z",
                "episode": "S01E99",
                "workflow_version": "12",
                "manifest_sha256": "a" * 64,
                "manifest_non_reference_sha256": "d" * 64,
                "selected_manifest_sha256": None,
                "original_reference": None,
                "captures": ["L1", "M1"],
                "input_fingerprint": [{"name": "L1.tbc", "size": 10, "mtime_ns": 20}],
                "orc_fingerprint": {"size": 100, "mtime_ns": 30},
                "profile_name": "machine.json",
                "machine": "DEVICE-A",
                "profile_sha256": "c" * 64,
                "reselect": False,
                "dry_run": False,
                "decisions": [],
                "next_seed": "L1",
                "next_pass": 1,
                "selected": None,
                "stable_project_name": None,
                "finalized": False,
            }
            write_checkpoint(checkpoint, state, ld, create=True)
            loaded = load_checkpoint(checkpoint)
            already_committed = validate_checkpoint(
                loaded, episode="S01E99", workflow_version="12",
                manifest_sha256="a" * 64, manifest_non_reference_sha256="d" * 64,
                current_reference=None, original_reference=None,
                captures=["L1", "M1"], input_fingerprint=state["input_fingerprint"],
                orc_fingerprint=state["orc_fingerprint"], profile_name="machine.json",
                machine="DEVICE-A", profile_sha256="c" * 64,
                reselect=False, dry_run=False)
            self.assertFalse(already_committed)
            with self.assertRaisesRegex(PipelineError, "manifest changed"):
                validate_checkpoint(
                    loaded, episode="S01E99", workflow_version="12",
                    manifest_sha256="b" * 64, manifest_non_reference_sha256="d" * 64,
                    current_reference=None, original_reference=None,
                    captures=["L1", "M1"], input_fingerprint=state["input_fingerprint"],
                    orc_fingerprint=state["orc_fingerprint"], profile_name="machine.json",
                    machine="DEVICE-A", profile_sha256="c" * 64,
                    reselect=False, dry_run=False)

            committed = json.loads(json.dumps(loaded))
            committed["decisions"] = [{"pass": 1, "seed": "M1", "order": ["M1", "L1"],
                                       "decision": {"suggested_label": "M1"}}]
            committed.update({"next_pass": 2, "next_seed": None, "selected": "M1",
                              "selected_manifest_sha256": "b" * 64})
            self.assertTrue(validate_checkpoint(
                committed, episode="S01E99", workflow_version="12",
                manifest_sha256="b" * 64, manifest_non_reference_sha256="d" * 64,
                current_reference="M1", original_reference=None,
                captures=["L1", "M1"], input_fingerprint=state["input_fingerprint"],
                orc_fingerprint=state["orc_fingerprint"], profile_name="machine.json",
                machine="DEVICE-A", profile_sha256="c" * 64,
                reselect=False, dry_run=False))

    def test_resume_rehydrates_only_hash_verified_completed_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe, logs = root / "probe", root / "logs"
            probe.mkdir(); logs.mkdir()
            project = probe / "pass1.orcprj"
            project.write_bytes(b"saved Orc project")
            (logs / "orc1.log").write_text("saved refresh output", encoding="utf-8")
            (logs / "build1.log").write_text("built", encoding="utf-8")
            item = {"pass": 1, "seed": "L1", "order": ["L1", "M1"],
                    "decision": {"suggested_label": "M1", "source_count": 2},
                    "project_name": project.name,
                    "project_sha256": hashlib.sha256(project.read_bytes()).hexdigest(),
                    "refresh_log_name": "orc1.log", "builder_log_name": "build1.log",
                    "refresh_log_sha256": hashlib.sha256((logs / "orc1.log").read_bytes()).hexdigest(),
                    "builder_log_sha256": hashlib.sha256((logs / "build1.log").read_bytes()).hexdigest(),
                    "exit_code": 0}
            state = {"decisions": [item], "selected": None}
            decisions, stable = restore_checkpoint_decisions(state, probe, logs)
            self.assertEqual(stable, None)
            self.assertEqual(decisions[0]["log_path"], str(logs / "orc1.log"))
            project.write_bytes(b"changed project")
            with self.assertRaisesRegex(PipelineError, "missing or changed"):
                restore_checkpoint_decisions(state, probe, logs)

    def test_cli_resumes_after_last_completed_pass_without_repeating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads = root / "downloads"
            probe, logs, stack = root / "probe", root / "logs", root / "stack"
            for directory in (downloads, probe, logs, stack):
                directory.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"reference": ""}), encoding="utf-8")
            profile_path = root / "machine.json"
            profile_path.write_text(json.dumps({"machine": "DEVICE-A"}), encoding="utf-8")
            tbc_files = []
            captures = []
            for label in ("L1", "M1"):
                name = f"{label}.tbc"
                path = downloads / name
                path.write_bytes(label.encode("ascii"))
                tbc_files.append(path)
                captures.append({"label": label, "tbc_file": name,
                                 "tbc_md5": hashlib.md5(label.encode("ascii")).hexdigest()})
            orc = root / "orc-cli"
            orc.write_text("synthetic Orc executable", encoding="ascii")
            final_project = stack / "stack-full.orcprj"

            class FakeEpisode:
                def __init__(self):
                    self.code = "S01E99"
                    self.title = "Synthetic"
                    self.data = {"format": "one_part", "captures": captures}
                    self.capture_set = ["L1", "M1"]

                def downloads(self):
                    return downloads

                def path(self, key):
                    return {"stack_project": final_project,
                            "stack_master": stack / "master.mov"}[key]

            class FakeLd:
                def __init__(self):
                    self.profile = {"machine": "DEVICE-A", "shared": {"enabled": False}}
                    self.profile_path = profile_path

                def root(self, name):
                    return {"probe": probe, "logs": logs}[name]

                def tool(self, name):
                    self.tool_name = name
                    return orc

            build_calls = []
            refresh_seeds = []

            def build_scratch(_episode, seed, project_path, _sink, builder_log):
                build_calls.append(seed)
                if len(build_calls) == 2:
                    raise KeyboardInterrupt()
                project_path.write_text(seed, encoding="ascii")
                builder_log.write_text("synthetic builder log", encoding="utf-8")

            def refresh(_orc, project_path, refresh_log):
                seed = project_path.read_text(encoding="ascii")
                refresh_seeds.append(seed)
                refresh_log.write_text("synthetic Orc refresh log", encoding="utf-8")
                order = [seed] + [label for label in ("L1", "M1") if label != seed]
                suggested_index = 2 if seed == "L1" else 1
                report = {"completed": True, "saved": True, "nodes": [{
                    "stage": "source_align", "tool": "source_alignment", "status": "success",
                    "statistics": {"sourceCount": 2, "suggestedReference": suggested_index,
                                   "referenceRunnerUp": 1 if suggested_index == 2 else 2,
                                   "stackSigma": 1.0, "fitTrusted": True},
                    "parameter_changes": {"alignmentMap": "1+0, 2+0"},
                    "sources": [{"label": f"TBC Source {label}"} for label in order],
                }]}
                return report, 0

            def fake_validate(*_args, **_kwargs):
                return CompletedProcess([], 0, "inputs pass", "")

            def invoke(argv):
                with patch.object(selector, "Ld", FakeLd), \
                        patch.object(selector, "load_episode", return_value=FakeEpisode()), \
                        patch.object(selector, "episode_manifest_path", return_value=manifest), \
                        patch.object(selector.subprocess, "run", side_effect=fake_validate), \
                        patch.object(selector, "_build_scratch_project", side_effect=build_scratch), \
                        patch.object(selector, "_run_orc_refresh", side_effect=refresh), \
                        patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                    return selector.main()

            first_rc = invoke(["select_stack_reference.py", "--episode", "S01E99"])
            self.assertEqual(first_rc, 130)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["reference"], "")
            checkpoints = list(logs.glob("S01E99-reference-selection-*.checkpoint.json"))
            self.assertEqual(len(checkpoints), 1)
            checkpoint = checkpoints[0]
            first_state = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(first_state["next_pass"], 2)
            self.assertEqual([item["seed"] for item in first_state["decisions"]], ["L1"])

            resume_rc = invoke(["select_stack_reference.py", "--episode", "S01E99",
                                "--resume", str(checkpoint)])
            self.assertEqual(resume_rc, 0)
            self.assertEqual(refresh_seeds, ["L1", "M1"])
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["reference"], "M1")
            self.assertEqual(build_calls, ["L1", "M1", "M1"])
            final_state = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertTrue(final_state["finalized"])
            self.assertEqual(len(final_state["decisions"]), 2)


if __name__ == "__main__":
    unittest.main()
