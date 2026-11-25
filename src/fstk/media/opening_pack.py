"""DVD opening packs: the DVD's first seconds as alignment descriptors, for people who do not have the DVD.

The programme start on the LD is found by searching for the DVD's opening pictures as one sequence
(``alignment.find_opening``, ADR 0006). That needs only the DVD's first seconds, as 48×36 grey descriptors on the
30000/1001 grid (the alignment's own recipe), not the DVD itself and not viewable pictures. A pack holds those
descriptors for one episode: the opening run itself (12 s for most episodes, longer when the opening shot is nearly
still; ``alignment.opening_length``) plus ``SPARE_S``, where the opening starts on the DVD, and how the pack was made. Packs ship with the app under ``fstk/data/dvd_openings/<series>/<code>.npz``;
``fstk openings export`` makes one from a DVD.

Different ffmpeg builds decode soft-pulldown DVD frames slightly differently (a few repeated-field frames in a
hundred); the opening search is a whole-sequence correlation and finds the same LD frame either way (measured on
S01E20 with ffmpeg 4.4, 6.1 and the Windows build: LD 1514, NCC 0.975 vs runner-up 0.28 at 6 s).
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from fstk.media import alignment as al

SCHEMA = "fstk-dvd-opening-v1"
SPARE_S = 1.0                         # stored past the opening run (the self-score check looks half a second on)
EXPORT_S = al.OPENING["dvd_search_s"] + max(al.OPENING["lengths"]) + 2 * SPARE_S   # DVD seconds an export decodes
RECIPE = "ffmpeg -map 0:v:0 -vf scale=48:36:flags=area,format=gray -f rawvideo -pix_fmt gray (sequential, frame 0 on)"
PACK_ROOT = Path(__file__).resolve().parents[1] / "data" / "dvd_openings"


@dataclass
class OpeningPack:
    descriptors: np.ndarray           # uint8 (frames, 1728): DVD frames from the opening's first frame on
    meta: dict[str, Any]

    @property
    def opening_dvd_frame(self) -> int:
        return int(self.meta["opening_dvd_frame"])

    @property
    def seconds(self) -> float:
        return float(self.meta["seconds"])


def first_textured(desc: np.ndarray, *, limit_s: float | None = None) -> int | None:
    lim = len(desc) if limit_s is None else min(len(desc), int(limit_s * al.FPS))
    hits = np.flatnonzero(~al.flat_rows(np.asarray(desc[:lim]), al.DEFAULTS["flat_std"]))
    return int(hits[0]) if len(hits) else None


def distinctiveness(desc: np.ndarray, start: int, seconds_list=(2, 3, 4, 6, 8, 12), *,
                    stride: int = al.OPENING["stride"], exclude: int = al.OPENING["exclude_frames"]) -> dict:
    """How clearly the opening run beginning at ``start`` stands out from everything else in ``desc`` (the DVD's
    own following minutes stand in for the LD, which holds the same programme): best NCC outside the run's own
    peak, per length. Low is good; the real match on an LD is ≈ 0.97."""
    norm = al.normalize_rows(np.asarray(desc))
    flat = al.flat_rows(np.asarray(desc), al.DEFAULTS["flat_std"])
    out = {}
    for secs in seconds_list:
        length = round(secs * al.FPS)
        cols = start + np.arange(0, length, stride)
        cols = cols[(cols < len(desc))]
        cols = cols[~flat[cols]]
        npos = len(desc) - length
        if len(cols) < 3 or npos <= 0:
            continue
        corr = norm @ norm[cols].T
        score = np.zeros(npos)
        for j, c in enumerate(cols - start):
            score += corr[c:c + npos, j]
        score /= len(cols)
        others = al.outside_peak(score, start, exclude)
        out[str(secs)] = {"runner_up_ncc": round(float(others.max()), 3) if others.size else None,
                          "frames": len(cols), "self_ncc": round(float(score[start]), 3)}
    return out


def make_pack(dvd_desc: np.ndarray, *, series: str, code: str, source: dict[str, Any], ffmpeg: str,
              title: str | None = None, check_against: np.ndarray | None = None) -> OpeningPack:
    """A pack from a DVD's descriptors (frame 0 onwards, ``EXPORT_S`` seconds or to the DVD's end).
    ``check_against`` (default: the same descriptors) is what the distinctiveness is measured on."""
    start = first_textured(dvd_desc, limit_s=al.OPENING["dvd_search_s"])
    if start is None:
        raise ValueError(f"{code}: no picture in the DVD's first {al.OPENING['dvd_search_s']:.0f} s")
    dflat = al.flat_rows(np.asarray(dvd_desc), al.DEFAULTS["flat_std"])
    length, self_ncc = al.opening_length(dvd_desc, start, dflat)
    end = start + length + round(SPARE_S * al.FPS)
    if len(dvd_desc) < end:
        raise ValueError(f"{code}: need {end} DVD frames for the pack, got {len(dvd_desc)}")
    seconds = round(length / al.FPS, 3)
    meta = {"schema": SCHEMA, "series": series, "code": code, "title": title, "opening_dvd_frame": start,
            "seconds": seconds, "self_ncc_half_s": round(self_ncc, 3), "frames": end - start,
            "grid": "30000/1001", "descriptor": f"{al.W}x{al.H} grey uint8", "recipe": RECIPE,
            "ffmpeg": ffmpeg, "source": source}
    meta["distinctiveness"] = distinctiveness(check_against if check_against is not None else dvd_desc, start,
                                              (seconds,))
    return OpeningPack(np.ascontiguousarray(dvd_desc[start:end], dtype=np.uint8), meta)


def save_pack(pack: OpeningPack, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    np.savez_compressed(buf, descriptors=pack.descriptors, meta=np.frombuffer(json.dumps(pack.meta).encode(), np.uint8))
    tmp = path.with_suffix(".partial")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)
    return path


def load_pack(path: Path) -> OpeningPack:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(bytes(z["meta"]).decode())
        desc = np.array(z["descriptors"], dtype=np.uint8)
    if meta.get("schema") != SCHEMA or desc.ndim != 2 or desc.shape[1] != al.FEATURES or len(desc) != meta["frames"]:
        raise ValueError(f"{path}: not a {SCHEMA} pack (or damaged)")
    return OpeningPack(desc, meta)


def pack_path(series: str, code: str, root: Path | None = None) -> Path:
    return (root or PACK_ROOT) / series.upper() / f"{code.upper()}.npz"


def find_pack(series: str, code: str, root: Path | None = None) -> OpeningPack | None:
    path = pack_path(series, code, root)
    return load_pack(path) if path.is_file() else None


def find_start(master_head: np.ndarray, pack: OpeningPack, *, search_s: float = 180.0) -> dict[str, Any]:
    """Where the pack's DVD opening is on the LD (``master_head``: the master's first descriptors).
    ``master_frame`` is the LD frame showing the DVD's ``opening_dvd_frame``."""
    op = al.find_opening(master_head, pack.descriptors, search_s=search_s, seconds=pack.seconds)
    for t in [op, *op.get("tried", [])]:
        if "dvd_frame" in t:
            t["dvd_frame"] += pack.opening_dvd_frame
    if op.get("found"):
        op["offset"] = op["dvd_frame"] - op["master_frame"]
    op["pack"] = {k: pack.meta.get(k) for k in ("series", "code", "title", "opening_dvd_frame", "seconds", "ffmpeg")}
    return op


