from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dependency_lock  # noqa: E402
from dependency_lock import LockError, read_lock, target_for, version_relation  # noqa: E402
from doctor import parse_windows_preflight  # noqa: E402
from generate_runbooks import OUTPUTS, generated_text  # noqa: E402
from run_stage import (STAGES, make_action, requires_doctor, validate_command,
                       safe_working_directory, stack_render_inputs,
                       write_stage_report)  # noqa: E402


class DependencyLockTests(unittest.TestCase):
    def test_supported_target_mapping(self):
        self.assertEqual(target_for("Windows", "AMD64"), "windows-x86_64")
        self.assertEqual(target_for("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(target_for("Linux", "x86_64"), "linux-x86_64")
        with self.assertRaises(LockError):
            target_for("Linux", "aarch64")

    def test_each_lock_has_an_exact_python_and_direct_package_pins(self):
        for target in ("windows-x86_64", "macos-arm64", "linux-x86_64"):
            path, version, packages = read_lock(target)
            self.assertTrue(path.is_file())
            self.assertRegex(version, r"^\d+\.\d+\.\d+$")
            names = {name.casefold() for name, _ in packages}
            self.assertTrue({"numpy", "pillow"}.issubset(names))
            self.assertTrue(any(name.startswith("opencv-python") for name in names))

    def test_conda_recipes_repeat_the_platform_direct_package_pins(self):
        for target in ("macos-arm64", "linux-x86_64"):
            _, _, packages = read_lock(target)
            recipe = (ROOT / "dependencies" / f"environment-{target}.yml").read_text(encoding="utf-8")
            for name, version in packages:
                self.assertIn(f"{name}=={version}", recipe)

    def test_pep440_target_or_newer_policy_is_conservative(self):
        self.assertEqual(version_relation("5.0.0", "5.0.0"), "exact")
        self.assertEqual(version_relation("5.0.0.93", "5.0.0"), "newer")
        self.assertEqual(version_relation("5.0.0rc1", "5.0.0"), "older")
        self.assertEqual(version_relation("not-a-version", "5.0.0"), "unverified")
        self.assertEqual(version_relation(None, "5.0.0"), "missing")

    def test_lock_accepts_newer_importable_package_but_not_import_errors(self):
        lock_path = ROOT / "dependencies" / "requirements-macos-arm64.lock.txt"
        packages = [("opencv-python-headless", "5.0.0")]
        actual = {"python": "3.15.2", "executable": "python",
                  "packages": {"opencv-python-headless": "5.0.0.93"},
                  "import_errors": {}}
        with patch.object(dependency_lock, "read_lock",
                          return_value=(lock_path, "3.15.2", packages)), \
                patch.object(dependency_lock, "inspect_interpreter", return_value=actual):
            result = dependency_lock.compare_lock("macos-arm64", "python")
        self.assertTrue(result["ok"])
        self.assertEqual(result["package_version_status"]["opencv-python-headless"], "newer")
        self.assertEqual(result["newer_versions_accepted"], [{
            "package": "opencv-python-headless", "target": "5.0.0", "installed": "5.0.0.93"}])

        broken = actual | {"import_errors": {
            "opencv-python-headless": "ImportError: incompatible native library"}}
        with patch.object(dependency_lock, "read_lock",
                          return_value=(lock_path, "3.15.2", packages)), \
                patch.object(dependency_lock, "inspect_interpreter", return_value=broken):
            result = dependency_lock.compare_lock("macos-arm64", "python")
        self.assertFalse(result["ok"])
        self.assertTrue(any("actual_import_error" in item for item in result["mismatches"]))


class StageRunnerTests(unittest.TestCase):
    def test_stage_report_persists_active_and_gate_action_timings(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "stage.json"
            log = Path(tmp) / "stage.log"
            timing = {"active_elapsed_seconds": 12.5,
                      "gate_elapsed_seconds": 2.5,
                      "action_elapsed_seconds": 10.0,
                      "post_action_elapsed_seconds": 0.0,
                      "human_wait_seconds": 0.0}
            write_stage_report(report, Path(tmp) / "profile.json", "S01E99", "stack-reference",
                               ["tool", "--run"], "2026-11-15T00:00:00+00:00", "PASS", 0,
                               log, [], timing)
            saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(saved["timing"], timing)

    def test_download_skips_full_doctor_but_processing_writes_do_not(self):
        self.assertFalse(requires_doctor("download"))
        self.assertTrue(requires_doctor("init-episode"))
        self.assertTrue(requires_doctor("stack-reference"))
        self.assertTrue(requires_doctor("encode-final"))

    def test_default_stage_plan_is_allowlisted_and_argument_vector_based(self):
        with tempfile.TemporaryDirectory(prefix="Power Shell ") as tmp:
            ps = Path(tmp) / "pwsh.exe"
            ps.write_bytes(b"stub")
            profile = {"os": "windows", "tools": {"pwsh": str(ps),
                                                     "python": sys.executable},
                       "python": {"interpreter": sys.executable}}
            cmd = make_action("encode-final", "S01E99", profile, ["-Output", r"C:\A B\out.mkv"], None)
            self.assertEqual(cmd[0], str(ps))
            self.assertIn("encode_episode.ps1", cmd[cmd.index("-File") + 1])
            self.assertEqual(cmd[-2:], ["-Output", r"C:\A B\out.mkv"])
        self.assertIn("audio-promote-no-dvd", STAGES)

    def test_voy_init_action_carries_explicit_series_into_initializer(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        command = make_action("init-episode", "S01E01", profile,
                              ["--url", "https://archive.org/details/VOY-S01E01-LaserDisc-Copies"],
                              None, series="VOY")
        self.assertTrue(command[1].endswith("init_episode.py"))
        self.assertEqual(command[2:4], ["--series", "VOY"])

    def test_no_dvd_promotion_requires_unique_report_path(self):
        profile = {"os": "darwin", "python": {"interpreter": sys.executable}}
        with self.assertRaisesRegex(LockError, "--report-out"):
            make_action("audio-promote-no-dvd", "S01E99", profile, [], None)
        command = make_action("audio-promote-no-dvd", "S01E99", profile, [], "new.json")
        self.assertIn("--promote-dvd-free", command)
        self.assertIn("new.json", command)

    def test_linux_production_action_is_fail_closed(self):
        profile = {"os": "linux", "python": {"interpreter": sys.executable}}
        with self.assertRaisesRegex(LockError, "not enabled"):
            make_action("encode-final", "S01E99", profile, [], None)
        self.assertTrue(validate_command("S01E99", profile, "encode")[1].endswith("validate_episode.py"))

    def test_deterministic_helpers_and_corrected_pad_review_gates(self):
        profile = {"os": "windows", "python": {"interpreter": sys.executable},
                   "tools": {"pwsh": sys.executable}}
        self.assertIn("build-ld-synced", STAGES)
        self.assertIn("retime-assets", STAGES)
        self.assertIn("measure-pad-audio", STAGES)
        self.assertIn("measure-ld-black", STAGES)
        master = make_action("master-descriptors", "S01E99", profile,
                             ["--input-fps", "30000/1001"], None)
        self.assertEqual(master[1].endswith("extract_video_descriptors.py"), True)
        self.assertEqual(master[master.index("--source") + 1], "master")
        self.assertEqual(master[-2:], ["--source", "master"])
        self.assertIn("--input-fps", master)
        dvd = make_action("dvd-descriptors", "S01E99", profile, [], None)
        self.assertEqual(dvd[dvd.index("--source") + 1], "dvd")
        with self.assertRaisesRegex(LockError, "fixes argument.*--source"):
            make_action("master-descriptors", "S01E99", profile, ["--source", "dvd"], None)
        self.assertEqual(STAGES["fullres-check"]["gate"], "cadence")
        self.assertEqual(STAGES["rife-replacements"]["gate"], "cadence")
        self.assertEqual(STAGES["measure-pad-audio"]["gate"], "stack")
        self.assertEqual(STAGES["measure-ld-black"]["gate"], "stack")

    def test_retime_break_candidate_stages_are_allowlisted_and_doctor_gated(self):
        profile = {"os": "darwin", "python": {"interpreter": sys.executable}}
        prepare = make_action("prepare-retime-breaks", "S01E12", profile,
                              ["--ld-threshold", "15", "--report-out", "candidates.json"], None)
        self.assertTrue(prepare[1].endswith("prepare_retime_breaks.py"))
        self.assertEqual(prepare[prepare.index("--episode") + 1], "S01E12")
        self.assertIn("--report-out", prepare)
        record = make_action("record-retime-breaks", "S01E12", profile,
                             ["--candidate-report", "candidates.json"], None)
        self.assertTrue(record[1].endswith("record_retime_breaks.py"))
        self.assertNotIn("--apply", record)
        self.assertEqual(STAGES["prepare-retime-breaks"]["gate"], "stack")
        self.assertEqual(STAGES["record-retime-breaks"]["gate"], "inputs")
        self.assertTrue(requires_doctor("prepare-retime-breaks"))
        self.assertTrue(requires_doctor("record-retime-breaks"))

    def test_stack_render_is_space_gated_doctor_gated_and_resolves_manifest_paths(self):
        self.assertTrue(STAGES["stack-render"]["write"])
        self.assertEqual(STAGES["stack-render"]["gate"], "inputs")
        self.assertTrue(requires_doctor("stack-render"))
        planned = make_action("stack-render", "S01E15",
                              {"tools": {"orc": "orc-cli"}}, [], None)
        self.assertEqual(planned, ["orc-cli", "<manifest stack_project>", "--process"])
        with self.assertRaisesRegex(LockError, "no arbitrary Orc arguments"):
            make_action("stack-render", "S01E15", {"tools": {"orc": "orc-cli"}},
                        ["--sink", "unsafe"], None)
        self.assertEqual(STAGES["efm-report-table"]["gate"], "inputs")
        self.assertFalse(STAGES["efm-report-table"]["write"])
        self.assertTrue(requires_doctor("verify-tfm-run"))
        self.assertEqual(STAGES["verify-tfm-run"]["gate"], "stack")
        profile = {"os": "windows", "python": {"interpreter": sys.executable}}
        efm = make_action("efm-report-table", "S01E18", profile, [], None)
        self.assertTrue(efm[1].endswith("efm_report_table.py"))
        tfm = make_action("verify-tfm-run", "S01E18", profile,
                          ["--session-log", "session.log"], None)
        self.assertTrue(tfm[1].endswith("validate_tfm_run.py"))
        self.assertIn("--session-log", tfm)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "stack.orcprj"
            master = root / "stack-master.mov"
            project.write_text("project", encoding="utf-8")
            master.write_text("not-yet-rendered", encoding="utf-8")
            class FakeLd:
                def __init__(self, _profile): pass
                def tool(self, name):
                    self_name = name
                    if self_name != "orc":
                        raise AssertionError(self_name)
                    return root / "orc-cli"
            ep = type("Ep", (), {
                "identity": "DS9:S01E15",
                "data": {"reference": "M1"},
                "path": lambda self, key: {"stack_project": project,
                                           "stack_master": master}[key],
            })()
            env = {"LDP_PROFILE": str(root / "machine.json"),
                   "LDP_INSTANCE_ROOT": str(root / "devices" / "DEVICE-A1B2C3D4" /
                                             "episodes" / "S01E15"),
                   "LDP_INSTANCE_MODE": "shared-release",
                   "LDP_SERIES": "DS9", "LDP_EPISODE": "S01E15",
                   "LDP_IDENTITY": "DS9:S01E15"}
            prior_instance = os.environ.get("LDP_INSTANCE_ROOT")
            with patch("run_stage.Ld", FakeLd), \
                    patch("run_stage.load_episode", return_value=ep):
                command, resolved_project, resolved_master = stack_render_inputs(
                    root / "machine.json", "S01E15", "DS9",
                    Path(env["LDP_INSTANCE_ROOT"]), env)
            self.assertEqual(command, [str(root / "orc-cli"), str(project), "--process"])
            self.assertEqual(resolved_project, project)
            self.assertEqual(resolved_master, master)
            self.assertEqual(os.environ.get("LDP_INSTANCE_ROOT"), prior_instance)

    def test_shared_subprocess_cwd_defaults_local_and_rejects_release_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release"
            work = root / "local" / "work"
            release.mkdir()
            work.mkdir(parents=True)
            profile = {"shared": {"enabled": True, "release_root": str(release)},
                       "roots": {"work": str(work)}}
            self.assertEqual(safe_working_directory(profile, True, None), Path.home().resolve())
            self.assertEqual(safe_working_directory(profile, True, str(work)), work.resolve())
            with self.assertRaisesRegex(LockError, "outside the README/release tree"):
                safe_working_directory(profile, True, str(release))


class RunbookGeneratorTests(unittest.TestCase):
    def test_both_generated_variants_include_canonical_source_and_model_policy(self):
        for filename in OUTPUTS:
            output = generated_text(filename)
            self.assertEqual((ROOT / filename).read_text(encoding="utf-8"), output)
            self.assertIn("AUTO-GENERATED", output)
            self.assertIn("V6 audio decision", output)
            self.assertIn("Model operating contract", output)
            self.assertNotIn("{{CANONICAL_RUNBOOK}}", output)
        self.assertIn("fallback", generated_text("DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md").casefold())
        canonical = (ROOT / "DS9 LaserDisc OpenCode Runbook.md").read_text(encoding="utf-8")
        self.assertIn("audio/<episode>/<capture>-efm.wav", canonical)
        self.assertIn("efm_paths.py", canonical)
        self.assertIn("sign/scale convention", canonical)

    def test_windows_preflight_parser_extracts_gate_rows(self):
        rows = parse_windows_preflight("  [PASS] ffmpeg  path ok\n  [WARN] dvd_root  unset\n  [FAIL] python  import failed")
        self.assertEqual([r["status"] for r in rows], ["PASS", "WARN", "FAIL"])
        self.assertEqual(rows[-1]["check"], "python")


if __name__ == "__main__":
    unittest.main()
