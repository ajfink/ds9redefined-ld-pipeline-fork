"""Active picture area, crop lock and SAR/DAR (pure).

Owner rules (2026-11-22): the DVD is a *geometry reference*. The LaserDisc picture keeps its own pixels;
the only DVD-derived decisions are (a) which borders to trim and (b) which display aspect ratio to
signal. Both are measured per episode and recorded; nothing is guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

#: Aspect ratios a broadcast/DVD picture is expected to have; a measurement within 0.6 % snaps to one.
STANDARD_DAR = {"4:3": Fraction(4, 3), "16:9": Fraction(16, 9), "1.85:1": Fraction(185, 100),
                "2.00:1": Fraction(2, 1), "2.35:1": Fraction(235, 100)}
SNAP_TOLERANCE = 0.006
EXPECTED_TAGS = {"color_primaries": "smpte170m", "color_transfer": "smpte170m", "color_space": "smpte170m",
                 "color_range": "tv"}


@dataclass(frozen=True)
class Crop:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def is_zero(self) -> bool:
        return not (self.left or self.top or self.right or self.bottom)

    def size(self, width: int, height: int) -> tuple[int, int]:
        return width - self.left - self.right, height - self.top - self.bottom

    def as_dict(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}


def active_crop(ys: list[np.ndarray], *, margin: float = 6.0, even: int = 2, max_trim_frac: float = 0.12) -> Crop:
    """Black borders of a set of luma frames (8-bit codes), the same on every frame of the episode.

    A column/row is picture if its brightest content across *all* frames exceeds the border level by
    ``margin`` codes; pooling over many frames keeps one dark scene from looking like a border. Trims are
    rounded *down* to ``even`` (4:2:2/4:2:0 chroma siting) and capped at ``max_trim_frac`` of the raster.
    """
    h, w = ys[0].shape
    col_max = np.max([np.percentile(y, 99.5, axis=0) for y in ys], axis=0)
    row_max = np.max([np.percentile(y, 99.5, axis=1) for y in ys], axis=0)
    floor = float(min(np.percentile(col_max, 2), np.percentile(row_max, 2)))
    cols = np.flatnonzero(col_max > floor + margin)
    rows = np.flatnonzero(row_max > floor + margin)
    if len(cols) == 0 or len(rows) == 0:
        return Crop()
    left, right = int(cols[0]), int(w - 1 - cols[-1])
    top, bottom = int(rows[0]), int(h - 1 - rows[-1])

    def snap(v: int, limit: int) -> int:
        return min(v - v % even, int(limit * max_trim_frac) // even * even)
    return Crop(snap(left, w), snap(top, h), snap(right, w), snap(bottom, h))


def snap_dar(dar: Fraction) -> tuple[Fraction, str]:
    for name, std in STANDARD_DAR.items():
        if abs(float(dar / std) - 1.0) <= SNAP_TOLERANCE:
            return std, name
    return dar, "measured"


def sar_for(dar: Fraction, width: int, height: int) -> Fraction:
    """Sample aspect ratio that makes ``width``x``height`` display at ``dar`` (reduced, bounded)."""
    return (dar * height / width).limit_denominator(2000)


def lock_geometry(*, master_w: int, master_h: int, master_sar: Fraction, ld_crop: Crop,
                  dvd_dar: Fraction | None) -> dict:
    """Crop plus the display aspect to signal.

    With a DVD: its measured active-picture DAR (snapped to a standard when within tolerance) is the
    target and the LD SAR is derived so the cropped LD picture displays at exactly that DAR. Without a
    DVD: the master's own SAR is kept on the cropped raster (``source: master``).
    """
    w, h = ld_crop.size(master_w, master_h)
    if dvd_dar is not None:
        dar, label = snap_dar(dvd_dar)
        sar, source = sar_for(dar, w, h), f"dvd ({label})"
    else:
        sar = master_sar
        dar, label = snap_dar(Fraction(w, h) * sar)
        source = "master"
    return {"crop": ld_crop.as_dict(), "width": w, "height": h, "sar": f"{sar.numerator}:{sar.denominator}",
            "dar": f"{dar.numerator}:{dar.denominator}", "dar_label": label, "source": source}


def check_colour_tags(stream: dict, *, expect_dar: str | None = None, expect_sar: str | None = None) -> list[dict]:
    """ffprobe stream dict vs the SMPTE 170M / limited-range recipe and the locked aspect."""
    rows = []
    for key, want in EXPECTED_TAGS.items():
        got = stream.get(key)
        rows.append({"check": key, "value": got, "expected": want, "status": "PASS" if got == want else "FAIL"})
    for key, want in (("sample_aspect_ratio", expect_sar), ("display_aspect_ratio", expect_dar)):
        if want:
            got = stream.get(key)
            rows.append({"check": key, "value": got, "expected": want, "status": "PASS" if got == want else "FAIL"})
    return rows
