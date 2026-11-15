from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from generate_tfm_log import build_avs_script, build_vpy_script, count_tfm_decisions, redact_shared_text, tfm_frame_indices, write_avs_script  # noqa: E402
from ldcommon import redact_shared_text as redact_with_ld  # noqa: E402
from dependency_lock import LockError  # noqa: E402
from run_stage import STAGES, make_action  # noqa: E402


class TfmLogPreparationTests(unittest.TestCase):
    def test_counts_both_supported_pp0_and_flagged_tfm_log_rows(self):
        text = "0 c [12]\n1 p + [81]\n2 c - [3]\nnot a frame row\n"
        self.assertEqual(count_tfm_decisions(text), (3, 0, 2))
        self.assertEqual(tfm_frame_indices(text), [0, 1, 2])

    def test_builds_windows_avs_with_measured_pad_and_parameters(self):
        source = Path(r"D:\laser discs\stack.mov")
        log = Path(r"D:\probe\tfm.log")
        text = build_avs_script(source, 37, log, Path(r"D:\plugins\loader.avsi"))
        self.assertIn(r'Import("D:\\plugins\\loader.avsi")', text)
        self.assertIn(r'LSMASHVideoSource("D:\\laser discs\\stack.mov").Trim(37, 0)', text)
        self.assertIn("TFM(order=-1, pp=0, chroma=true, slow=2", text)

    def test_writes_windows_avs_without_utf8_bom(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "tfm-probe.avs"
            write_avs_script(script, Path(r"D:\stack.mov"), 32, Path(r"D:\tfm.log"))
            contents = script.read_bytes()

        self.assertFalse(contents.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(contents.decode("utf-8"), build_avs_script(
            Path(r"D:\stack.mov"), 32, Path(r"D:\tfm.log")))

    def test_tfm_report_uses_ld_aware_shared_path_redaction(self):
        self.assertIs(redact_shared_text, redact_with_ld)

    def test_builds_mac_vpy_with_audio_pad_and_field_order(self):
        text = build_vpy_script(Path("/media/stack.mov"), 41, Path("/probe/tfm.log"))
        self.assertIn("core.std.Trim(clip, 41, None)", text)
        self.assertIn("_FieldBased=2", text)
        self.assertIn("core.tivtc.TFM(clip, order=-1, PP=0, chroma=1, slow=2", text)
        self.assertIn("tfm.log", text)

    def test_helper_stages_are_review_gated_at_stack_boundaries(self):
        self.assertIn("tfm-log", STAGES)
        self.assertTrue(STAGES["render-stack"]["review_gate"])
        self.assertTrue(STAGES["build-map"]["review_gate"])
        self.assertTrue(STAGES["cadence-analysis"]["review_gate"])

    def test_mac_cadence_uses_combined_analysis_adapter(self):
        profile = {"os": "darwin", "python": {"interpreter": sys.executable}}
        action = make_action("cadence-analysis", "S01E07", profile, ["--report-out", "/tmp/cadence.json"], None)
        self.assertTrue(action[1].endswith("cadence_analysis_mac.py"))
        self.assertEqual(action[-2:], ["--report-out", "/tmp/cadence.json"])
        vpy = make_action("cadence-vpy", "S01E07", profile, ["--repair", "qtgmc"], None)
        self.assertTrue(vpy[1].endswith("build_map_vpy.py"))
        with self.assertRaisesRegex(LockError, "cadence-analysis"):
            make_action("build-map", "S01E07", profile, [], None)


if __name__ == "__main__":
    unittest.main()
