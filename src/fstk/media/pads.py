"""PAD (inserted placeholder frame) audit math.

Ported verbatim from v38 ``scripts/pad_audit.py``. The detection/descriptor I/O parts
(blackdetect over the source-derived clip, descriptor cache) become a stage.
"""
from __future__ import annotations

from fstk.core.errors import ArtifactError as PipelineError

W, H = 48, 36
F24 = 24000.0 / 1001.0
BLIP_MAX_S = 0.050
TOLERANCE_FRAMES = 2


def boundary_orphan_frames(candidate_frames: list[int], matched_candidates: set[int],
                           frame_count: int) -> set[int]:
    """Exclude boundary orphans, while failing closed for boundary repair candidates."""
    if frame_count < 1:
        raise PipelineError("source-derived clip has no frames")
    boundaries = {frame for frame in candidate_frames if frame in (0, frame_count - 1)}
    unsafe = boundaries & matched_candidates
    if unsafe:
        raise PipelineError(
            "a predicted PAD is at a clip boundary; neighbor-based audit is undefined: "
            f"{sorted(unsafe)}")
    return boundaries


# --------------------------------------------------------------------------------------------
# Audit (v38 ``pad_audit.py`` rules, one decode instead of two)
# --------------------------------------------------------------------------------------------
from dataclasses import dataclass, field  # noqa: E402

import numpy as np  # noqa: E402

#: v38 near-black full-raster verdict (8-bit RGB, 0-255): max ≤ 2 and mean ≤ 0.0005 of full scale.
RGB_MAX_LIMIT = 2
RGB_MEAN_LIMIT = 0.0005 * 255.0
#: Flank-score cut threshold. v38 measured non-cut PAD flanks ≤ 0.3705 and cuts ≥ 0.73 (E-case);
#: anything in between is treated as a cut, which is the safe side (DVD borrow, or hold-previous).
CUT_THRESHOLD = 0.45
AMBIGUOUS_BAND = (0.30, 0.75)


def project_map_pads(map_pads: list[int], *, pad_trim: int, head_trim: int) -> list[int]:
    """Reference-map PAD master frames → output frames (v38: ``(m − pad_trim)*4//5 − head_trim``)."""
    return [(int(m) - int(pad_trim)) * 4 // 5 - int(head_trim) for m in map_pads]


def blips_from_events(events: list[tuple[float, float, float]], *, fps: float = F24,
                      blip_max_s: float = BLIP_MAX_S) -> tuple[list[int], list[tuple[int, int]]]:
    """(single-frame blips, longer black spans as (first, last) frames) from blackdetect events."""
    blips: list[int] = []
    spans: list[tuple[int, int]] = []
    for start, end, dur in events:
        first = round(start * fps)
        if dur <= blip_max_s:
            blips.append(first)
        else:
            spans.append((first, max(first, round(end * fps) - 1)))
    return sorted(set(blips)), spans


def match_blips(predicted: list[int], blips: list[int], *, tolerance: int = TOLERANCE_FRAMES) -> dict[int, int]:
    """Greedy minimum-distance one-to-one matching within ``tolerance`` frames → {predicted: blip}."""
    pairs = sorted((abs(p - b), p, b) for p in predicted for b in blips if abs(p - b) <= tolerance)
    used_p: set[int] = set()
    used_b: set[int] = set()
    out: dict[int, int] = {}
    for _, p, b in pairs:
        if p in used_p or b in used_b:
            continue
        out[p] = b
        used_p.add(p)
        used_b.add(b)
    return out


def flank_scores(thumbs: np.ndarray) -> np.ndarray:
    """``1 − NCC(desc[i−1], desc[i+1])`` for every interior frame (nan at the clip boundary)."""
    n = len(thumbs)
    d = np.stack([_norm(t) for t in thumbs]) if n else np.zeros((0, 1), np.float32)
    out = np.full(n, np.nan, np.float32)
    if n >= 3:
        out[1:-1] = 1.0 - np.einsum("ij,ij->i", d[:-2], d[2:])
    return out


def _norm(a) -> np.ndarray:
    x = np.asarray(a, np.float32).ravel()
    x = x - x.mean()
    n = float(np.linalg.norm(x))
    return x / n if n else x


def is_cut(score: float, threshold: float = CUT_THRESHOLD) -> bool:
    """Ambiguous or unmeasurable scores count as cuts (the conservative branch)."""
    return bool(np.isnan(score) or score >= threshold)


def near_black(rgb: np.ndarray) -> tuple[bool, int, float]:
    """Full-raster verdict for one RGB frame (8-bit): (is_black, max, mean)."""
    mx, mean = int(rgb.max()), float(rgb.mean())
    return (mx <= RGB_MAX_LIMIT and mean <= RGB_MEAN_LIMIT), mx, mean


@dataclass
class PadAuditResult:
    candidates: list[int] = field(default_factory=list)          # repairable output frames
    rows: list[dict] = field(default_factory=list)                # one row per map PAD
    orphans: list[int] = field(default_factory=list)              # blips with no map PAD (dark content)
    counts: dict[str, int] = field(default_factory=dict)


def classify_pads(projected: dict[int, int], *, frames: int, blips: list[int], spans: list[tuple[int, int]] = (),
                  tolerance: int = TOLERANCE_FRAMES) -> PadAuditResult:
    """Account for every map PAD. ``projected`` is {map pad master frame: output frame}.

    Statuses: ``trimmed_away`` (outside the output), ``inside_break_black`` (inside a longer black span),
    ``matched`` (a single-frame blip within tolerance → repair candidate), ``never_rendered``
    (no blip at all). Blips that match no map PAD are dark content and are left alone.
    """
    res = PadAuditResult()
    inside = [(m, o) for m, o in projected.items() if 0 <= o < frames]
    in_span = {o for _, o in inside if any(a - tolerance <= o <= b + tolerance for a, b in spans)}
    candidates = [o for _, o in inside if o not in in_span]
    matched = match_blips(candidates, blips, tolerance=tolerance)
    boundary = boundary_orphan_frames(sorted(set(matched.values())), set(matched.values()), frames)
    for m, o in sorted(projected.items()):
        if not 0 <= o < frames:
            status, at = "trimmed_away", None
        elif o in in_span:
            status, at = "inside_break_black", None
        elif o in matched:
            status, at = "matched", matched[o]
        else:
            status, at = "never_rendered", None
        res.rows.append({"map_pad": m, "predicted": o, "status": status, "blip": at})
    res.candidates = sorted(matched.values())
    res.orphans = sorted(b for b in blips if b not in set(matched.values()) | boundary)
    c = {k: sum(r["status"] == k for r in res.rows)
         for k in ("trimmed_away", "inside_break_black", "never_rendered", "matched")}
    res.counts = {"map_pads": len(res.rows), "rendered_isolated": c["matched"], **{k: c[k] for k in (
        "trimmed_away", "inside_break_black", "never_rendered")}}
    return res
