from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldcommon import PipelineError  # noqa: E402
import scripts.ld_dvd_audio_compare as comparator  # noqa: E402
from scripts.ld_dvd_audio_compare import (  # noqa: E402
    SR,
    REPORT_SCHEMA,
    best_lag,
    compare_envelopes,
    load_dvd,
    load_ld,
    per_second_bands,
    per_second_rms,
    resolve_dvd_selector,
    serialize_report,
    summarize_bands,
    write_report,
)

FIXTURE = ROOT / "tests" / "fixtures" / "ld_dvd_audio_compare_e08_numeric.json"


class LdDvdAudioCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_sanitized_e08_envelope_regression(self):
        expected = self.fixture["expected"]
        result = compare_envelopes(
            self.fixture["ld_env_db"], self.fixture["dvd_env_db"], 120)
        self.assertEqual(result["envelope_best_lag_s"], 50)
        self.assertEqual(result["envelope_refined_lag_s"], 50)
        self.assertAlmostEqual(result["envelope_best_corr"],
                               expected["envelope_best_corr"], places=4)
        self.assertAlmostEqual(result["envelope_refined_corr"],
                               expected["envelope_refined_corr"], places=4)
        actual_diff = result["envelope_diff_db"]
        for metric in ("median", "mean", "sd"):
            with self.subTest(metric=metric):
                self.assertAlmostEqual(actual_diff[metric],
                                       expected["envelope_diff_db"][metric], delta=0.01)
        # The fixture contains envelopes rounded to 0.01 dB, so one boundary
        # sample may differ from the full-precision report's strict <3 dB count.
        self.assertLessEqual(abs(actual_diff["within_3db"] -
                                 expected["envelope_diff_db"]["within_3db"]), 1)
        self.assertEqual(result["aligned_seconds"], 400)
        self.assertEqual(REPORT_SCHEMA, "ld-dvd-audio-compare-v3")

    def test_lag_sign_and_aligned_window_for_positive_and_negative_shifts(self):
        rng = np.random.default_rng(20261115)
        content = rng.normal(size=1200)
        ld_has_lead = np.concatenate((np.zeros(7), content[:-7]))
        dvd_has_lead = np.concatenate((np.zeros(7), content[:-7]))
        self.assertEqual(best_lag(ld_has_lead, content, 20)[0], 7)
        self.assertEqual(best_lag(content, dvd_has_lead, 20)[0], -7)
        self.assertEqual(compare_envelopes(ld_has_lead, content, 20)["envelope_refined_lag_s"], 7)
        self.assertEqual(compare_envelopes(content, dvd_has_lead, 20)["envelope_refined_lag_s"], -7)

    def test_lag_search_rejects_negative_search_range(self):
        with self.assertRaisesRegex(PipelineError, "non-negative"):
            best_lag(np.ones(200), np.ones(200), -1)

    def test_band_summary_labels_unshifted_values_and_preserves_legacy_means(self):
        expected = self.fixture["expected"]
        ld = np.tile(expected["ld_band_mean"], (5, 1))
        dvd = np.tile(expected["dvd_band_mean"], (5, 1))
        summary = summarize_bands(ld, dvd, expected["band_lag_s"])
        self.assertEqual(summary["band_lag_corrected"], False)
        self.assertEqual(summary["band_lag_s"], 50)
        self.assertIn("NOT corrected", summary["band_scope"])
        self.assertEqual(summary["ld_band_mean"], expected["ld_band_mean"])
        self.assertEqual(summary["dvd_band_mean"], expected["dvd_band_mean"])
        self.assertEqual(summary["band_seconds_compared"], 5)

    def test_dvd_selector_prefers_cli_then_manifest_then_documented_default(self):
        episode = {"qc": {"dvd_audio_map": "0:a:2"}}
        self.assertEqual(resolve_dvd_selector(episode, "0:a:1"),
                         ("0:a:1", "command-line override"))
        self.assertEqual(resolve_dvd_selector(episode, None),
                         ("0:a:2", "manifest qc.dvd_audio_map"))
        self.assertEqual(resolve_dvd_selector({}, None),
                         ("0:a:1", "default (discrete DVD 5.1 stream)"))
        with self.assertRaisesRegex(PipelineError, "must not be empty"):
            resolve_dvd_selector({}, "  ")

    def test_per_second_features_have_expected_shapes_and_frequency_band(self):
        seconds = np.arange(SR * 2, dtype=np.float64) / SR
        tone = (12000 * np.sin(2 * np.pi * 1000 * seconds)).astype(np.float32)
        rms, rms_seconds = per_second_rms(tone)
        bands, band_seconds = per_second_bands(tone)
        self.assertEqual((rms_seconds, band_seconds), (2, 2))
        self.assertEqual(rms.shape, (2,))
        self.assertEqual(bands.shape, (2, 6))
        self.assertEqual(np.argmax(bands, axis=1).tolist(), [2, 2])
        np.testing.assert_allclose(bands.sum(axis=1), 1.0)

    def test_shared_report_serialization_redacts_local_paths_and_keeps_json_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = root / "private"
            private.mkdir()
            ld = SimpleNamespace(
                profile_path=private / "machine.json",
                profile={"shared": {"enabled": True, "release_root": str(root / "bundle")},
                         "roots": {"work": str(private)}},
            )
            document = {"dvd": str(private / "reference.mkv")}
            encoded = serialize_report(document, ld)
            decoded = json.loads(encoded)
            self.assertNotIn(str(private), encoded)
            self.assertIn("<roots.work>", decoded["dvd"])

    def test_report_is_created_without_overwriting_an_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compare.json"
            ld = SimpleNamespace(profile={"shared": {"enabled": False}},
                                 profile_path=Path(temp) / "machine.json")
            document = {"schema": "test", "value": 7}
            self.assertEqual(write_report(path, document, ld), path.resolve())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            with self.assertRaisesRegex(PipelineError, "refusing to overwrite"):
                write_report(path, {"value": 9}, ld)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)

    def test_ld_loader_rejects_non_pcm16(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "not-s16.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(1)
                wav.setframerate(SR)
                wav.writeframes(b"\x00" * SR)
            with self.assertRaisesRegex(PipelineError, "expected 16-bit PCM"):
                load_ld(path)

    def test_dvd_decode_is_sequential_and_rejects_malformed_pcm(self):
        payload = np.asarray([1, -2, 300], dtype=np.int16).tobytes()
        completed = SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        with patch("scripts.ld_dvd_audio_compare.subprocess.run", return_value=completed) as run:
            samples = load_dvd(Path("ffmpeg"), Path("reference.mkv"), "0:a:1")
        command = run.call_args.args[0]
        self.assertNotIn("-ss", command)
        self.assertEqual(command[command.index("-map") + 1], "0:a:1")
        np.testing.assert_array_equal(samples, [1, -2, 300])

        malformed = SimpleNamespace(returncode=0, stdout=b"\x01", stderr=b"")
        with patch("scripts.ld_dvd_audio_compare.subprocess.run", return_value=malformed):
            with self.assertRaisesRegex(PipelineError, "partial 16-bit"):
                load_dvd(Path("ffmpeg"), Path("reference.mkv"), "0:a:1")

        failed = SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad stream selector")
        with patch("scripts.ld_dvd_audio_compare.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(PipelineError, "DVD decode failed: bad stream selector"):
                load_dvd(Path("ffmpeg"), Path("reference.mkv"), "0:a:9")

    def test_cli_generates_redacted_report_without_mutating_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ld_wav, dvd = root / "native.wav", root / "dvd.mkv"
            ld_wav.write_bytes(b"fixture")
            dvd.write_bytes(b"fixture")
            (root / "probe").mkdir()
            report = root / "probe" / "compare.json"
            before = {
                "episode": "S01E99",
                "files": {"ld_native": {"name": "native.wav"},
                          "dvd": {"name": "dvd.mkv"}},
                "qc": {"dvd_audio_map": "0:a:2"},
            }

            class FakeEpisode:
                code = "S01E99"
                title = "Synthetic"
                data = json.loads(json.dumps(before))

                def path(self, key):
                    return {"ld_native": ld_wav, "dvd": dvd}[key]

            class FakeLd:
                profile = {"shared": {"enabled": False}}
                profile_path = root / "machine.json"

                def tool(self, name):
                    self.tool_name = name
                    return "ffmpeg"

                def probe(self):
                    return report.parent

            env = np.random.default_rng(77).normal(size=300)
            band_rows = np.tile(np.arange(1, 7, dtype=np.float64), (300, 1))
            argv = ["ld_dvd_audio_compare.py", "--episode", "S01E99", "--out", str(report)]
            with patch.object(comparator, "Ld", FakeLd), \
                    patch.object(comparator, "load_episode", return_value=FakeEpisode()), \
                    patch.object(comparator, "load_ld", return_value=np.zeros(10)), \
                    patch.object(comparator, "load_dvd", return_value=np.zeros(10)) as decode, \
                    patch.object(comparator, "per_second_rms", return_value=(env, len(env))), \
                    patch.object(comparator, "per_second_bands",
                                 return_value=(band_rows, len(band_rows))), \
                    patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(comparator.main(), 0)
            self.assertEqual(decode.call_args.args[2], "0:a:2")
            self.assertEqual(FakeEpisode.data, before)
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema"], "ld-dvd-audio-compare-v3")
            self.assertEqual(saved["dvd_selector_source"], "manifest qc.dvd_audio_map")
            self.assertEqual(saved["band_lag_corrected"], False)

    def test_cli_fails_clearly_when_manifest_audio_input_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            class FakeEpisode:
                code = "S01E99"
                title = "Synthetic"
                data = {"files": {"dvd": {}}}

            class FakeLd:
                profile = {"shared": {"enabled": False}}
                profile_path = root / "machine.json"

            with patch.object(comparator, "Ld", FakeLd), \
                    patch.object(comparator, "load_episode", return_value=FakeEpisode()), \
                    patch.object(sys, "argv", ["ld_dvd_audio_compare.py", "--episode", "S01E99"]):
                with self.assertRaisesRegex(PipelineError, "files.ld_native is unset"):
                    comparator.main()


if __name__ == "__main__":
    unittest.main()
