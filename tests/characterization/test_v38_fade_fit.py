"""Characterization: v38 tests/test_fade_midpoint_fit.py (fit/pairing verbatim; detector
parsing adapted to the pure parsers)."""
from __future__ import annotations

import unittest

from fstk.media import fade_fit as fade


class FadeMidpointFitTests(unittest.TestCase):
    def test_exact_linear_fit_and_asetrate_convention(self):
        pairs = [(float(x), 3.0 + 1.001 * x) for x in range(7)]
        result = fade.fit(pairs, "synthetic")
        self.assertEqual(result["n"], 7)
        self.assertAlmostEqual(result["slope_b"], 1.001)
        self.assertAlmostEqual(result["theil_sen_b"], 1.001)
        self.assertAlmostEqual(result["asetrate_from_b"], 44100 / 1.001)

    def test_theil_sen_is_robust_to_one_outlier_and_uses_median_for_even_slope_count(self):
        pairs = [(float(x), 2.0 + 1.0002 * x) for x in range(5)]
        pairs[-1] = (pairs[-1][0], pairs[-1][1] + 20.0)
        result = fade.fit(pairs, "outlier")
        self.assertGreater(abs(result["slope_b"] - 1.0002), 0.1)
        self.assertAlmostEqual(result["theil_sen_b"], 1.0002)

    def test_fit_refuses_too_few_or_degenerate_pairs(self):
        self.assertEqual(fade.fit([(0.0, 1.0), (1.0, 2.0)], "few")["error"], "too few pairs")
        self.assertIn("degenerate", fade.fit([(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)], "same-x")["error"])

    def test_pairing_is_one_to_one_nearest_midpoint_and_skips_eof_silence(self):
        blacks = [(10.0, 12.0, 2.0), (20.0, 22.0, 2.0)]
        silences = [(10.5, 12.5, 2.0), (19.9, 22.1, 2.2), (39.96, 40.0, 0.04)]
        result = fade.pair_up(blacks, silences, 0.0, 40.0, 3.0)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0][0], 11.5)
        self.assertAlmostEqual(result[0][1], 11.0)
        self.assertAlmostEqual(result[1][0], 21.0)
        self.assertAlmostEqual(result[1][1], 21.0)

    def test_detection_parsers_accept_synthetic_ffmpeg_log(self):
        log = """\
[blackdetect] black_start:1.000 black_end:2.000 black_duration:1.000
[silencedetect] silence_start:3.000
[silencedetect] silence_end:4.000 | silence_duration:1.000
"""
        self.assertEqual(fade.parse_blacks(log, 0.4), [(1.0, 2.0, 1.0)])
        self.assertEqual(fade.parse_silences(log, 0.4), [(3.0, 4.0, 1.0)])

    def test_detector_filters_match_v38_command_lines(self):
        self.assertEqual(fade.blackdetect_filter(), "blackdetect=d=0:pix_th=0.10:pic_th=0.98")
        self.assertEqual(fade.silencedetect_filter(), "silencedetect=noise=-40dB:d=0.5")
