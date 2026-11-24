"""Building replacement frames for operator frame fixes: a DVD frame borrowed for a delivered frame, registered
to the LaserDisc raster and colour-matched to its LaserDisc neighbour, plus the small image and script helpers.

(Formerly ``media/pad_repair.py``. The PAD-specific parts went with ADR 0004: the stack master no longer has
Orc's CLV time-code PAD frames, so nothing is repaired automatically any more.)

The DVD frame is found by matching the LD flank frames to the aligned DVD window, registered to the LD raster
(affine, ECC) and colour-matched; when the flanks do not match uniquely the borrow declines rather than guess.
Every function here is pure numpy/OpenCV so it is unit-testable without VapourSynth.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError

#: Flank matching: both flanks must match a DVD frame this well, uniquely, and be two frames apart.
MIN_FLANK_NCC = 0.90
MIN_FLANK_MARGIN = 0.03
MIN_POST_NCC = 0.90          # registered + colour-matched DVD flank vs the LD flank it stands in for
GAIN_BOUNDS = (0.70, 1.40)


def f32(img: np.ndarray) -> np.ndarray:
    """Any 8- or 16-bit frame as float32 on the 0–255 scale (all replacement maths is done in float)."""
    if img.dtype == np.uint16:
        return img.astype(np.float32) * np.float32(255.0 / 65535.0)
    return img.astype(np.float32)


def to_u16(img: np.ndarray) -> np.ndarray:
    """float 0–255 → uint16 full scale (×257, so 8-bit values keep their exact 16-bit equivalents)."""
    return np.clip(np.rint(img * np.float32(257.0)), 0, 65535).astype(np.uint16)


def desc48(img: np.ndarray) -> np.ndarray:
    """48×36 zero-mean unit-norm descriptor of an RGB/grey frame (area resize)."""
    import cv2

    img = f32(img)
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(g, (48, 36), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    small -= small.mean()
    n = float(np.linalg.norm(small))
    return small / n if n else small


@dataclass
class FlankMatch:
    p: int
    q: int
    ncc_p: float
    ncc_q: float
    candidate: int


def match_flanks(prev_desc: np.ndarray, next_desc: np.ndarray, dvd_descs: np.ndarray,
                 gap: int = 1) -> FlankMatch | None:
    """Locate the LD flank frames in a DVD window; require the DVD frames to be ``gap + 1`` apart.

    ``dvd_descs`` is (n, 1728) unit-norm. Returns None unless both matches are strong, unique and
    ``q − p == gap + 1`` (``gap`` DVD frames lie between them — the missing frames). ``candidate``
    is the first of them.
    """
    if len(dvd_descs) < 3:
        return None
    sp, sq = dvd_descs @ prev_desc, dvd_descs @ next_desc
    p, q = int(np.argmax(sp)), int(np.argmax(sq))

    def unique(s: np.ndarray, k: int) -> bool:
        rest = np.delete(s, k)
        return len(rest) == 0 or s[k] - float(rest.max()) >= MIN_FLANK_MARGIN

    if sp[p] < MIN_FLANK_NCC or sq[q] < MIN_FLANK_NCC or q - p != gap + 1:
        return None
    if not (unique(sp, p) and unique(sq, q)):
        return None
    return FlankMatch(p, q, float(sp[p]), float(sq[q]), p + 1)


def register_affine(src_rgb: np.ndarray, dst_rgb: np.ndarray) -> np.ndarray:
    """2×3 affine mapping ``src`` (DVD) onto ``dst`` (LD raster), by ECC from a scale start."""
    import cv2

    h, w = dst_rgb.shape[:2]
    sh, sw = src_rgb.shape[:2]
    g_src = cv2.cvtColor(f32(src_rgb), cv2.COLOR_RGB2GRAY) / 255.0
    g_dst = cv2.cvtColor(f32(dst_rgb), cv2.COLOR_RGB2GRAY) / 255.0
    warp = np.array([[sw / w, 0.0, 0.0], [0.0, sh / h, 0.0]], np.float32)   # dst → src coordinates
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-7)
    try:
        # inputs: template = dst, input = src; warp maps template coords → input coords
        _, warp = cv2.findTransformECC(g_dst, g_src, warp, cv2.MOTION_AFFINE, crit, None, 5)
    except cv2.error as exc:
        raise ArtifactError(f"DVD frame could not be registered to the LD raster: {exc}") from None
    return warp


def warp_to(src_rgb: np.ndarray, warp: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    w, h = size
    return cv2.warpAffine(f32(src_rgb), warp, (w, h), flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


def fit_colour(src_rgb: np.ndarray, ref_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel gain/offset taking ``src`` to ``ref`` using stable, low-gradient, unclipped pixels."""
    import cv2

    s, r = f32(src_rgb), f32(ref_rgb)
    gs = cv2.cvtColor(s, cv2.COLOR_RGB2GRAY)
    gx = np.abs(cv2.Sobel(gs, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(gs, cv2.CV_32F, 0, 1))
    mask = (gx < np.percentile(gx, 60)) & (gs > 8) & (gs < 247)
    mask[:8, :] = mask[-8:, :] = False
    mask[:, :8] = mask[:, -8:] = False
    if mask.sum() < 500:
        raise ArtifactError("too few stable pixels to fit a colour correction")
    gains, offsets = np.ones(3, np.float32), np.zeros(3, np.float32)
    for c in range(3):
        x, y = s[..., c][mask], r[..., c][mask]
        for _ in range(4):                      # iteratively reweighted: drops moving/changed regions
            A = np.stack([x, np.ones_like(x)], 1)
            g, o = np.linalg.lstsq(A, y, rcond=None)[0]
            res = np.abs(g * x + o - y)
            keep = res <= max(3.0, 2.5 * float(np.median(res)))
            if keep.sum() < 300:
                break
            x, y = x[keep], y[keep]
        gains[c], offsets[c] = float(np.clip(g, *GAIN_BOUNDS)), float(o)
    return gains, offsets


def apply_colour(rgb: np.ndarray, gains: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Float result on the 0–255 scale (no rounding: it is quantised once, to 16 bits, at the very end)."""
    return np.clip(f32(rgb) * gains + offsets, 0, 255)


@dataclass
class Borrowed:
    images: list[np.ndarray]  # uint16 RGB (full-scale 16-bit) at the LD raster, one per missing frame
    gains: list[float]
    offsets: list[float]
    reference: str           # "prev" or "next": the LD flank the colour was matched to
    post_ncc: float
    match: FlankMatch

    @property
    def image(self) -> np.ndarray:
        return self.images[0]


def borrow_dvd_frame(ld_prev: np.ndarray, ld_next: np.ndarray, dvd_window: np.ndarray,
                     gap: int = 1) -> Borrowed | None:
    """Build ``gap`` replacement frames from a DVD window ((n,h,w,3) progressive RGB).

    Returns None when the flanks do not match uniquely two frames
    apart, or the registered/colour-matched DVD flank does not reproduce the LD flank.
    """
    m = match_flanks(desc48(ld_prev), desc48(ld_next), np.stack([desc48(f) for f in dvd_window]), gap)
    if m is None:
        return None
    cands = [dvd_window[m.candidate + k] for k in range(gap)]
    # which shot do the borrowed frames belong to? colour-match to that side's LD frame (the first
    # missing frame decides: the shot before a cut is the one that lost its last frame(s))
    sim_prev = float(desc48(cands[0]) @ desc48(ld_prev))
    sim_next = float(desc48(cands[0]) @ desc48(ld_next))
    ref_name, ref_ld, ref_dvd = (("prev", ld_prev, dvd_window[m.p]) if sim_prev >= sim_next
                                 else ("next", ld_next, dvd_window[m.q]))
    h, w = ref_ld.shape[:2]
    try:
        warp = register_affine(ref_dvd, ref_ld)
        warped_ref = warp_to(ref_dvd, warp, (w, h))
        gains, offsets = fit_colour(warped_ref, ref_ld)
    except ArtifactError:
        return None
    post = float(desc48(apply_colour(warped_ref, gains, offsets)) @ desc48(ref_ld))
    if post < MIN_POST_NCC:
        return None
    images = [to_u16(apply_colour(warp_to(c, warp, (w, h)), gains, offsets)) for c in cands]
    return Borrowed(images, [round(float(g), 4) for g in gains], [round(float(o), 3) for o in offsets],
                    ref_name, post, m)


def base_sha(base_text: str) -> str:
    import hashlib

    return hashlib.sha256(base_text.encode("utf-8")).hexdigest()


def write_png(path: Path, rgb: np.ndarray) -> None:
    """RGB uint8 or uint16 → PNG of the same depth (16-bit PNGs keep the 10-bit chain's precision)."""
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
        raise ArtifactError(f"could not write {path}")
