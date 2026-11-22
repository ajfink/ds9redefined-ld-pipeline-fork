"""Render the retimed DVD 5.1 onto the LD output timeline (streaming, exact-ratio, 24-bit FLAC).

Takes the pieces of a ``retime_map.RetimeMap``:

* **dvd** pieces: the DVD 5.1 between two DVD times, resampled with libsoxr VHQ at that
  piece's exact ratio (≈ 1000:1001). v38's ``atempo`` time-stretch is replaced by resampling,
  which leaves no WSOLA artifacts and shifts pitch by only 1.7 cents. Each piece is pinned to its
  exact output sample span;
* **ld** pieces: LD-only material (edits where the LD has picture the DVD lacks), filled from
  the time-aligned LD stereo track into front left/right, so an edit mid-scene is not a dropout.

The DVD is decoded once, sequentially (never seeked). Joins get 2 ms raised-cosine fades. The
output is exactly ``round(total_s × 48000)`` samples, written ``.tmp`` first, never overwriting.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np

RATE = 48000
FADE_S = 0.002
CHUNK = 1 << 16
BITS = 24
SEED = 0x35315254  # "51RT"


class _PCMReader:
    """Sequential float32 reader over an FFmpeg decode of one audio stream."""

    def __init__(self, args: list[str], channels: int):
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.channels = channels
        self.pos = 0                        # samples consumed
        self.buf = np.zeros((0, channels), np.float32)

    def read_until(self, end: int) -> np.ndarray:
        """Samples [pos, end) (zero-padded past EOF); advances pos to end."""
        want = max(0, end - self.pos)
        parts = [self.buf] if len(self.buf) else []
        have = len(self.buf)
        while have < want:
            raw = self.proc.stdout.read(max(CHUNK, want - have) * self.channels * 4)
            if not raw:
                break
            a = np.frombuffer(raw, "<f4").reshape(-1, self.channels)
            parts.append(a)
            have += len(a)
        data = np.concatenate(parts) if parts else np.zeros((0, self.channels), np.float32)
        out, self.buf = data[:want], data[want:]
        if len(out) < want:
            out = np.concatenate([out, np.zeros((want - len(out), self.channels), np.float32)])
        self.pos = end
        return out

    def skip_to(self, sample: int) -> None:
        if sample > self.pos:
            self.read_until(sample)

    def close(self) -> int:
        self.proc.stdout.close()
        return self.proc.wait()


def _fade(x: np.ndarray, n: int) -> np.ndarray:
    n = min(n, len(x) // 2)
    if n > 0:
        r = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n, dtype=np.float32))
        x[:n] *= r[:, None]
        x[-n:] *= r[::-1, None]
    return x


def render_51(pieces, *, total_s: float, dvd: Path, dvd_selector: str, layout: str, ld_synced: Path, out: Path,
              ffmpeg: str = "ffmpeg", cancel=None, progress=None) -> dict:
    """Write the retimed 5.1 FLAC; returns sample counts, per-piece spans and the PCM hash."""
    import soxr

    if out.exists() or out.with_name(out.stem + ".tmp.flac").exists():
        raise FileExistsError(f"refusing to overwrite {out}")
    tmp = out.with_name(out.stem + ".tmp.flac")
    total = round(total_s * RATE)
    dvd_in = _PCMReader([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(dvd), "-map", dvd_selector,
                         "-ac", "6", "-ar", str(RATE), "-f", "f32le", "-"], 6)
    ld_in = _PCMReader([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(ld_synced), "-map", "0:a:0",
                        "-af", f"aresample={RATE}:resampler=soxr:precision=28", "-ac", "2", "-f", "f32le", "-"], 2)
    enc = subprocess.Popen([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "s32le", "-ar", str(RATE),
                            "-ac", "6", "-channel_layout", layout, "-i", "-", "-c:a", "flac", "-compression_level", "8",
                            "-sample_fmt", "s32", "-bits_per_raw_sample", str(BITS), "-f", "flac", str(tmp)],
                           stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    rng = np.random.default_rng(SEED)
    h = hashlib.sha256()
    written = 0
    spans = []
    clipped = 0

    def emit(block: np.ndarray) -> None:
        nonlocal written, clipped
        scale = float(1 << (BITS - 1))
        d = rng.random(block.shape, dtype=np.float32) - rng.random(block.shape, dtype=np.float32)
        q = np.round(block * scale + d)
        clipped += int(np.count_nonzero((q < -scale) | (q > scale - 1)))
        data = (np.clip(q, -scale, scale - 1).astype(np.int32) << (32 - BITS)).astype("<i4").tobytes()
        enc.stdin.write(data)
        h.update(data)
        written += len(block)

    try:
        for i, p in enumerate(sorted(pieces, key=lambda q: q.out_start_s)):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled")
            o0, o1 = round(max(p.out_start_s, 0.0) * RATE), round(min(p.out_end_s, total_s) * RATE)
            if o0 > written:                       # any hole: silence (should not happen)
                emit(np.zeros((o0 - written, 6), np.float32))
            o0 = max(o0, written)
            n = o1 - o0
            if n <= 0:
                continue
            if p.source == "dvd":
                d0, d1 = round(p.dvd_start_s * RATE), round(p.dvd_end_s * RATE)
                dvd_in.skip_to(d0)
                rs = soxr.ResampleStream(RATE * p.ratio, RATE, 6, dtype="float32", quality="VHQ")
                parts, got, cur = [], 0, d0
                while cur < d1:
                    nxt = min(cur + CHUNK, d1)
                    y = rs.resample_chunk(dvd_in.read_until(nxt), last=nxt >= d1)
                    parts.append(y)
                    got += len(y)
                    cur = nxt
                block = np.concatenate(parts) if parts else np.zeros((0, 6), np.float32)
            else:
                ld_in.skip_to(o0)
                st = ld_in.read_until(o1)
                block = np.zeros((len(st), 6), np.float32)
                block[:, 0], block[:, 1] = st[:, 0], st[:, 1]
            if len(block) < n:                     # pin to the exact span
                block = np.concatenate([block, np.zeros((n - len(block), 6), np.float32)])
            block = _fade(block[:n].copy(), round(FADE_S * RATE))
            spans.append({"piece": i, "source": p.source, "out_samples": [o0, o1], "rendered": int(n)})
            for k in range(0, n, CHUNK):
                emit(block[k:k + CHUNK])
            if progress:
                progress(min(1.0, o1 / total))
        if written < total:
            emit(np.zeros((total - written, 6), np.float32))
    except BaseException:
        enc.kill()
        dvd_in.proc.kill()
        ld_in.proc.kill()
        raise
    finally:
        if enc.stdin and not enc.stdin.closed:
            enc.stdin.close()
    code = enc.wait()
    dvd_in.proc.kill()
    ld_in.proc.kill()
    dvd_in.close()
    ld_in.close()
    if code != 0:
        raise RuntimeError(f"FLAC encoder failed ({code}); partial output kept at {tmp}")
    tmp.rename(out)
    return {"samples": written, "target_samples": total, "rate": RATE, "bits": BITS, "pieces": spans,
            "clipped_samples": clipped, "pcm_sha256": h.hexdigest(),
            "resampler": "libsoxr VHQ (python-soxr), exact ratio per piece; LD fill in FL/FR"}
