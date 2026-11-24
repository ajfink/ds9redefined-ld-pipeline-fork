"""Finishing script: the chain (encode script + operator fixes) plus crop, optional chroma-only denoise and
dropout concealment.

Stacked on that chain's text (``# fstk-finish-base-sha256`` guards staleness). Order is fixed and
deliberate: chain → dropout concealment (needs the real neighbours) →
chroma denoise (luma, i.e. grain, is never touched) → crop.
"""
from __future__ import annotations

from fstk.core.errors import ArtifactError
from fstk.media.frame_replace import base_sha

TAIL = "out.set_output()"


def finish_matches_base(finish_text: str, base_text: str) -> bool:
    return f"# fstk-finish-base-sha256: {base_sha(base_text)}" in finish_text


def finish_script(base_text: str, *, expected_frames: int, crop: dict | None = None,
                  chroma_denoise: dict | None = None, dropout_fixes: list[tuple] | None = None) -> str:
    """``dropout_fixes``: (frame, src, dx, dy, x, y, w, h). ``chroma_denoise``: {"strength": 0..1, "limit": codes}.

    ``crop``: {"left","top","right","bottom"} (all even). Every option off → the script equals the base.
    """
    stripped = base_text.rstrip()
    if not stripped.endswith(TAIL) or stripped.count(TAIL) != 1:
        raise ArtifactError("base script does not end with exactly one out.set_output(); refusing to patch")
    out = [f"# fstk-finish-base-sha256: {base_sha(base_text)}", stripped[: -len(TAIL)].rstrip() + "\n"]
    fixes = sorted(dropout_fixes or [])
    if fixes:
        for f in fixes:
            if f[0] < 1 or f[0] >= expected_frames - 1:
                raise ArtifactError(f"dropout fix at frame {f[0]} touches the clip boundary")
            if f[1] not in (-1, 1) or any(v % 2 for v in (f[2], f[4], f[6])):
                raise ArtifactError(f"dropout fix {f} must use src ±1 and even dx/x/w (chroma alignment)")
        out += ["# --- dropout concealment from neighbouring LD frames (fstk.quality.conceal) ---",
                f"DROPOUT_FIXES = {fixes!r}",
                "_W, _H = out.width, out.height",
                "_base = out",
                "def _mask_for(x, y, w, h):",
                "    _one = core.std.BlankClip(_base, width=w, height=h, length=1, format=_base.format.replace("
                "color_family=vs.GRAY, subsampling_w=0, subsampling_h=0), color=[(1 << _base.format.bits_per_sample) - 1])",
                "    return core.std.AddBorders(_one, left=x, top=y, right=_W - x - w, bottom=_H - y - h, color=[0])",
                "def _patched(n, fixes):",
                "    cur = _base[n]",
                "    for _n, src, dx, dy, x, y, w, h in fixes:",
                "        s = _base[n + src]",
                "        piece = core.std.Crop(s, left=x + dx, top=y + dy, right=_W - (x + dx) - w, bottom=_H - (y + dy) - h)",
                "        over = core.std.AddBorders(piece, left=x, top=y, right=_W - x - w, bottom=_H - y - h)",
                "        cur = core.std.MaskedMerge(cur, over, _mask_for(x, y, w, h), first_plane=True)",
                "    return cur",
                "_by_frame = {}",
                "for _f in DROPOUT_FIXES:",
                "    _by_frame.setdefault(_f[0], []).append(_f)",
                "_pieces, _at = [], 0",
                "for _n in sorted(_by_frame):",
                "    if _n > _at:",
                "        _pieces.append(_base[_at:_n])",
                "    _pieces.append(_patched(_n, _by_frame[_n]))",
                "    _at = _n + 1",
                "if _at < _base.num_frames:",
                "    _pieces.append(_base[_at:])",
                "out = _pieces[0]",
                "for _p in _pieces[1:]:",
                "    out = out + _p",
                f"assert out.num_frames == {expected_frames}, ('dropout concealment changed the frame count', out.num_frames)"]
    if chroma_denoise:
        s, lim = float(chroma_denoise["strength"]), float(chroma_denoise["limit"])
        if not 0 < s <= 1 or lim <= 0:
            raise ArtifactError("chroma_denoise needs 0 < strength <= 1 and limit > 0 (8-bit codes)")
        out += ["# --- chroma-only temporal denoise in static areas (luma / grain untouched) ---",
                "_bits = out.format.bits_per_sample",
                "_k = 1 << (_bits - 8)",
                "_prev, _next = out[0] + out[:-1], out[1:] + out[-1]",
                "_ly = [core.std.ShufflePlanes(c, 0, vs.GRAY) for c in (out, _prev, _next)]",
                "_act = core.std.Expr(_ly, 'x y - abs x z - abs max')",
                "_cw, _ch = out.width >> out.format.subsampling_w, out.height >> out.format.subsampling_h",
                "_actc = core.resize.Bilinear(_act, width=_cw, height=_ch)",
                "_mask = core.std.ShufflePlanes([out, _actc, _actc], [0, 0, 0], vs.YUV)",
                f"_lim, _T = {lim} * _k, 6 * _k",
                f"_expr = f'a {{_T}} < x y + z + 3 / x - {{_lim}} min -{{_lim}} max {s} * x + x ?'",
                "out = core.std.Expr([out, _prev, _next, _mask], ['x', _expr, _expr])"]
    if crop and any(crop.values()):
        c = crop
        if any(int(v) % 2 for v in c.values()):
            raise ArtifactError("crop values must be even (chroma alignment)")
        out += ["# --- locked active-area crop (measured against the DVD; fstk.quality.geometry) ---",
                f"out = core.std.Crop(out, left={int(c['left'])}, right={int(c['right'])}, "
                f"top={int(c['top'])}, bottom={int(c['bottom'])})"]
    out.append(TAIL)
    return "\n".join(out) + "\n"
