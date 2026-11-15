from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.init_episode import episode_files, title_from_capture  # noqa: E402


class InitEpisodeFileKeyTests(unittest.TestCase):
    def test_capture_independent_audio_and_avs_paths_are_initialized(self):
        files = episode_files("S01E07", "Q-Less")
        for key in ("avs_log", "avs_trim", "dvd51_source"):
            self.assertIn(key, files)
        self.assertEqual(files["dvd51_source"]["name"], "S01E07-Q-Less-dvd-51-original.ac3")

    def test_mac_profile_uses_vapoursynth_script_extensions(self):
        files = episode_files("S01E10", "Move Along Home", ".vpy")
        self.assertEqual(files["avs_encode"]["name"], "S01E10-encode.vpy")
        self.assertEqual(files["avs_padfix"]["name"], "S01E10-padfix.vpy")

    def test_no_dvd_title_can_be_derived_from_verified_capture_name(self):
        captures = [{"label": "L1", "tbc_file": "DS9-S01E07-Q-Less-L1.tbc"}]
        self.assertEqual(title_from_capture(captures, "S01E07"), "Q-Less")

    def test_capture_and_rate_dependent_paths_remain_unset_at_init(self):
        files = episode_files("S01E07", "Q-Less")
        self.assertNotIn("ld_native", files)
        self.assertNotIn("ld_corrected", files)


if __name__ == "__main__":
    unittest.main()
