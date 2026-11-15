from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from dependency_lock import LockError, read_lock, target_for  # noqa: E402
from doctor import parse_windows_preflight  # noqa: E402
from generate_runbooks import OUTPUTS, generated_text  # noqa: E402
from run_stage import STAGES, make_action, validate_command  # noqa: E402


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


class StageRunnerTests(unittest.TestCase):
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

    def test_windows_preflight_parser_extracts_gate_rows(self):
        rows = parse_windows_preflight("  [PASS] ffmpeg  path ok\n  [WARN] dvd_root  unset\n  [FAIL] python  import failed")
        self.assertEqual([r["status"] for r in rows], ["PASS", "WARN", "FAIL"])
        self.assertEqual(rows[-1]["check"], "python")


if __name__ == "__main__":
    unittest.main()
