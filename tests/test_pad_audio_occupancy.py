from __future__ import annotations

import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from measure_pad_audio import (PcmWavWindowReader, analyze_pcm,
                               window_rms_dbfs)  # noqa: E402


class PadAudioOccupancyTests(unittest.TestCase):
    def test_rms_window_reports_audio_energy_in_dbfs(self):
        samples = np.full((1000, 2), 16384, dtype=np.int16)
        value, bounds = window_rms_dbfs(samples, 500, 100)
        self.assertAlmostEqual(value, -6.0206, places=3)
        self.assertEqual(bounds, (450, 550))

    def test_wav_reader_fetches_only_requested_pcm_window(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.wav"
            payload = (np.full((1000, 2), 8192, dtype="<i2")).tobytes()
            with wave.open(str(path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(44100)
                output.writeframes(payload)
            reader = PcmWavWindowReader(path)
            try:
                self.assertEqual(len(reader), 1000)
                value, bounds = window_rms_dbfs(reader, 500, 100)
                self.assertAlmostEqual(value, -12.0412, places=3)
                self.assertEqual(bounds, (450, 550))
            finally:
                reader.close()

    def test_audible_pad_windows_match_nearby_controls_without_rate_decision(self):
        rate = 44100
        samples = np.full((rate * 5, 2), 6000, dtype=np.int16)
        pad_frames = [round(1 * 30000 / 1001), round(3 * 30000 / 1001)]
        result = analyze_pcm(samples, rate, pad_frames, 0,
                             window_seconds=0.1, minimum_controls=4)
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(result["pad_summary"]["count"], 2)
        self.assertEqual(result["control_summary"]["count"], 4)
        self.assertAlmostEqual(result["median_pad_minus_control_db"], 0.0, places=6)
        self.assertIsNone(result["rate_recommendation"])
        self.assertFalse(result["manifest_updated"])

    def test_silent_pad_windows_are_distinguished_from_audible_controls(self):
        rate = 44100
        samples = np.full((rate * 5, 2), 8000, dtype=np.int16)
        frame = round(2 * 30000 / 1001)
        center = round((frame - 0) * rate / (30000 / 1001))
        samples[center - 2205:center + 2205] = 0
        result = analyze_pcm(samples, rate, [frame], 0,
                             window_seconds=0.1, minimum_controls=2)
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(result["pad_summary"]["median_dbfs"], -160.0)
        self.assertGreater(result["control_summary"]["median_dbfs"], -20.0)
        self.assertLess(result["median_pad_minus_control_db"], -100.0)

    def test_out_of_bounds_pad_or_too_few_controls_is_limited(self):
        samples = np.ones((44100, 2), dtype=np.int16)
        result = analyze_pcm(samples, 44100, [0], 0,
                             window_seconds=0.1, minimum_controls=10)
        self.assertEqual(result["status"], "LIMIT")
        self.assertEqual(result["pad_windows"][0]["status"], "out-of-audio-bounds")
        self.assertIsNone(result["pad_summary"])


if __name__ == "__main__":
    unittest.main()
