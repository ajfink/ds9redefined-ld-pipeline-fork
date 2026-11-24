"""Waveform strips and drop-out detection for the EFM capture choice."""
from __future__ import annotations

import wave

import numpy as np

from fstk.media import audio_overview as ao

RATE = 8000


def write(path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.repeat(x[:, None], 2, axis=1).astype("<i2").tobytes())


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def test_drop_outs_are_found_once_even_across_blocks_and_fades_are_not(tmp_path):
    x = tone(30.0)
    for at in (3.0, 9.995, 20.0):                       # 9.995 s straddles the 10 s reading block
        i = int(at * RATE)
        x[i:i + 80] = 0                                 # 10 ms muted
    x[-int(2 * RATE):] = 0                              # the programme ends in silence: not a drop-out
    x[int(14 * RATE):int(14.5 * RATE)] = (x[int(14 * RATE):int(14.5 * RATE)] * np.linspace(1, 0, int(0.5 * RATE))
                                          ).astype(np.int16)
    x[int(14.5 * RATE):int(15 * RATE)] = 0              # fade out, a pause, then hard restart
    write(tmp_path / "a.wav", x)
    doc = ao.overview(tmp_path / "a.wav")
    starts = [round(a, 2) for a, _b in doc["gaps"]]
    assert 3.0 in starts and 20.0 in starts and any(abs(s - 9.995) < 0.01 for s in starts)
    assert len(doc["gaps"]) == 3 + (1 if any(abs(s - 14.5) < 0.6 for s in starts) else 0)
    assert abs(doc["duration_s"] - 30.0) < 1e-6 and len(doc["peaks"]) == 60
    assert max(doc["peaks"]) > 0.2 and doc["peaks"][-1] == 0.0
    text, tone_ = ao.describe(doc)
    assert "drop-out" in text and tone_ in ("warn", "bad")


def test_overview_is_cached_until_the_wav_changes(tmp_path):
    write(tmp_path / "b.wav", tone(2.0))
    cache = tmp_path / "c" / "b.json"
    first = ao.cached_overview(tmp_path / "b.wav", cache)
    assert cache.is_file() and ao.cached_overview(tmp_path / "b.wav", cache) == first
    assert ao.describe(first) == ("no sudden drop-outs heard", "good")
    write(tmp_path / "b.wav", tone(3.0))
    assert abs(ao.cached_overview(tmp_path / "b.wav", cache)["duration_s"] - 3.0) < 1e-6
