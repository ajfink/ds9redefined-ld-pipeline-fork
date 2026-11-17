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
                                build_archive_picker_tree,
                                build_retime_candidate_plan,
                                build_retime_candidate_record_plan,
                                choose_existing_profile, create_default_profile,
                                archive_series_from_url_identifier, default_profile_path,
                                episode_code_from_url_identifier,
                                load_archive_picker_catalog, resolve_archive_picker_selection,
                                read_intake_template, resolve_episode_code, resolve_episode_identity,
                                resolve_series_episode_identity,
                                resolve_local_source,
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
    profile = root / "machine.json"
    if not profile.exists():
        profile.write_text(json.dumps({"shared": {"enabled": False}, "roots": {}}),
                           encoding="utf-8")
    return {
        "series": "DS9",
        "episode": "S01E07",
        "confirmed_title": "Q-Less",
        "archive_url": "https://archive.org/details/DS9-S01E07-Copies",
        "profile": str(profile),
        "downloads_dir": str(root / "downloads"),
        "dvd_mode": "no-dvd",
        "dvd_path": "not-applicable",
        "cut_pad_images": "skip",
        "manifest_path": str(root / "workspace" / "manifest" / "S01E07.json"),
        "fingerprint_policy": "balanced",
    }


def create_local_download(root: Path, *, include_manifest: bool = True,
                          series: str = "DS9") -> tuple[Path, Path]:
    source = root / f"{series} S01E07 LaserDisc"
    source.mkdir(parents=True)
    files = [
        (f"{series}-S01E07-Q-Less-L1.tbc", b"small TBC fixture"),
        (f"{series}-S01E07-Q-Less-L1.tbc.json", b"small metadata fixture"),
        (f"{series}-S01E07-Q-Less-M1.tbc", b"second TBC fixture"),
        (f"{series}-S01E07-Q-Less-M1.tbc.json", b"second metadata fixture"),
    ]
    manifest = source / "download-manifest.csv"
    rows = []
    for name, payload in files:
        (source / name).write_bytes(payload)
        rows.append({"ArchiveId": f"{series}-S01E07-LaserDisc-Copies", "Status": "verified",
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

    def test_archive_identity_populates_human_episode_title_from_metadata_files_or_title(self):
        code, title, source = resolve_episode_identity(
            "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
            metadata_reader=lambda item: {
                "metadata": {"identifier": item, "title": item},
                "files": [{"name": "DS9-S01E07-Q-Less-L1.tbc"}],
            })
        self.assertEqual((code, title), ("S01E07", "Q-Less"))
        self.assertIn("Archive capture filename", source)

        code, title, source = resolve_episode_identity(
            "https://archive.org/details/DS9-Copies",
            metadata_reader=lambda _item: {
                "metadata": {"identifier": "DS9-Copies",
                             "title": "Deep Space Nine S01E08 Dax"},
                "files": [],
            })
        self.assertEqual((code, title), ("S01E08", "Dax"))
        self.assertIn("Archive metadata title", source)
        with self.assertRaisesRegex(WorkflowAppError, "does not provide a human episode title"):
            resolve_episode_identity(
                "https://archive.org/details/DS9-S01E07-LaserDisc-Copies",
                metadata_reader=lambda item: {"metadata": {"identifier": item, "title": item},
                                              "files": []})

    def test_archive_series_and_episode_identity_is_clarified_and_conflicts_are_rejected(self):
        url = "https://archive.org/details/VOY-S01E01-LaserDisc-Copies"
        self.assertEqual(archive_series_from_url_identifier(url), "VOY")
        series, episode, title, source = resolve_series_episode_identity(
            url, requested_series="VOY",
            metadata_reader=lambda item: {
                "metadata": {"identifier": item, "title": item},
                "files": [{"name": "VOY-S01E01-Caretaker-Part-I-L1.tbc"}],
            })
        self.assertEqual((series, episode, title), ("VOY", "S01E01", "Caretaker-Part-I"))
        self.assertIn("Archive capture filename", source)
        with self.assertRaisesRegex(WorkflowAppError, "selected series is DS9"):
            resolve_series_episode_identity(
                url, requested_series="DS9",
                metadata_reader=lambda item: {
                    "metadata": {"identifier": item, "title": item},
                    "files": [{"name": "VOY-S01E01-Caretaker-L1.tbc"}],
                })
        series, episode, title, _source = resolve_series_episode_identity(
            "https://archive.org/details/S01E01-LaserDisc-Copies",
            metadata_reader=lambda item: {
                "metadata": {"identifier": item, "title": "Star Trek Voyager S01E01"},
                "files": [{"name": "VOY-S01E01-Caretaker-L1.tbc"}],
            })
        self.assertEqual((series, episode, title), ("VOY", "S01E01", "Caretaker"))
        series, episode, title, _source = resolve_series_episode_identity(
            "https://archive.org/details/DS9-S02E02-LaserDisc-Copies",
            requested_series="DS9",
            metadata_reader=lambda item: {
                "metadata": {"identifier": item, "title": item},
                "files": [
                    {"name": "DS9-S02E02-The Circle-L1.tbc"},
                    {"name": "DS9-S02E02-The Circle-L3-BAD.tbc"},
                    {"name": "DS9-S02E02-The Circle-M1.tbc"},
                ],
            })
        self.assertEqual((series, episode, title), ("DS9", "S02E02", "The Circle"))
        with self.assertRaisesRegex(WorkflowAppError, "TNG combined/extra"):
            resolve_series_episode_identity(
                "https://archive.org/details/TNG-Extra-LaserDisc-Copies",
                requested_series="DS9",
                metadata_reader=lambda item: {"metadata": {"identifier": item, "title": item},
                                              "files": []})

    def test_voy_archive_intake_plan_persists_series_and_routes_series_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            values.update({"series": "VOY", "episode": "S01E01",
                           "archive_url": "https://archive.org/details/VOY-S01E01-LaserDisc-Copies",
                           "confirmed_title": "Caretaker, Part I",
                           "manifest_path": str(root / "episodes" / "VOY" / "S01E01.json")})
            plan = build_intake_plan(intake_template(), values)
            self.assertEqual(plan["series"], "VOY")
            self.assertEqual(plan["episode"], "S01E01")
            self.assertEqual(plan["identity"], "VOY:S01E01")
            self.assertEqual(plan["_fromstuntokill_intake"]["series"]["value"], "VOY")
            self.assertNotIn("_operator_inputs_EDIT", plan)
            self.assertNotIn("${input:", json.dumps(plan))
            init_args = plan["steps"][1]["args"]
            self.assertIn("--series", init_args)
            self.assertEqual(init_args[init_args.index("--series") + 1], "VOY")

    def test_local_voy_capture_prefix_clarifies_series_and_plan_preserves_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, tbc = create_local_download(root, include_manifest=False, series="VOY")
            info = resolve_local_source(tbc)
            self.assertEqual((info["series"], info["series_inferred"]), ("VOY", True))
            values = values_for(root)
            values.update({"source_mode": "local", "series": "VOY",
                           "local_source_path": str(tbc), "episode": "S01E07",
                           "manifest_path": str(root / "episodes" / "VOY" / "S01E07.json")})
            plan = build_intake_plan(intake_template(), values)
            self.assertEqual(plan["identity"], "VOY:S01E07")
            self.assertIn("--series", plan["steps"][0]["args"])
            self.assertEqual(plan["steps"][0]["args"][plan["steps"][0]["args"].index("--series") + 1], "VOY")

    def test_v15_retime_candidate_plan_is_series_aware_report_only_and_review_gated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            profile = root / "machine.json"
            profile.write_text(json.dumps({"shared": {"enabled": False}, "roots": {"probe": str(probe)}}),
                               encoding="utf-8")
            manifest = root / "S01E01.json"
            master = probe / "master.npy"
            alignment = probe / "alignment.npy"
            dvd = root / "dvd.mkv"
            for path in (manifest, master, alignment, dvd):
                path.write_bytes(b"synthetic evidence")
            context = {"series": "VOY", "episode": "S01E01", "identity": "VOY:S01E01",
                       "title": "Caretaker", "profile": profile, "manifest": manifest,
                       "master_descriptors": master, "alignment": alignment, "dvd": dvd,
                       "probe": probe, "instance_root": None}
            plan = build_retime_candidate_plan(context, {
                "ld_thresholds": [5, 10, 15], "ld_min_duration_s": "0.3",
                "ld_threshold": "15", "dvd_silence_noise_db": "-40",
                "dvd_silence_min_duration_s": "0.015", "pair_tolerance_s": "4",
                "max_offset_spread_frames": "4", "dvd_audio_selector": "",
            })
            self.assertEqual(plan["identity"], "VOY:S01E01")
            self.assertEqual([step.get("stage", step.get("kind")) for step in plan["steps"]],
                             ["measure-ld-black", "prepare-retime-breaks", "review"])
            prepare = plan["steps"][1]
            self.assertIn("--ld-threshold", prepare["args"])
            self.assertIn("--report-out", prepare["args"])
            self.assertFalse(any("record-retime-breaks" == step.get("stage") for step in plan["steps"]))
            self.assertTrue(plan["steps"][-1]["prompt"].find("report-only") >= 0)
            plan_path = root / "prepare-retime-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            loaded, _raw = load_plan(plan_path)
            self.assertEqual(loaded["identity"], "VOY:S01E01")

    def test_v15_retime_record_plan_binds_report_digest_and_requires_review(self):
        import hashlib

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "probe"
            probe.mkdir()
            profile = root / "machine.json"
            profile.write_text("{}\n", encoding="utf-8")
            manifest = root / "S01E12.json"
            manifest.write_text(json.dumps({"episode": "S01E12", "series": "DS9",
                                            "identity": "DS9:S01E12"}), encoding="utf-8")
            ld_report = probe / "S01E12-ld-black.json"
            ld_report.write_text("{}\n", encoding="utf-8")
            alignment = probe / "alignment.npy"
            alignment.write_bytes(b"alignment")
            dvd = root / "dvd.mkv"
            dvd.write_bytes(b"dvd bytes")
            report = {
                "schema": "ld-pipeline-retime-break-candidates-v1",
                "status": "REPORT_ONLY", "identity": "DS9:S01E12",
                "series": "DS9", "episode": "S01E12",
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "inputs": {"ld_black_report_relative_path": ld_report.name,
                           "ld_black_report_sha256": hashlib.sha256(ld_report.read_bytes()).hexdigest(),
                           "alignment_sha256": hashlib.sha256(alignment.read_bytes()).hexdigest(),
                           "dvd_name": dvd.name, "dvd_size_bytes": dvd.stat().st_size,
                           "dvd_mtime_ns": dvd.stat().st_mtime_ns, "dvd_duration_s": 120.0},
                "parameters": {"pair_tolerance_s": 4.0},
                "candidates": [
                    {"label": "B1", "pair_status": "unique-pair", "ld_black": [10.0, 11.0],
                     "pair_candidates": [{"id": "D1", "start_s": 10.0, "end_s": 11.0}]},
                    {"label": "B2", "pair_status": "no-dvd-silence", "ld_black": [20.0, 21.0],
                     "pair_candidates": []},
                ],
            }
            report_path = probe / "candidate-report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            context = {"series": "DS9", "episode": "S01E12", "identity": "DS9:S01E12",
                       "title": "The Wire", "profile": profile, "manifest": manifest,
                       "alignment": alignment, "dvd": dvd, "probe": probe, "instance_root": None}
            plan = build_retime_candidate_record_plan(
                context, report_path, ["B1"], ["B2"], "Reviewed synthetic pair evidence")
            self.assertEqual([step.get("stage", step.get("kind")) for step in plan["steps"]],
                             ["validate-inputs", "review", "record-retime-breaks"])
            record = plan["steps"][-1]
            self.assertIn("--apply", record["args"])
            self.assertEqual(record["args"][record["args"].index("--confirm-review") + 1], "DS9:S01E12")
            self.assertEqual(record["outputs"], [str(manifest.resolve())])
            self.assertIn("sha256:", json.dumps(plan["decisions"]))
            plan_path = root / "record-retime-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            loaded, _raw = load_plan(plan_path)
            self.assertEqual(loaded["series"], "DS9")

    def test_archive_picker_groups_series_season_episode_and_routes_only_safe_ds9_items(self):
        catalog = load_archive_picker_catalog(ROOT / "archive_catalog" / "section_31_tree.json")
        grouped = build_archive_picker_tree(catalog)
        self.assertEqual(list(grouped), ["DS9", "VOY", "TNG"])
        self.assertIn(1, grouped["DS9"])
        self.assertEqual(grouped["DS9"][1][0]["episode"], 1)
        self.assertEqual(grouped["DS9"][1][0]["title"], "Emissary, Part I")
        self.assertIn(5, grouped["VOY"])
        self.assertEqual({entry["episode"] for entries in grouped["TNG"].values()
                          for entry in entries}, {24, 1, 21, 15, 19})

        circle = next(item for section in catalog["children"] for item in section["items"]
                      if item.get("identifier") == "DS9-S02E02-LaserDisc-Copies")
        series, code, title, _source = resolve_series_episode_identity(
            circle["url"], requested_series="DS9",
            metadata_reader=lambda _item: {
                "metadata": {"identifier": circle["identifier"], "title": circle["title"]},
                "files": circle["files"],
            })
        self.assertEqual((series, code, title), ("DS9", "S02E02", "The Circle"))

        ds9 = resolve_archive_picker_selection(catalog, "DS9", 2, 18)
        self.assertTrue(ds9["picker_selectable"])
        self.assertEqual(ds9["url"], "https://archive.org/details/DS9-S02E18-LaserDisc-Copies")

        voyager = resolve_archive_picker_selection(catalog, "VOY", 1, 1)
        self.assertEqual(voyager["status"], "matched")
        self.assertTrue(voyager["picker_selectable"])
        self.assertEqual(voyager["workflow_identity"], "VOY:S01E01")
        self.assertTrue(voyager["workflow_route"]["supported"])

        combined = resolve_archive_picker_selection(catalog, "TNG", 1, 24)
        self.assertEqual(combined["status"], "combined-item-only")
        self.assertFalse(combined["picker_selectable"])
        self.assertIn("combined item", combined["picker_reason"])

        emissary = resolve_archive_picker_selection(catalog, "DS9", 1, 1)
        self.assertFalse(emissary["picker_selectable"])
        self.assertIn("two-part", emissary["picker_reason"])

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
                             str(tbc.resolve()))
            self.assertIn(str(folder.resolve()), plan["_fromstuntokill_intake"]["downloads_dir"]["value"])
            self.assertIn(str(tbc.resolve()), plan["steps"][0]["inputs"])
            self.assertEqual(plan["steps"][0]["outputs"], [str(root / "workspace" / "manifest" / "S01E07.json")])
            saved = write_new_plan(root / "local-plan.json", plan)
            loaded, _ = load_plan(saved)
            self.assertEqual(loaded["steps"][0]["stage"], "init-local-episode")

    def test_local_dvd_intake_needs_neither_title_nor_no_dvd_cut_pad_choice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, tbc = create_local_download(root, include_manifest=False)
            dvd = root / "Deep Space Nine - S01E07.mkv"
            dvd.write_bytes(b"DVD reference fixture")
            values = values_for(root)
            Path(values["profile"]).write_text(json.dumps({
                "shared": {"enabled": False}, "roots": {}}), encoding="utf-8")
            values.update({
                "source_mode": "local", "local_source_path": str(folder),
                "episode": "S01E07", "archive_url": "", "downloads_dir": "",
                "confirmed_title": "", "dvd_mode": "dvd-backed",
                "dvd_path": str(dvd), "cut_pad_images": "",
            })

            plan = build_intake_plan(intake_template(), values)
            placeholder = "Title not supplied (DS9:S01E07)"
            self.assertEqual(plan["episode_title"], placeholder)
            init_args = plan["steps"][0]["args"]
            self.assertEqual(init_args[init_args.index("--confirmed-title") + 1], placeholder)
            self.assertIn("--dvd", init_args)
            self.assertNotIn("--cut-pad-images", init_args)
            self.assertEqual(plan["_fromstuntokill_intake"]["cut_pad_images"]["value"],
                             "not-applicable")
            decision_ids = {row["id"] for row in plan["decisions"]}
            self.assertIn("episode-title-status", decision_ids)
            self.assertNotIn("episode-title-confirmed", decision_ids)
            self.assertIn("replace it from evidence", " ".join(plan["_instructions_DO_NOT_EDIT"]))

    def test_no_dvd_requires_cut_pad_choice_but_not_a_dvd_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = values_for(Path(temporary))
            values["dvd_path"] = ""
            plan = build_intake_plan(intake_template(), values)
            self.assertIn("--no-dvd", plan["steps"][1]["args"])
            values["cut_pad_images"] = ""
            with self.assertRaisesRegex(WorkflowAppError, "cut_pad_images"):
                build_intake_plan(intake_template(), values)

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
            self.assertEqual(plan["steps"][1]["outputs"], [values_for(root)["manifest_path"]])
            self.assertEqual(plan["steps"][2]["inputs"], [
                values_for(root)["downloads_dir"], values_for(root)["manifest_path"]])

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
            self.assertEqual(args[args.index("--dvd") + 1], str(dvd.resolve()))
            self.assertNotIn("--no-dvd", args)
            self.assertNotIn("--cut-pad-images", args)
            self.assertIn(str(dvd.resolve()), plan["steps"][1]["inputs"])
            self.assertIn(str(dvd.resolve()), plan["steps"][2]["inputs"])
            self.assertEqual(plan["fingerprint_policy"], "full")

            saved = write_new_plan(root / "dvd-plan.json", plan)
            loaded, _ = load_plan(saved)
            command_args = loaded["steps"][1]["args"]
            self.assertEqual(command_args[command_args.index("--dvd") + 1], str(dvd.resolve()))

    def test_archive_plan_uses_operator_selected_download_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            profile = Path(values["profile"])
            profile.write_text(json.dumps({"shared": {"enabled": False}, "roots": {}}),
                               encoding="utf-8")
            chosen = root / "user-selected" / "episode-files"
            values["downloads_dir"] = str(chosen)
            plan = build_intake_plan(intake_template(), values)
            saved = write_new_plan(root / "custom-download-plan.json", plan)
            loaded, _ = load_plan(saved)
            self.assertEqual(loaded["_fromstuntokill_intake"]["downloads_dir"]["value"],
                             str(chosen.resolve()))
            self.assertEqual(loaded["steps"][0]["args"][-1], str(chosen.resolve()))
            self.assertEqual(loaded["steps"][1]["args"][
                loaded["steps"][1]["args"].index("--downloads") + 1], str(chosen.resolve()))

    def test_shared_archive_download_destination_must_match_configured_profile_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = values_for(root)
            profile = Path(values["profile"])
            configured_root = root / "downloads"
            profile.write_text(json.dumps({
                "shared": {"enabled": True},
                "roots": {"downloads_root": str(configured_root),
                          "downloads_template": "DS9 {episode} LaserDisc"},
            }), encoding="utf-8")
            values["downloads_dir"] = str(root / "different-folder")
            with self.assertRaisesRegex(WorkflowAppError, "shared profile download destination"):
                build_intake_plan(intake_template(), values)

    def test_shared_profile_allows_existing_local_source_outside_download_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _tbc = create_local_download(root, include_manifest=False)
            dvd = root / "Deep Space Nine - S01E07.mkv"
            dvd.write_bytes(b"DVD evidence")
            values = values_for(root)
            configured_downloads = root / "configured-archive-downloads"
            Path(values["profile"]).write_text(json.dumps({
                "machine": "TEST", "os": "windows",
                "shared": {"enabled": True,
                           "release_root": str(root / "release"),
                           "device_name": "TEST"},
                "roots": {"downloads_root": str(configured_downloads),
                          "downloads_template": "DS9 {episode} LaserDisc"},
            }), encoding="utf-8")
            values.update({
                "source_mode": "local", "local_source_path": str(source),
                "episode": "S01E07", "archive_url": "", "downloads_dir": "",
                "confirmed_title": "", "dvd_mode": "dvd-backed",
                "dvd_path": str(dvd), "cut_pad_images": "",
            })

            plan = build_intake_plan(intake_template(), values)
            self.assertEqual(plan["steps"][0]["stage"], "init-local-episode")
            self.assertEqual(plan["steps"][0]["inputs"][0], str(source.resolve()))
            self.assertNotIn(str(configured_downloads), json.dumps(plan))

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
