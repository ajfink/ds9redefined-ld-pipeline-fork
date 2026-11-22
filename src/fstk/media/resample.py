"""Exact-ratio LD audio rate correction and sample-accurate sync in one streaming pass.

Replaces v38's ``aresample=88200 → asetrate=round(2×rate) → aresample=44100`` chain. FFmpeg's
``asetrate`` only takes whole hertz, so v38 quantised the measured rate to 0.5 Hz: up to
±0.25 Hz ≈ 5.7 ppm, about 16 ms over a 46-minute episode. The owner asked for the best automatic
solution (2026-11-21), so this uses libsoxr (python-soxr, "VHQ") at the exact float ratio.

One pass reads the native EFM WAV and resamples it from ``rate_hz`` to 44.1 kHz. It writes:

* ``corrected``: the whole side, rate-corrected (the ``ld_corrected`` artifact);
* ``synced``: the corrected stream from ``start_sample`` for exactly ``target_samples``, with
  silence before the WAV's first sample if the origin is negative and after its end.

Both are 24-bit FLAC. Resampling produces more than 16 bits of precision, so the result is
quantised to 24 bits with deterministic TPDF dither (same input → same bytes → same hashes),
rather than truncated back to 16.
"""
from __future__ import annotations

import hashlib
import math
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

OUT_RATE = 44100
BITS = 24
QUALITY = "VHQ"
CHUNK_FRAMES = 1 << 18
DITHER_SEED = 0x46535443  # "FSTC": fixed so a rebuild is byte-identical


def _encoder(ffmpeg: str, out: Path, log: Path | None) -> subprocess.Popen:
    args = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "s32le", "-ar", str(OUT_RATE), "-ac", "2",
            "-i", "-", "-c:a", "flac", "-compression_level", "8", "-sample_fmt", "s32",
            "-bits_per_raw_sample", str(BITS), "-f", "flac", str(out)]
    err = open(log, "ab") if log else subprocess.DEVNULL  # noqa: SIM115 - closed by the caller via proc
    return subprocess.Popen(args, stdin=subprocess.PIPE, stderr=err)


class _Quantiser:
    def __init__(self) -> None:
        self.rng = np.random.default_rng(DITHER_SEED)
        self.clipped = 0
        self.peak = 0.0

    def __call__(self, x: np.ndarray) -> bytes:
        scale = float(1 << (BITS - 1))
        self.peak = max(self.peak, float(np.abs(x).max()) if x.size else 0.0)
        d = self.rng.random(x.shape, dtype=np.float32) - self.rng.random(x.shape, dtype=np.float32)
        q = np.round(x * scale + d)
        lo, hi = -scale, scale - 1
        self.clipped += int(np.count_nonzero((q < lo) | (q > hi)))
        q = np.clip(q, lo, hi).astype(np.int32)
        return (q << (32 - BITS)).astype("<i4").tobytes()


