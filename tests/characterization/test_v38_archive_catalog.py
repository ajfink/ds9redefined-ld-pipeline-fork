"""Characterization: v38 tests/test_archive.py (7 tests), ported by tools/port_v38.py."""
from __future__ import annotations

import unittest

from fstk.catalog.archive import (
    CATALOG_SCHEMA,
    build_file_tree,
    classify_upload,
    identify_series,
    parse_episode_request,
    resolve_episode,
)


def small_catalog() -> dict:
    ds9 = {
        "identifier": "DS9-S02E18-LaserDisc-Copies",
        "title": "DS9-S02E18-LaserDisc-Copies",
        "url": "https://archive.org/details/DS9-S02E18-LaserDisc-Copies",
        "category": "episode-copy", "series": "DS9", "season": 2, "episode": 18,
        "capture_labels": ["L1", "M1", "P1"], "tbc_file_count": 3,
        "workflow_warnings": [],
        "download_inventory": {"selected_file_count": 9,
                                "selected_file_names": ["L1.tbc", "L1.tbc.json", "L1.efm"],
                                "missing_tbc_json_sidecars": [], "raw_efm_count": 3},
        "files": [{"name": "DS9-S02E18-L1.tbc", "selected_by_download_archive": True},
                  {"name": "DS9-S02E18-L1.tbc.json", "selected_by_download_archive": True},
                  {"name": "DS9-S02E18-L1.efm", "selected_by_download_archive": True}],
    }
    tng_extra = {
        "identifier": "TNG-Extra-LaserDisc-Copies",
        "url": "https://archive.org/details/TNG-Extra-LaserDisc-Copies",
        "category": "multi-episode-extra", "series": "TNG",
        "episode_groups": [{"season": 1, "episode": 24,
                            "title_candidates": ["We'll Always Have Paris"],
                            "files": [{"name": "TNG-S01E24-example.tbc"}]}],
    }
    return {"schema": CATALOG_SCHEMA,
            "source": {"account_url": "https://archive.org/details/@section_31",
                       "generated_at_utc": "2026-11-16T00:00:00Z"},
            "children": [{"items": [ds9]}, {"items": [tng_extra]}]}


class ArchiveCatalogTests(unittest.TestCase):
    def test_all_requested_series_aliases_and_star_trek_prefixes_resolve(self):
        cases = {
            "DS9": "DS9",
            "Deep Space Nine": "DS9",
            "Deep Space 9": "DS9",
            "Star Trek DS9": "DS9",
            "Trek Deep Space 9": "DS9",
            "VOY": "VOY",
            "Voyager": "VOY",
            "Star Trek Voyager": "VOY",
            "Trek VOY": "VOY",
            "TNG": "TNG",
            "The Next Generation": "TNG",
            "Next Generation": "TNG",
            "Star Trek: The Next Generation": "TNG",
            "Trek Next Generation": "TNG",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(identify_series(text), expected)

    def test_natural_prompt_and_sxxexx_forms_parse(self):
        self.assertEqual(parse_episode_request(
            "Download Episode 18 of season 2 of DS9"),
            {"series": "DS9", "season": 2, "episode": 18})
        self.assertEqual(parse_episode_request("Star Trek Voyager S01E01"),
                         {"series": "VOY", "season": 1, "episode": 1})
        with self.assertRaisesRegex(ValueError, "multiple series"):
            parse_episode_request("DS9 and Voyager season 1 episode 1")
        with self.assertRaisesRegex(ValueError, "include a season and episode"):
            parse_episode_request("Download DS9 episode 18")

    def test_item_identifiers_are_classified_without_inventing_missing_items(self):
        self.assertEqual(classify_upload("DS9-S02E18-LaserDisc-Copies"), {
            "kind": "episode-copy", "series": "DS9", "season": 2, "episode": 18})
        self.assertEqual(classify_upload("VOY-S05-LaserDisc-2-of-3"), {
            "kind": "season-stack", "series": "VOY", "season": 5,
            "part": 2, "parts": 3})
        self.assertEqual(classify_upload("TNG-Extra-LaserDisc-Copies"), {
            "kind": "multi-episode-extra", "series": "TNG"})

    def test_exact_standalone_item_returns_item_url_and_live_listonly_gate(self):
        result = resolve_episode(small_catalog(), "DS9", 2, 18)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["identifier"], "DS9-S02E18-LaserDisc-Copies")
        self.assertEqual(result["url"], "https://archive.org/details/DS9-S02E18-LaserDisc-Copies")
        self.assertIn("-ListOnly", result["next_step"])
        self.assertEqual(result["suggested_download_folder_name"], "DS9 S02E18 LaserDisc")
        self.assertEqual(result["catalog_item"]["capture_labels"], ["L1", "M1", "P1"])

    def test_tng_combined_extra_is_visible_but_never_auto_downloaded_for_one_episode(self):
        result = resolve_episode(small_catalog(), "TNG", 1, 24)
        self.assertEqual(result["status"], "combined-item-only")
        self.assertFalse(result["automatic_download_allowed"])
        self.assertEqual(result["candidates"][0]["identifier"], "TNG-Extra-LaserDisc-Copies")

    def test_unavailable_episode_returns_actual_coverage(self):
        result = resolve_episode(small_catalog(), "DS9", 6, 1)
        self.assertEqual(result["status"], "not-found")
        self.assertEqual(result["available_seasons"], [2])
        self.assertFalse(result["automatic_download_allowed"])

    def test_file_tree_keeps_item_relative_subdirectories_and_rejects_parent_paths(self):
        tree = build_file_tree([
            {"name": "capture/L1.tbc", "size": "12"},
            {"name": "capture/L1.tbc.json", "size": "2"},
            {"name": "root.efm", "size": "4"},
        ])
        self.assertEqual(tree["directories"][0]["name"], "capture")
        self.assertEqual(tree["directories"][0]["files"], ["L1.tbc", "L1.tbc.json"])
        with self.assertRaisesRegex(ValueError, "unsafe Archive metadata path"):
            build_file_tree([{"name": "../escape.tbc"}])
