"""Replacement-image identity gate (v38 ``verify_pad_identities.py``), extended to every PAD.

For each repaired PAD frame the delivered frame's 48×36 grey descriptor must match *its own*
replacement image (NCC ≥ 0.99) and be clearly lower against every other replacement
(separation ≥ 0.10). This is the check that once caught a silently drifted replacement layout
(a single-PAD preview passed while 43 PADs held the wrong image).

Change from v38: v38 sampled three PADs (first/middle/last) with one input seek each. fstk
decodes the deliverable once, sequentially, to 48×36 grey thumbnails and checks **every**
repaired PAD by exact frame index (no seeking, no sampling). Descriptors are made exactly as in
v38: frames via FFmpeg ``scale=48:36:flags=area,format=gray``; replacement images via OpenCV
``INTER_AREA`` resize + grey conversion; zero-mean, unit-norm vectors.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError

FPS = 24000.0 / 1001.0
MIN_OWN = 0.99
MIN_SEPARATION = 0.10
THUMB_W, THUMB_H = 48, 36


def norm(a: np.ndarray) -> np.ndarray:
    x = a.astype(np.float32).ravel()
    x -= x.mean()
    n = np.linalg.norm(x)
    return x / n if n else x


def image_desc(path: Path) -> np.ndarray:
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ArtifactError(f"replacement image missing/unreadable: {path}")
    small = cv2.resize(img, (THUMB_W, THUMB_H), interpolation=cv2.INTER_AREA)
    if small.ndim == 3:
        small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY if small.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
    return norm(small)


def read_replacements(tsv: Path, label: str, probe: Path) -> dict[int, dict]:
    """v38 ``rife-pad-replacements-local.tsv`` rows for one episode label → {frame: {png, method}}."""
    if not tsv.is_file():
        raise ArtifactError(f"replacement report missing: {tsv}")
    out = {}
    with tsv.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if r.get("episode") != label:
                continue
            png = Path(r["png"])
            if not png.is_absolute():
                png = probe / png
                if not png.exists():   # older rows recorded only a file name
                    png = probe / "rife-padframes-local" / label / "interpolated" / Path(r["png"]).name
            out[int(r["frame"])] = {"png": png, "method": r.get("method", "")}
    return out


def identity_check(pads: list[int], replacements: dict[int, dict], thumbs: np.ndarray) -> dict:
    """``thumbs``: (frames, 48*36) uint8 from one sequential decode. Every PAD is checked."""
    pads = sorted(set(pads))
    if not pads:
        return {"rows": [], "pass": True, "note": "no PAD frames were repaired; identity check not applicable"}
    missing = [n for n in pads if n not in replacements]
    if missing:
        raise ArtifactError(f"no recorded replacement for PAD frame(s) {missing[:5]}{'…' if len(missing) > 5 else ''}")
    out_of_range = [n for n in pads if n >= len(thumbs)]
    if out_of_range:
        raise ArtifactError(f"PAD frame(s) {out_of_range[:5]} beyond the decoded {len(thumbs)} frames")
    pool = {n: image_desc(replacements[n]["png"]) for n in pads}
    keys = list(pool)
    matrix = np.stack([pool[n] for n in keys])
    rows, failures = [], []
    for frame in pads:
        a = norm(thumbs[frame])
        scores = matrix @ a
        own_i = keys.index(frame)
        own = float(scores[own_i])
        if len(keys) > 1:
            others = scores.copy()
            others[own_i] = -np.inf
            j = int(np.argmax(others))
            best_other, other_frame = float(others[j]), keys[j]
        else:
            best_other, other_frame = -1.0, None
        sep = own - best_other
        ok = own >= MIN_OWN and sep >= MIN_SEPARATION
        if not ok:
            failures.append(frame)
        rows.append({"frame": frame, "time_s": round(frame / FPS, 6), "method": replacements[frame]["method"],
                     "own_ncc": round(own, 6), "best_other_ncc": round(best_other, 6),
                     "best_other_frame": other_frame, "separation": round(sep, 6), "pass": ok})
    return {"rows": rows, "failures": failures, "pass": not failures,
            "own_min": min(r["own_ncc"] for r in rows),
            "other_max": max(r["best_other_ncc"] for r in rows)}