def correct_and_sync(native: Path, *, rate_hz: float, start_sample: int, target_samples: int,
                     corrected: Path, synced: Path, ffmpeg: str = "ffmpeg", log_dir: Path | None = None,
                     cancel=None, progress=None) -> dict[str, Any]:
    """Resample ``native`` (16-bit stereo WAV) from ``rate_hz`` to 44.1 kHz; write corrected + synced.

    ``start_sample`` is in the corrected (44.1 kHz) stream and may be negative (lead silence).
    Outputs are written to ``.tmp.flac`` first and renamed only after both encoders succeed.
    Nothing is overwritten.
    """
    import soxr

    if target_samples <= 0:
        raise ValueError("target_samples must be positive")
    if not 0.9 * OUT_RATE <= rate_hz <= 1.1 * OUT_RATE:
        raise ValueError(f"rate {rate_hz} Hz is not a plausible LD audio rate")
    for out in (corrected, synced):
        if out.exists() or out.with_name(out.stem + ".tmp.flac").exists():
            raise FileExistsError(f"refusing to overwrite {out} (or its .tmp)")
        out.parent.mkdir(parents=True, exist_ok=True)
    tmp = {k: p.with_name(p.stem + ".tmp.flac") for k, p in (("corrected", corrected), ("synced", synced))}
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    logs = {k: (Path(log_dir) / f"{p.stem}.ffmpeg.log" if log_dir else None) for k, p in tmp.items()}
    with wave.open(str(native), "rb") as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2 or w.getcomptype() != "NONE":
            raise ValueError(f"native audio must be 16-bit stereo PCM WAV: {native}")
        total_in = w.getnframes()
        stream = soxr.ResampleStream(rate_hz, OUT_RATE, 2, dtype="float32", quality=QUALITY)
        enc = {k: _encoder(ffmpeg, p, logs[k]) for k, p in tmp.items()}
        quant = _Quantiser()
        hashes = {k: hashlib.sha256() for k in tmp}
        written = {"corrected": 0, "synced": 0}
        pos = 0                           # corrected-stream sample index of the next output sample
        try:
            if start_sample < 0:          # origin before the WAV: lead silence in the synced track
                lead = min(-start_sample, target_samples)
                zeros = np.zeros((lead, 2), np.int32).astype("<i4").tobytes()
                enc["synced"].stdin.write(zeros)
                hashes["synced"].update(zeros)
                written["synced"] += lead
            read = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("cancelled")
                raw = w.readframes(CHUNK_FRAMES)
                last = len(raw) == 0
                x = (np.frombuffer(raw, "<i2").reshape(-1, 2).astype(np.float32) / 32768.0) if raw else \
                    np.zeros((0, 2), np.float32)
                read += len(x)
                y = stream.resample_chunk(x, last=last)
                if len(y):
                    data = quant(y)
                    enc["corrected"].stdin.write(data)
                    hashes["corrected"].update(data)
                    written["corrected"] += len(y)
                    # the part of this chunk that falls inside [start_sample, start_sample + target)
                    a = max(pos, start_sample) - pos
                    b = min(pos + len(y), start_sample + target_samples) - pos
                    if b > a:
                        part = data[a * 8:b * 8]
                        enc["synced"].stdin.write(part)
                        hashes["synced"].update(part)
                        written["synced"] += b - a
                    pos += len(y)
                if progress and total_in:
                    progress(min(1.0, read / total_in))
                if last:
                    break
            if written["synced"] < target_samples:              # tail silence
                pad = target_samples - written["synced"]
                zeros = np.zeros((pad, 2), np.int32).astype("<i4").tobytes()
                enc["synced"].stdin.write(zeros)
                hashes["synced"].update(zeros)
                written["synced"] += pad
        except BaseException:
            for p in enc.values():
                p.kill()
            raise
        finally:
            for p in enc.values():
                if p.stdin and not p.stdin.closed:
                    p.stdin.close()
            codes = {k: p.wait() for k, p in enc.items()}
    if any(codes.values()):
        raise RuntimeError(f"FLAC encoder failed {codes}; partial output kept at {list(tmp.values())}")
    expected_corrected = math.floor(total_in * OUT_RATE / rate_hz)
    tmp["corrected"].rename(corrected)
    tmp["synced"].rename(synced)
    return {"resampler": f"libsoxr {QUALITY} (python-soxr {soxr.__version__}), exact ratio",
            "input_rate_hz": rate_hz, "output_rate_hz": OUT_RATE, "ratio": OUT_RATE / rate_hz,
            "bits": BITS, "dither": f"TPDF, seed {DITHER_SEED:#x}", "native_samples": total_in,
            "corrected_samples": written["corrected"], "expected_corrected_samples": expected_corrected,
            "synced_samples": written["synced"], "start_sample": start_sample, "target_samples": target_samples,
            "lead_silence_samples": max(0, -start_sample),
            "tail_silence_samples": max(0, start_sample + target_samples - written["corrected"]),
            "clipped_samples": quant.clipped, "peak": quant.peak,
            "pcm_sha256": {k: h.hexdigest() for k, h in hashes.items()}}
