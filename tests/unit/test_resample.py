"""Exact-ratio rate correction + sample-accurate sync (python-soxr, 24-bit FLAC)."""
from __future__ import annotations

import shutil
import subprocess
import wave

import numpy as np
import pytest

from fstk.media.resample import correct_and_sync

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg")
TRUE_RATE = 44144.37          # the disc's real sample clock (non-integer: asetrate could not express it)


def native(path, seconds=6.0, freq=1000.0):
    n = int(seconds * TRUE_RATE)
    t = np.arange(n) / TRUE_RATE
    x = (0.5 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)                     # EFM WAVs are labelled 44.1 kHz regardless
        w.writeframes(np.column_stack([x, x]).tobytes())
    return n


def decode(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s32le", "-ac", "2", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, "<i4").reshape(-1, 2).astype(np.float64) / 2**31


def peak_hz(x, sr=44100):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    k = int(np.argmax(spec))
    a, b, c = np.log(spec[k - 1:k + 2])
    return (k + 0.5 * (a - c) / (a - 2 * b + c)) * sr / len(x)


def test_exact_ratio_restores_the_true_pitch_and_exact_lengths(tmp_path):
    n = native(tmp_path / "n.wav")
    info = correct_and_sync(tmp_path / "n.wav", rate_hz=TRUE_RATE, start_sample=1000, target_samples=200_000,
                            corrected=tmp_path / "c.flac", synced=tmp_path / "s.flac")
    c, s = decode(tmp_path / "c.flac"), decode(tmp_path / "s.flac")
    assert len(s) == 200_000 and info["synced_samples"] == 200_000
    assert abs(len(c) - n * 44100 / TRUE_RATE) <= 2
    assert abs(peak_hz(c[5000:-5000, 0]) - 1000.0) < 0.01          # 10 mHz = 10 ppm at 1 kHz; exact ratio
    assert np.array_equal(s, c[1000:201_000])                         # synced = corrected slice, bit-exact
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=bits_per_raw_sample,sample_rate",
                            "-of", "csv=p=0", str(tmp_path / "s.flac")], capture_output=True, text=True).stdout
    assert probe.strip().split(",") == ["44100", "24"]


def test_negative_origin_and_overrun_are_padded_with_silence_and_rebuild_is_identical(tmp_path):
    native(tmp_path / "n.wav", seconds=2.0)
    total = 100_000
    a = correct_and_sync(tmp_path / "n.wav", rate_hz=TRUE_RATE, start_sample=-500, target_samples=total,
                         corrected=tmp_path / "c1.flac", synced=tmp_path / "s1.flac")
    s = decode(tmp_path / "s1.flac")
    assert len(s) == total and not s[:500].any() and s[500:600].any() and not s[-1000:].any()
    assert a["lead_silence_samples"] == 500 and a["tail_silence_samples"] > 0
    b = correct_and_sync(tmp_path / "n.wav", rate_hz=TRUE_RATE, start_sample=-500, target_samples=total,
                         corrected=tmp_path / "c2.flac", synced=tmp_path / "s2.flac")
    assert a["pcm_sha256"] == b["pcm_sha256"]                         # deterministic dither
    with pytest.raises(FileExistsError):
        correct_and_sync(tmp_path / "n.wav", rate_hz=TRUE_RATE, start_sample=0, target_samples=10,
                         corrected=tmp_path / "c1.flac", synced=tmp_path / "s3.flac")
