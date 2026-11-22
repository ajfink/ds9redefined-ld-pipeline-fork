"""Raw planar frame buffers → code-value planes (8-bit scale, float32)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FORMATS = {   # pix_fmt: (bytes per sample, chroma width divisor, chroma height divisor)
    "yuv420p": (1, 2, 2), "yuv422p": (1, 2, 1), "yuv420p10le": (2, 2, 2), "yuv422p10le": (2, 2, 1),
}


@dataclass
class Planes:
    """One frame; every plane scaled to 8-bit code values (10-bit / 4), so thresholds read like v38's."""
    y: np.ndarray
    u: np.ndarray
    v: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return self.y.shape


def frame_bytes(width: int, height: int, pix_fmt: str) -> int:
    size, cw, ch = FORMATS[pix_fmt]
    return size * (width * height + 2 * (width // cw) * (height // ch))


def split_frames(buf: bytes, width: int, height: int, pix_fmt: str) -> list[Planes]:
    if pix_fmt not in FORMATS:
        raise ValueError(f"unsupported pix_fmt {pix_fmt!r}")
    size, cw, ch = FORMATS[pix_fmt]
    per = frame_bytes(width, height, pix_fmt)
    if per == 0 or len(buf) % per:
        raise ValueError(f"buffer of {len(buf)} bytes is not a whole number of {width}x{height} {pix_fmt} frames")
    dtype = np.uint8 if size == 1 else np.dtype("<u2")
    scale = 1.0 if size == 1 else 0.25
    out = []
    for i in range(len(buf) // per):
        a = np.frombuffer(buf, dtype, count=per // size, offset=i * per).astype(np.float32) * scale
        ny, nc = width * height, (width // cw) * (height // ch)
        out.append(Planes(a[:ny].reshape(height, width), a[ny:ny + nc].reshape(height // ch, width // cw),
                          a[ny + nc:].reshape(height // ch, width // cw)))
    return out


def upsample_chroma(p: Planes) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-neighbour chroma to luma size (measurement only; never used to build output)."""
    h, w = p.y.shape
    ry, rx = h // p.u.shape[0], w // p.u.shape[1]
    return (np.repeat(np.repeat(p.u, ry, 0), rx, 1), np.repeat(np.repeat(p.v, ry, 0), rx, 1))
