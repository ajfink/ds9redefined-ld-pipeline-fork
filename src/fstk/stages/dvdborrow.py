"""Borrowing DVD frames for operator frame fixes (``frame-fixes``) and the quality pass's DVD comparisons.

The DVD window around a delivered frame's aligned position is decoded through the same field matching the DVD
needs (TFM + TDecimate) and handed to ``media.frame_replace.borrow_dvd_frame``. (Formerly part of the PAD
stages, removed by ADR 0004.)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from fstk.core.fsio import atomic_write_bytes
from fstk.media import frame_replace as fr

DVD_WINDOW = 15   # DVD grid frames either side of the aligned position


def dvd_window_script(dvd: Path, first: int, last: int) -> str:
    """Progressive 23.976 window of the DVD between grid frames ``first``..``last``.

    Grid frames are the alignment's: the DVD decoded at 30000/1001 with its pulldown flags honoured (FFmpeg,
    ``gray_thumbnails``). BestSource returns *coded* frames unless ``rff=True``; on a soft-telecined DVD that is
    ~4 frames per 5 (S01E20: the quality windows asked for frame 77954 of a clip that ended before it, and every
    earlier window showed the wrong moment). With ``rff=True`` frame n is the same 1/29.97 s as grid frame n."""
    return "\n".join([
        "import vapoursynth as vs", "core = vs.core",
        f"src = core.bs.VideoSource({str(dvd)!r}, rff=True)",
        f"src = core.std.Trim(src, {int(first)}, {int(last)})",
        "src = core.std.SetFrameProps(src, _FieldBased=2)",
        "fm = core.tivtc.TFM(src, order=-1, PP=0, chroma=1, slow=2)",
        "out = core.tivtc.TDecimate(fm, mode=1, cycle=5, cycleR=1, chroma=1)",
        "out.set_output()", ""])


def ld_to_master_frame(out_frame: int, *, head_trim: int, pad: int) -> int:
    """Inverse of the v38 mapping ``(master − pad)*4//5 − head`` (nearest source frame of the cadence);
    ``pad`` is the selected capture's lead-in from the Orc alignment map (``audio.pad_frames``)."""
    return (out_frame + head_trim) * 5 // 4 + pad


def dvd_borrow(ctx, work: Path, f: int, ld_prev, ld_next, gap: int, *, head: int, pad: int):
    """DVD frames standing in for delivered frames ``f``..``f+gap−1``, registered to the LD raster and
    colour-matched to an LD neighbour → (Borrowed | None, reason)."""
    m = ctx.manifest
    rec = m.require("video.dvd_alignment")
    master = ld_to_master_frame(f, head_trim=head, pad=pad)
    act = next((a for a in rec["acts"] if a["start_frame"] <= master <= a["end_frame"]), None)
    if act is None:
        return None, "no accepted act covers this frame"
    off = float(np.interp(master, act["knots"], act["values"]))
    centre = round(master + off)
    first = max(0, centre - DVD_WINDOW)
    work.mkdir(parents=True, exist_ok=True)
    win = work / f"dvd-window-{f}.vpy"
    atomic_write_bytes(win, dvd_window_script(ctx.artifact_path("dvd", m), first, centre + DVD_WINDOW + gap).encode())
    vs = ctx.tool("vapoursynth")
    info = vs.info(win)
    frames = vs.frames(win, 0, info["frames"] - 1, width=int(info["width"]), height=int(info["height"]))
    got = fr.borrow_dvd_frame(ld_prev, ld_next, frames, gap)
    return got, ("" if got else f"flank frames not found {gap + 1} apart in the aligned DVD window, or the "
                                "registered DVD frames do not reproduce the LD neighbour")
