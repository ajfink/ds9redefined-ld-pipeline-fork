from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from efm_paths import EfmPathError, efm_output_paths, require_new_outputs  # noqa: E402


class EfmPathTests(unittest.TestCase):
    def test_same_capture_label_is_episode_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            first = efm_output_paths(temp, "S01E07", "L1")
            second = efm_output_paths(temp, "S01E08", "L1")
            self.assertNotEqual(first, second)
            self.assertEqual(first[0].relative_to(temp).as_posix(), "audio/S01E07/L1-efm.wav")
            self.assertEqual(first[1], Path(str(first[0]) + ".txt"))
            self.assertEqual(second[0].relative_to(temp).as_posix(), "audio/S01E08/L1-efm.wav")

    def test_existing_wav_or_report_refuses_without_modification(self):
        with tempfile.TemporaryDirectory() as temp:
            wav, report = efm_output_paths(temp, "S01E07", "M1")
            wav.parent.mkdir(parents=True)
            wav.write_bytes(b"prior decode")
            with self.assertRaisesRegex(EfmPathError, "refusing to overwrite"):
                require_new_outputs(wav, report)
            self.assertEqual(wav.read_bytes(), b"prior decode")
            wav.unlink()
            report.write_text("prior report", encoding="utf-8")
            with self.assertRaisesRegex(EfmPathError, "refusing to overwrite"):
                require_new_outputs(wav, report)
            self.assertEqual(report.read_text(encoding="utf-8"), "prior report")

    def test_invalid_episode_or_capture_path_components_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(EfmPathError, "episode"):
                efm_output_paths(temp, "episode", "L1")
            with self.assertRaisesRegex(EfmPathError, "capture"):
                efm_output_paths(temp, "S01E07", "../L1")


if __name__ == "__main__":
    unittest.main()
