"""A picture of a decoded EFM capture's sound: its loudness over time and where it drops out.

For the EFM capture choice the numbers (C1/C2 rates, concealed and muted samples) do not say *where* a capture is
damaged or whether you would hear it. This reads the decoded WAV once and returns

* ``peaks`` – the loudest sample in every ``STEP_S`` seconds (0–1), drawn as a waveform strip;
* ``gaps`` – places where the sound suddenly stops: a run of exact digital silence in both channels (what the
  decoder does when it gives up and mutes) with audible sound right before and after. Real silence in a
  soundtrack fades out; a muted gap starts and ends abruptly.

Reads the file in blocks (never all of it at once); pure numpy + the standard ``wave`` module.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

STEP_S = 0.5
MIN_GAP_SAMPLES = 8                 # ~0.2 ms at 44.1 kHz: shorter zero runs happen in normal audio
EDGE_S = 0.05                       # sound must be present this close before and after the gap
AUDIBLE = 30                        # |sample| above this (≈ −60 dBFS) counts as sound
SCHEMA = "fstk-audio-overview-v1"


def overview(path: str | Path, *, block_s: float = 10.0) -> dict:
    p = Path(path)
    with wave.open(str(p), "rb") as w:
        rate, channels, width, total = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        if width != 2:
            raise ValueError(f"{p.name}: only 16-bit PCM is supported (sample width {width})")
        step = max(int(rate * STEP_S), 1)
        edge = int(rate * EDGE_S)
        block = max(int(rate * block_s) // step * step, step)
        peaks: list[float] = []
        gaps: list[list[float]] = []
        tail = np.zeros((0, channels), np.int16)          # the end of the previous block, for gaps across blocks
        done = 0
        while True:
            raw = w.readframes(block)
            if not raw:
                if len(tail):                   # the very end: judge what is left against the file's end
                    amp = np.abs(tail.astype(np.int32)).max(axis=1)
                    gaps.extend(_gaps(amp, done - len(tail), rate, edge, until=len(tail),
                                      min_end=len(tail) - edge))
                break
            x = np.frombuffer(raw, "<i2").reshape(-1, channels)
            a = np.abs(x.astype(np.int32)).max(axis=1)
            n = len(a) // step * step
            if n:
                peaks.extend((a[:n].reshape(-1, step).max(axis=1) / 32768.0).round(4).tolist())
            if len(a) > n:
                peaks.append(round(float(a[n:].max()) / 32768.0, 4))
            both = np.concatenate([tail, x])
            amp = np.abs(both.astype(np.int32)).max(axis=1)
            offset = done - len(tail)
            # gaps judged last time ended at least ``edge`` before the old end; only the rest are new here
            gaps.extend(_gaps(amp, offset, rate, edge, until=len(both) - edge,
                              min_end=len(tail) - edge if len(tail) else 0))
            keep = max(2 * rate, 3 * edge)
            tail = both[-keep:] if len(both) > keep else both
            done += len(x)
    merged: list[list[float]] = []
    for g in sorted(gaps):
        if merged and g[0] <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], g[1])
        else:
            merged.append(list(g))
    return {"schema": SCHEMA, "duration_s": total / rate if rate else 0.0, "step_s": STEP_S, "peaks": peaks,
            "gaps": [[round(a, 3), round(b, 3)] for a, b in merged]}


def _gaps(amp: np.ndarray, offset: int, rate: int, edge: int, *, until: int, min_end: int = 0) -> list[list[float]]:
    silent = amp == 0
    if not silent.any():
        return []
    d = np.diff(np.concatenate([[0], silent.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    out = []
    for s, e in zip(starts, ends, strict=True):
        if e - s < MIN_GAP_SAMPLES or s < edge or e + edge > until or e <= min_end:
            continue
        if amp[s - edge:s].max() > AUDIBLE and amp[e:e + edge].max() > AUDIBLE:
            out.append([(offset + s) / rate, (offset + e) / rate])
    return out


def cached_overview(wav: str | Path, cache: str | Path) -> dict:
    """``overview`` of ``wav``, kept in ``cache`` (JSON) and reused while the WAV's size and time are unchanged."""
    wav, cache = Path(wav), Path(cache)
    st = wav.stat()
    key = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    try:
        doc = json.loads(cache.read_text(encoding="utf-8"))
        if doc.get("schema") == SCHEMA and doc.get("source") == key:
            return doc
    except (OSError, ValueError):
        pass
    doc = {**overview(wav), "source": key}
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(cache)
    return doc


def describe(doc: dict) -> tuple[str, str]:
    """Plain words and a tone for one capture's overview."""
    n = len(doc.get("gaps", []))
    if n == 0:
        return "no sudden drop-outs heard", "good"
    worst = max(b - a for a, b in doc["gaps"])
    return (f"{n} sudden drop-out{'s' if n != 1 else ''} (longest {worst * 1000:.0f} ms)",
            "bad" if n > 5 or worst > 0.05 else "warn")
