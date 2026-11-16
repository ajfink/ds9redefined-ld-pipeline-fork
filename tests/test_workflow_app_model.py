from __future__ import annotations

import csv
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_workflow import load_plan  # noqa: E402
from workflow_app_model import (WorkflowAppError, build_intake_plan,
                                choose_existing_profile, create_default_profile,
                                default_profile_path, episode_code_from_url_identifier,
                                read_intake_template, resolve_episode_code, resolve_local_source,
                                write_new_plan)  # noqa: E402


def intake_template() -> dict:
    fields = {
        "episode": "S01E07",
        "profile": "C:\\machine.json",
        "archive_url": "https://archive.org/details/DS9-S01E07-Copies",
        "downloads_dir": "D:\\downloads",
        "confirmed_title": "Q-Less",
        "dvd_mode": "no-dvd",
        "cut_pad_images": "skip",
        "manifest_path": "D:\\workspace\\manifest\\S01E07.json",
    }
    return {
        "schema": "ld-pipeline-workflow-plan-v1",
        "_instructions_DO_NOT_EDIT": ["Example intake template."],
        "_operator_inputs_EDIT": {
            key: {"value": value, "_comment": f"Input {key}."}
            for key, value in fields.items()
        },
        "episode": "${input:episode}",
        "episode_title": "${input:confirmed_title}",
        "profile": "${input:profile}",
        "fingerprint_policy": "balanced",
        "decisions": [
            {"id": "dvd-mode", "value": "${input:dvd_mode}"},
            {"id": "cut-pad-images", "value": "${input:cut_pad_images}"},
        ],
        "steps": [
            {"id": "download-source", "stage": "download", "resume_policy": "download",
             "args": ["-Url", "${input:archive_url}", "-Destination", "${input:downloads_dir}"],
             "inputs": [], "outputs": ["${input:downloads_dir}"]},
            {"id": "initialize-episode", "stage": "init-episode",
             "args": ["--url", "${input:archive_url}", "--downloads", "${input:downloads_dir}",
                      "--no-dvd", "--cut-pad-images", "${input:cut_pad_images}"],
             "inputs": ["${input:downloads_dir}"], "outputs": ["${input:manifest_path}"]},
            {"id": "validate-inputs", "stage": "validate-inputs",
             "inputs": ["${input:downloads_dir}", "${input:manifest_path}"], "outputs": []},
        ],
    }


def values_for(root: Path) -> dict[str, str]:
    return {
        "episode": "S01E07",
        "confirmed_title": "Q-Less",
        "archive_url": "https://archive.org/details/DS9-S01E07-Copies",
        "profile": str(root / "machine.json"),
        "downloads_dir": str(root / "downloads"),
        "dvd_mode": "no-dvd",
        "dvd_path": "not-applicable",
        "cut_pad_images": "skip",
        "manifest_path": str(root / "workspace" / "manifest" / "S01E07.json"),
        "fingerprint_policy": "balanced",
    }


def create_local_download(root: Path, *, include_manifest: bool = True) -> tuple[Path, Path]:
    source = root / "DS9 S01E07 LaserDisc"
    source.mkdir(parents=True)
    files = [
        ("DS9-S01E07-Q-Less-L1.tbc", b"small TBC fixture"),
        ("DS9-S01E07-Q-Less-L1.tbc.json", b"small metadata fixture"),
        ("DS9-S01E07-Q-Less-M1.tbc", b"second TBC fixture"),
        ("DS9-S01E07-Q-Less-M1.tbc.json", b"second metadata fixture"),
    ]
    manifest = source / "download-manifest.csv"
    rows = []
    for name, payload in files:
        (source / name).write_bytes(payload)
        rows.append({"ArchiveId": "DS9-S01E07-LaserDisc-Copies", "Status": "verified",
                     "Name": name, "Bytes": len(payload), "MD5": "0" * 32})
    if include_manifest:
        with manifest.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("ArchiveId", "Status", "Name", "Bytes", "MD5"))
            writer.writeheader()
            writer.writerows(rows)
    return source, source / files[0][0]


