from __future__ import annotations

import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qc_report  # noqa: E402


class SeekabilityQcTests(unittest.TestCase):
    def setUp(self):
        self.ld = SimpleNamespace(ffmpeg=lambda: "ffmpeg")
        self.media = Path("master.mkv")

    def test_progress_parser_handles_microseconds_and_timestamp_fallback(self):
        self.assertEqual(qc_report._progress_seconds("out_time_us=5000000\nprogress=end\n"), 5.0)
        self.assertEqual(qc_report._progress_seconds("out_time=00:00:05.125000\n"), 5.125)
        self.assertIsNone(qc_report._progress_seconds("progress=end\n"))

    def test_tests_every_audio_track_at_four_points_with_both_seek_modes(self):
        commands = []

        def fake_sh(command):
            commands.append(command)
            return CompletedProcess(command, 0, "out_time_us=5000000\nprogress=end\n", "")

        with patch.object(qc_report, "sh", side_effect=fake_sh):
            status, detail = qc_report.check_seekability(self.ld, self.media, 2400, 2)

        self.assertEqual(status, "PASS")
        self.assertIn("2 audio track(s), 4 timeline points each", detail)
        self.assertEqual(len(commands), 16)
        self.assertIn("-ss", commands[0][:commands[0].index("-i")])
        self.assertGreater(commands[1].index("-ss"), commands[1].index("-i"))
        self.assertEqual({cmd[cmd.index("-map") + 1] for cmd in commands}, {"0:a:0", "0:a:1"})

    def test_truncated_input_seek_fails_with_known_matroska_remux_fix(self):
        call = 0

        def fake_sh(command):
            nonlocal call
            call += 1
            decoded = 0.093 if call == 1 else 5.0
            return CompletedProcess(command, 0,
                                    f"out_time_us={int(decoded * 1_000_000)}\nprogress=end\n", "")

        with patch.object(qc_report, "sh", side_effect=fake_sh):
            status, detail = qc_report.check_seekability(self.ld, self.media, 100, 1)

        self.assertEqual(status, "FAIL")
        self.assertIn("0.093s", detail)
        self.assertIn("-max_interleave_delta 0", detail)

    def test_no_audio_or_too_short_master_cannot_pass(self):
        self.assertEqual(qc_report.check_seekability(self.ld, self.media, 100, 0)[0], "FAIL")
        self.assertEqual(qc_report.check_seekability(self.ld, self.media, 8, 1)[0], "ERROR")

    def test_both_platform_remuxers_keep_the_verified_matroska_interleave_fix(self):
        for relative in ("scripts/remux_padfixed.ps1", "scripts/remux_padfixed_mac.py"):
            with self.subTest(remuxer=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("-max_interleave_delta", source)
                self.assertIn("'0'" if relative.endswith(".ps1") else '"0"', source)


if __name__ == "__main__":
    unittest.main()
