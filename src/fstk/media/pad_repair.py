"""PAD repair: choosing and building the replacement frame (owner rules 2026-11-21).

* PAD not on a shot cut → RIFE midpoint of the neighbours (v38 method).
* PAD on a shot cut, DVD available → the missing frame is **borrowed from the DVD** (never
  interpolated across a cut): found by matching the LD flank frames to the aligned DVD window,
  registered to the LD raster (affine, ECC) and colour-matched to the neighbouring LD frame.
* PAD on a shot cut, no DVD (or the borrow is not unambiguous) → hold the previous frame for one
  more frame (v38 ``hold-prev-scene-cut``).

Every function here is pure numpy/OpenCV so it is unit-testable without VapourSynth.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError

RIFE = "rife"
DVD_CUT = "dvd-cut-frame"
HOLD = "hold-prev-scene-cut"

#: Flank matching: both flanks must match a DVD frame this well, uniquely, and be two frames apart.
MIN_FLANK_NCC = 0.90
MIN_FLANK_MARGIN = 0.03
MIN_POST_NCC = 0.90          # registered + colour-matched DVD flank vs the LD flank it stands in for
GAIN_BOUNDS = (0.70, 1.40)


def choose_method(*, cut: bool, dvd: bool) -> str:
    if not cut:
        return RIFE
    return DVD_CUT if dvd else HOLD


def desc48(img: np.ndarray) -> np.ndarray:
    """48×36 zero-mean unit-norm descriptor of an RGB/grey frame (area resize)."""
    import cv2

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


def match_flanks(prev_desc: np.ndarray, next_desc: np.ndarray, dvd_descs: np.ndarray) -> FlankMatch | None:
    """Locate the LD flank frames in a DVD window; require the DVD frames to be exactly two apart.

    ``dvd_descs`` is (n, 1728) unit-norm. Returns None unless both matches are strong, unique and
    ``q − p == 2`` (one DVD frame lies between them — the missing frame).
    """
    if len(dvd_descs) < 3:
        return None
    sp, sq = dvd_descs @ prev_desc, dvd_descs @ next_desc
    p, q = int(np.argmax(sp)), int(np.argmax(sq))

    def unique(s: np.ndarray, k: int) -> bool:
        rest = np.delete(s, k)
        return len(rest) == 0 or s[k] - float(rest.max()) >= MIN_FLANK_MARGIN

    if sp[p] < MIN_FLANK_NCC or sq[q] < MIN_FLANK_NCC or q - p != 2:
        return None
    if not (unique(sp, p) and unique(sq, q)):
        return None
    return FlankMatch(p, q, float(sp[p]), float(sq[q]), p + 1)


def register_affine(src_rgb: np.ndarray, dst_rgb: np.ndarray) -> np.ndarray:
    """2×3 affine mapping ``src`` (DVD) onto ``dst`` (LD raster), by ECC from a scale start."""
    import cv2

    h, w = dst_rgb.shape[:2]
    sh, sw = src_rgb.shape[:2]
    g_src = cv2.cvtColor(src_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    g_dst = cv2.cvtColor(dst_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
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
    return cv2.warpAffine(src_rgb, warp, (w, h), flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


def fit_colour(src_rgb: np.ndarray, ref_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel gain/offset taking ``src`` to ``ref`` using stable, low-gradient, unclipped pixels."""
    import cv2

    s, r = src_rgb.astype(np.float32), ref_rgb.astype(np.float32)
    gs = cv2.cvtColor(src_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
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
    return np.clip(rgb.astype(np.float32) * gains + offsets, 0, 255).round().astype(np.uint8)


@dataclass
class Borrowed:
    image: np.ndarray        # uint8 RGB at the LD raster
    gains: list[float]
    offsets: list[float]
    reference: str           # "prev" or "next": the LD flank the colour was matched to
    post_ncc: float
    match: FlankMatch


def borrow_dvd_frame(ld_prev: np.ndarray, ld_next: np.ndarray, dvd_window: np.ndarray) -> Borrowed | None:
    """Build the replacement for a PAD on a cut from a DVD window ((n,h,w,3) progressive RGB).

    Returns None (→ hold the previous frame) when the flanks do not match uniquely two frames
    apart, or the registered/colour-matched DVD flank does not reproduce the LD flank.
    """
    m = match_flanks(desc48(ld_prev), desc48(ld_next), np.stack([desc48(f) for f in dvd_window]))
    if m is None:
        return None
    cand = dvd_window[m.candidate]
    # which shot does the borrowed frame belong to? colour-match to that side's LD frame
    sim_prev = float(desc48(cand) @ desc48(ld_prev))
    sim_next = float(desc48(cand) @ desc48(ld_next))
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
    image = apply_colour(warp_to(cand, warp, (w, h)), gains, offsets)
    return Borrowed(image, [round(float(g), 4) for g in gains], [round(float(o), 3) for o in offsets],
                    ref_name, post, m)


# --------------------------------------------------------------------------------------------
# Replacement report + the final VapourSynth script
# --------------------------------------------------------------------------------------------
TSV_HEADER = ["episode", "frame", "method", "png", "note"]


def replacements_tsv(label: str, rows: list[dict]) -> str:
    """Rows compatible with ``fstk.qc.identity.read_replacements`` (hold rows name the held frame's png)."""
    lines = ["\t".join(TSV_HEADER)]
    for r in sorted(rows, key=lambda r: r["frame"]):
        lines.append("\t".join([label, str(r["frame"]), r["method"], str(r.get("png") or ""), r.get("note", "")]))
    return "\n".join(lines) + "\n"


def base_sha(base_text: str) -> str:
    import hashlib

    return hashlib.sha256(base_text.encode("utf-8")).hexdigest()


def padfix_matches_base(padfix_text: str, base_text: str) -> bool:
    """True when a padfix script was built from exactly this base script (stale-script guard)."""
    return f"# fstk-padfix-base-sha256: {base_sha(base_text)}" in padfix_text


def padfix_script(base_text: str, *, repairs: list[dict], payload: str | None, expected_frames: int) -> str:
    """The base IVTC script (final ``out.set_output()`` removed) plus the PAD splice.

    ``repairs``: [{"frame": i, "method": rife|dvd-cut-frame|hold-prev-scene-cut, "slot": k}] where ``slot`` is the
    index inside the lossless payload clip for image-backed repairs. Holds use ``out[i-1]`` itself, so they stay
    in the chain's own 10-bit pixel format.
    """
    tail = "out.set_output()"
    stripped = base_text.rstrip()
    if not stripped.endswith(tail) or stripped.count(tail) != 1:
        raise ArtifactError("base encode script does not end with exactly one out.set_output(); refusing to patch")
    body = stripped[: -len(tail)].rstrip() + "\n"
    reps = sorted(repairs, key=lambda r: r["frame"])
    frames = [r["frame"] for r in reps]
    if len(set(frames)) != len(frames) or any(f < 1 or f >= expected_frames - 1 for f in frames):
        raise ArtifactError("PAD repair frames must be unique and interior")
    if any(b - a < 2 for a, b in itertools.pairwise(frames)):
        raise ArtifactError("adjacent PAD repairs are not supported (neighbours would be PAD frames)")
    needs_payload = any(r["method"] != HOLD for r in reps)
    if needs_payload and not payload:
        raise ArtifactError("image-backed repairs need a payload clip")
    out = [f"# fstk-padfix-base-sha256: {base_sha(base_text)}", body, "# --- PAD repair (fstk.media.pad_repair; owner rules 2026-11-21) ---",
           f"PAD_REPAIRS = {[(r['frame'], r['method'], r.get('slot')) for r in reps]!r}"]
    if needs_payload:
        out += [f"_payload = core.bs.VideoSource({payload!r})",
                "_payload = core.resize.Bicubic(_payload, format=out.format.id, matrix_s='170m', range_s='limited')"]
    out += ["_pieces, _at = [], 0",
            "for _n, _m, _k in PAD_REPAIRS:",
            "    if _n > _at:",
            "        _pieces.append(out[_at:_n])",
            "    _pieces.append(out[_n - 1:_n] if _m == 'hold-prev-scene-cut' else _payload[_k:_k + 1])",
            "    _at = _n + 1",
            "if _at < out.num_frames:",
            "    _pieces.append(out[_at:])",
            "if _pieces:",
            "    _fixed = _pieces[0]",
            "    for _p in _pieces[1:]:",
            "        _fixed = _fixed + _p",
            "    out = _fixed",
            f"assert out.num_frames == {int(expected_frames)}, ('PAD repair changed the frame count', out.num_frames)",
            "out.set_output()"]
    return "\n".join(out) + "\n"


def write_png(path: Path, rgb: np.ndarray) -> None:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
        raise ArtifactError(f"could not write {path}")