class WorkflowAppPlanTests(unittest.TestCase):
    def test_default_profile_locations_are_per_user_and_portable(self):
        home = Path("C:/Users/tester")
        windows = default_profile_path(system="Windows", env={"LOCALAPPDATA": "C:/Users/tester/AppData/Local"}, home=home)
        self.assertEqual(windows, Path("C:/Users/tester/AppData/Local/ld-pipeline/profiles/machine.json"))
        mac = default_profile_path(system="Darwin", env={"HOME": "/Users/tester"}, home=Path("/Users/tester"))
        self.assertTrue(str(mac).replace("\\", "/").endswith(
            "/Users/tester/Library/Application Support/ld-pipeline/profiles/machine.json"))
        linux = default_profile_path(system="Linux", env={"HOME": "/home/tester", "XDG_CONFIG_HOME": "/home/tester/.cfg"}, home=Path("/home/tester"))
        self.assertTrue(str(linux).replace("\\", "/").endswith(
            "/home/tester/.cfg/ld-pipeline/profiles/machine.json"))

    def test_default_profile_creation_uses_template_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_path = root / "profiles" / "machine.json"
            created, did_create = create_default_profile(
                profile_path, ROOT, system="Windows", hostname="TEST-HOST")
            self.assertTrue(did_create)
            data = json.loads(created.read_text(encoding="utf-8"))
            self.assertEqual(data["machine"], "TEST-HOST")
            self.assertEqual(data["os"], "windows")
            data["notes"] = ["preserve"]
            created.write_text(json.dumps(data), encoding="utf-8")
            same_path, did_create = create_default_profile(
                profile_path, ROOT, system="Windows", hostname="OTHER-HOST")
            self.assertFalse(did_create)
            self.assertEqual(json.loads(same_path.read_text(encoding="utf-8"))["notes"], ["preserve"])

    def test_legacy_profile_hint_is_auto_selected_from_default_profile_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "profiles"
            directory.mkdir()
            default = directory / "machine.json"
            legacy = directory / "machine-test.json"
            legacy.write_text(json.dumps({"machine": "TEST-HOST"}), encoding="utf-8")
            template = {"_operator_inputs_EDIT": {
                "profile": {"value": str(legacy), "_comment": "local"}}}
            self.assertEqual(choose_existing_profile(default, template, hostname="TEST-HOST"), legacy.resolve())

    def test_episode_code_is_inferred_from_archive_url_or_item_metadata(self):
        self.assertEqual(episode_code_from_url_identifier(
            "https://archive.org/details/DS9-S01E07-LaserDisc-Copies"), "S01E07")
        code, source = resolve_episode_code(
            "https://archive.org/details/DS9-LaserDisc-Copies",
            metadata_reader=lambda _item: {
                "metadata": {"identifier": "DS9-LaserDisc-Copies", "title": "Deep Space Nine S01E08 Dax"},
                "files": [{"name": "DS9-S01E08-Dax-L1.tbc"}],
            })
        self.assertEqual((code, source), ("S01E08", "Archive metadata"))
        with self.assertRaisesRegex(WorkflowAppError, "multiple episode codes"):
            resolve_episode_code("https://archive.org/details/DS9-Copies", metadata_reader=lambda _item: {
                "metadata": {"title": "S01E07 and S01E08"}, "files": []})

    def test_local_source_does_not_require_download_manifest_and_resolves_folder_or_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, tbc = create_local_download(root, include_manifest=False)
            from_folder = resolve_local_source(folder)
            from_file = resolve_local_source(tbc)
            self.assertEqual(from_folder["episode"], "S01E07")
            self.assertFalse(from_folder["download_manifest_present"])
            self.assertEqual(from_file["source_dir"], str(folder.resolve()))
            (folder / "DS9-S01E07-raw.lds").write_bytes(b"raw RF fixture")
            self.assertIn("DS9-S01E07-raw.lds", resolve_local_source(folder)["raw_rf_files"])
            with self.assertRaisesRegex(WorkflowAppError, "raw RF capture detected"):
                resolve_local_source(folder / "DS9-S01E07-raw.lds")
            raw_only = root / "DS9-S01E08-raw"
            raw_only.mkdir()
            (raw_only / "DS9-S01E08-raw.ldf").write_bytes(b"raw RF fixture")
            with self.assertRaisesRegex(WorkflowAppError, r"raw RF capture\(s\) detected"):
                resolve_local_source(raw_only)

    def test_local_intake_plan_skips_download_and_tracks_local_source_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, tbc = create_local_download(root, include_manifest=False)
            values = values_for(root)
            probe = root / "probe"
            probe.mkdir()
            Path(values["profile"]).write_text(json.dumps({
                "shared": {"enabled": False}, "roots": {"probe": str(probe)}}), encoding="utf-8")
            values.update({"source_mode": "local", "local_source_path": str(tbc),
                           "episode": "S01E07", "archive_url": "", "downloads_dir": ""})
            plan = build_intake_plan(intake_template(), values)
            self.assertEqual([step["stage"] for step in plan["steps"]],
                             ["init-local-episode", "validate-inputs"])
            self.assertEqual(plan["steps"][0]["args"][plan["steps"][0]["args"].index("--local-source") + 1],
                             "${input:local_source_path}")
            self.assertIn(str(folder.resolve()), plan["_operator_inputs_EDIT"]["downloads_dir"]["value"])
            self.assertIn(str(tbc.resolve()), plan["steps"][0]["inputs"])
            self.assertEqual(plan["steps"][0]["outputs"], ["${input:manifest_path}"])
            saved = write_new_plan(root / "local-plan.json", plan)
            loaded, _ = load_plan(saved)
            self.assertEqual(loaded["steps"][0]["stage"], "init-local-episode")

    def test_real_intake_template_builds_without_modifying_source(self):
        template_path = ROOT / "CONFIGURE HERE FIRST.example.json"
        before = template_path.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = read_intake_template(template_path)
            plan = build_intake_plan(template, values_for(root))
            saved = write_new_plan(root / "generated-plan.json", plan)
            loaded, _ = load_plan(saved)
            self.assertEqual(loaded["episode"], "S01E07")
            self.assertEqual([step["stage"] for step in loaded["steps"]],
                             ["download", "init-episode", "validate-inputs"])
        self.assertEqual(template_path.read_bytes(), before)

    def test_no_dvd_plan_matches_audit_choice_and_never_mutates_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = intake_template()
            original = copy.deepcopy(template)
            plan = build_intake_plan(template, values_for(root))

            self.assertEqual(template, original)
            args = plan["steps"][1]["args"]
            self.assertIn("--no-dvd", args)
            self.assertIn("--cut-pad-images", args)
            self.assertNotIn("--dvd", args)
            self.assertEqual(plan["steps"][1]["outputs"], ["${input:manifest_path}"])
            self.assertEqual(plan["steps"][2]["inputs"], [
                "${input:downloads_dir}", "${input:manifest_path}"])

    def test_dvd_plan_passes_exact_file_and_omits_no_dvd_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dvd = root / "Deep Space Nine - S01E07.mkv"
            dvd.write_bytes(b"fixture")
            values = values_for(root)
            values.update({"dvd_mode": "dvd-backed", "dvd_path": str(dvd),
                           "cut_pad_images": "skip", "fingerprint_policy": "full"})
            plan = build_intake_plan(intake_template(), values)

            args = plan["steps"][1]["args"]
            self.assertEqual(args[args.index("--dvd") + 1], "${input:dvd_path}")
            self.assertNotIn("--no-dvd", args)
            self.assertNotIn("--cut-pad-images", args)
            self.assertIn("${input:dvd_path}", plan["steps"][1]["inputs"])
            self.assertIn("${input:dvd_path}", plan["steps"][2]["inputs"])
            self.assertEqual(plan["fingerprint_policy"], "full")

            saved = write_new_plan(root / "dvd-plan.json", plan)
            loaded, _ = load_plan(saved)
            command_args = loaded["steps"][1]["args"]
            self.assertEqual(command_args[command_args.index("--dvd") + 1], str(dvd.resolve()))

    def test_refuses_missing_dvd_and_mismatched_or_non_archive_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            values.update({"dvd_mode": "dvd-backed", "dvd_path": str(root / "missing.mkv")})
            with self.assertRaisesRegex(WorkflowAppError, "DVD file does not exist"):
                build_intake_plan(intake_template(), values)
            values = values_for(root)
            values["archive_url"] = "https://example.com/details/DS9-S01E07"
            with self.assertRaisesRegex(WorkflowAppError, "archive.org/details"):
                build_intake_plan(intake_template(), values)

    def test_refuses_existing_manifest_and_plan_destinations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            manifest = Path(values["manifest_path"])
            manifest.parent.mkdir(parents=True)
            manifest.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowAppError, "existing manifest"):
                build_intake_plan(intake_template(), values)

            plan_path = root / "existing.json"
            plan_path.write_text('{"keep":true}', encoding="utf-8")
            with self.assertRaisesRegex(WorkflowAppError, "refusing to overwrite"):
                write_new_plan(plan_path, {"schema": "ld-pipeline-workflow-plan-v1"})
            self.assertEqual(plan_path.read_text(encoding="utf-8"), '{"keep":true}')


if __name__ == "__main__":
    unittest.main()
