from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from retime_dvd_assets import (VERIFY_NOISE_DB, segment_chain,  # noqa: E402
                               silencedetect_filter,
                               verify_expected_gaps)


class SegmentChainPinTests(unittest.TestCase):
    """atempo output-length drift must be pinned per segment (S01E12 evidence).

    Measured: decode+atrim is sample-exact; `atempo=0.998868327` alone loses
    68 samples per 4.8 M (-14 ppm) and the A0 chain loses 267 (-31 ppm). Across
    S01E12's 18 segments this accumulated -76 ms and broke the 70 ms position
    tolerance at B16/B17.
    """

    def test_chain_pins_output_duration_after_atempo(self):
        chain = segment_chain(3, 1.004630, 180.914000, 0.998868327, 180.113200)
        self.assertTrue(chain.startswith("[src3]atrim=start=1.004630:end=180.914000"))
        self.assertIn("asetpts=PTS-STARTPTS", chain)
        self.assertIn("atempo=0.998868327", chain)
        # the pin: pad short output, trim long output, to exactly dst_span
        self.assertIn("apad=whole_dur=180.113200", chain)
        self.assertIn("atrim=duration=180.113200", chain)
        self.assertTrue(chain.endswith("[a3]"))
        # ordering: pin must come after atempo
        self.assertLess(chain.index("atempo="), chain.index("apad="))
        self.assertLess(chain.index("apad="), chain.index("atrim=duration="))

    def test_chain_keeps_tempo_ratio_unchanged(self):
        # the pin may not alter the adjudicated src/dst tempo
        chain = segment_chain(0, 0.0, 10.0, 1.000000000, 9.999990)
        self.assertIn("atempo=1.000000000", chain)
        self.assertIn("apad=whole_dur=9.999990", chain)


class VerificationThresholdTests(unittest.TestCase):
    """Fix B: -40 dB merged quiet post-break content into planned gaps (S01E12 B1)."""

    def test_scan_threshold_is_50db(self):
        self.assertEqual(VERIFY_NOISE_DB, -50)
        self.assertEqual(silencedetect_filter(),
                         "silencedetect=noise=-50dB:d=0.015")

    def test_s01e12_b1_quiet_ambience_shape_fails_duration(self):
        # At -40 dB the detected run extended 1.223 s into the scene opening;
        # such a merge must FAIL the duration check (it is the defect Fix B
        # removes at the detection layer, not a tolerance to relax).
        result = verify_expected_gaps(
            [{"label": "B1", "start_s": 180.1132, "end_s": 183.3831,
              "duration_s": 3.2699}],
            [(180.107667, 184.600708)],
        )
        self.assertEqual(result["gaps"][0]["status"], "FAIL")
        self.assertGreater(abs(result["gaps"][0]["duration_delta_s"]), 0.070)

    def test_cumulative_position_drift_fails_tolerance(self):
        # S01E12 B16/B17: gaps sample-exact but placed -76.4 ms early.
        result = verify_expected_gaps(
            [{"label": "B16", "start_s": 2670.7344, "end_s": 2671.3018,
              "duration_s": 0.5674}],
            [(2670.6580, 2671.2254)],
        )
        row = result["gaps"][0]
        self.assertEqual(row["status"], "FAIL")
        self.assertAlmostEqual(row["position_delta_s"], -0.0764, places=4)
        # duration is exact: the failure is positional only
        self.assertLess(abs(row["duration_delta_s"]), 0.001)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class PinnedChainDurationTests(unittest.TestCase):
    """End-to-end: the pinned chain delivers exactly dst_span; unpinned does not."""

    FF = shutil.which("ffmpeg")

    def _samples(self, chain: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.raw"
            cmd = [self.FF, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=100",
                   "-af", chain, "-f", "s16le", str(out)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
            return out.stat().st_size // 4  # stereo s16le = 4 B per frame

    def test_pinned_output_matches_dst_span_within_one_sample(self):
        tempo = 0.998868327
        ideal = round(4_800_000 / tempo)          # 4,805,438 samples
        dst_span = ideal / 48000.0
        chain = (f"asetpts=PTS-STARTPTS,atempo={tempo:.9f},"
                 f"apad=whole_dur={dst_span:.6f},atrim=duration={dst_span:.6f}")
        pinned = self._samples(chain)
        self.assertLessEqual(abs(pinned - ideal), 1,
                             f"pinned {pinned} vs ideal {ideal}")

    def test_unpinned_atempo_drifts_more_than_two_samples(self):
        # documents WHY the pin exists: raw atempo is short by 14-31 ppm
        tempo = 0.998868327
        ideal = round(4_800_000 / tempo)
        raw = self._samples(f"asetpts=PTS-STARTPTS,atempo={tempo:.9f}")
        self.assertGreaterEqual(ideal - raw, 2,
                                f"atempo delivered {raw} vs ideal {ideal}; "
                                "drift model needs re-measuring")


if __name__ == "__main__":
    unittest.main()