def is_distinct(pack: OpeningPack) -> bool:
    """The run picks out one place: nothing else in its own DVD's first minutes scores near a real match."""
    runner = pack.meta["distinctiveness"][str(pack.seconds)]["runner_up_ncc"]
    return runner is None or runner <= al.OPENING["min_ncc"] - al.OPENING["min_margin"]


def export(ff, dvd: Path, *, series: str, code: str, title: str | None = None,
           descriptors: np.ndarray | None = None, out: Path | None = None, force: bool = False,
           cancel=None) -> dict[str, Any]:
    """Make and save a pack from a DVD rip (``fstk openings export`` and the app's Save icon on dvd-probe).

    ``descriptors``: the DVD's descriptors already decoded by dvd-alignment (frame 0 on), used when they reach far
    enough; otherwise the DVD's first ``EXPORT_S`` seconds are decoded. A run that is not distinct is refused
    unless ``force``: ``{"written": False, "why": ...}``."""
    need = round(EXPORT_S * al.FPS)
    if descriptors is not None and len(descriptors) >= need:
        desc, decoded = np.asarray(descriptors[:need], dtype=np.uint8), False
    else:
        desc, decoded = ff.gray_thumbnails(Path(dvd), al.W, al.H, cancel=cancel, max_frames=need), True
    pack = make_pack(desc, series=series, code=code, title=title, ffmpeg=ff.version(),
                     source={"file": Path(dvd).name, "bytes": Path(dvd).stat().st_size})
    runner = pack.meta["distinctiveness"][str(pack.seconds)]["runner_up_ncc"]
    if not is_distinct(pack) and not force:
        return {"written": False, "pack": pack, "decoded": decoded,
                "why": f"the DVD's opening run also scores {runner} elsewhere in its first minutes, so it would not "
                       f"pick out one place on the LD"}
    path = save_pack(pack, out or pack_path(series, code))
    return {"written": True, "path": path, "pack": pack, "decoded": decoded, "runner_up_ncc": runner}
