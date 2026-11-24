"""Operator frame fixes chosen in the final review (contact sheet → frame or range → action).

Every fix becomes part of the VapourSynth chain that the single final encode reads from the lossless master;
nothing ever patches an encoded file. Actions, by how wide the defect is:

``interpolate``   1–3 whole frames rebuilt from the real LD frames either side (MVTools FlowInter). Refused
                  across a shot change. Interpolation smooths grain; the stage measures how much and reports it.
``ld-patch``      a rectangle of 1+ frames copied from the clean LD frame just outside the range (``src`` −1 =
                  the frame before, +1 = after). Real LD pixels; over several frames the patched area's grain
                  is frozen, which the review preview shows.
``dvd``           whole frames, or only a rectangle, taken from the DVD: found by matching the LD frames either
                  side in the aligned DVD window, registered to the LD raster and colour-matched to the LD
                  neighbour (v38's PAD-borrow method). Opt-in only, never automatic (owner decision 2026-11-22).
``leave``         reviewed and left as captured (recorded so the report says so).

Frame numbers are those of the delivered episode (after trims); rectangles are in the chain's full raster
(before the active-area crop) and must be even (4:2:x chroma alignment).
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from fstk.core.errors import ArtifactError
from fstk.media.frame_replace import base_sha

INTERPOLATE, LD_PATCH, DVD, LEAVE = "interpolate", "ld-patch", "dvd", "leave"
ACTIONS = (INTERPOLATE, LD_PATCH, DVD, LEAVE)
MAX_INTERPOLATE = 3
PREVIEW_MAX_FRAMES = 48          # in-context preview clips are rendered for fixes up to 2 s long
TAIL = "out.set_output()"


@dataclass(frozen=True)
class Fix:
    first: int
    last: int
    action: str
    rect: tuple[int, int, int, int] | None = None        # x, y, w, h
    src: int | None = None                               # ld-patch: −1 (frame before) / +1 (frame after)
    note: str = ""

    @property
    def length(self) -> int:
        return self.last - self.first + 1

    def as_dict(self) -> dict:
        d = {"first": self.first, "last": self.last, "action": self.action, "note": self.note}
        if self.rect is not None:
            d["rect"] = list(self.rect)
        if self.src is not None:
            d["src"] = self.src
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Fix:
        rect = tuple(int(v) for v in d["rect"]) if d.get("rect") is not None else None
        return cls(int(d["first"]), int(d["last"]), str(d["action"]), rect,  # type: ignore[arg-type]
                   int(d["src"]) if d.get("src") is not None else None, str(d.get("note", "")))


def validate(fixes: list[Fix], *, frames: int, width: int | None = None, height: int | None = None) -> list[str]:
    """Problems with a set of fixes (empty list = fine)."""
    issues = []
    spans = sorted((f.first, f.last) for f in fixes)
    for (a0, a1), (b0, _b1) in pairwise(spans):
        if b0 <= a1 + 1:
            issues.append(f"fixes at {a0}..{a1} and {b0} overlap or touch: merge them into one range")
    for f in fixes:
        tag = f"fix {f.first}..{f.last} ({f.action})"
        if f.action not in ACTIONS:
            issues.append(f"{tag}: unknown action")
            continue
        if f.first > f.last:
            issues.append(f"{tag}: first frame after last")
        if f.action != LEAVE and (f.first < 1 or f.last > frames - 2):
            issues.append(f"{tag}: needs a real frame on both sides (frames 1..{frames - 2})")
        if f.action == INTERPOLATE and f.length > MAX_INTERPOLATE:
            issues.append(f"{tag}: interpolation is limited to {MAX_INTERPOLATE} frames; use a region patch or "
                          "the DVD for wider damage")
        if f.action == INTERPOLATE and f.rect is not None:
            issues.append(f"{tag}: interpolation rebuilds whole frames (no rectangle)")
        if f.action == LD_PATCH and (f.rect is None or f.src not in (-1, 1)):
            issues.append(f"{tag}: a region patch needs a rectangle and src −1 or +1")
        if f.rect is not None:
            x, y, w, h = f.rect
            if any(v % 2 for v in f.rect) or w <= 0 or h <= 0 or x < 0 or y < 0:
                issues.append(f"{tag}: rectangle must be positive and even")
            elif width and height and (x + w > width or y + h > height):
                issues.append(f"{tag}: rectangle leaves the {width}x{height} picture")
        if f.action != LEAVE and len(f.note.strip()) < 8:
            issues.append(f"{tag}: say what is wrong there (at least 8 characters)")
    return issues


def manualfix_matches_base(text: str, base_text: str) -> bool:
    return f"# fstk-manualfix-base-sha256: {base_sha(base_text)}" in text


def manualfix_script(base_text: str, *, fixes: list[Fix], expected_frames: int, payload: str | None = None,
                     slots: dict[int, int] | None = None) -> str:
    """The encode-script chain plus the operator's fixes, as one script for the single final encode.

    ``slots``: delivered frame → index in the lossless ``payload`` clip (DVD frames, already registered and
    colour-matched). With no fixes the script is the base plus the guard header (so the chain is always
    resolved the same way).
    """
    stripped = base_text.rstrip()
    if not stripped.endswith(TAIL) or stripped.count(TAIL) != 1:
        raise ArtifactError("base script does not end with exactly one out.set_output(); refusing to patch")
    problems = validate(fixes, frames=expected_frames)
    if problems:
        raise ArtifactError("; ".join(problems))
    active = [f for f in sorted(fixes, key=lambda f: f.first) if f.action != LEAVE]
    out = [f"# fstk-manualfix-base-sha256: {base_sha(base_text)}", stripped[: -len(TAIL)].rstrip() + "\n"]
    if not active:
        out += ["# --- operator frame fixes: none ---", TAIL]
        return "\n".join(out) + "\n"
    slots = slots or {}
    if any(f.action == DVD for f in active):
        need = [n for f in active if f.action == DVD for n in range(f.first, f.last + 1)]
        missing = [n for n in need if n not in slots]
        if not payload or missing:
            raise ArtifactError(f"DVD fixes need payload frames; missing for {missing[:5]}")
    spec = [(f.first, f.last, f.action, list(f.rect) if f.rect else None, f.src) for f in active]
    out += ["# --- operator frame fixes (fstk.media.manual_fix; chosen in the final review) ---",
            f"MANUAL_FIXES = {spec!r}",
            f"DVD_SLOTS = {dict(sorted(slots.items()))!r}",
            "_base = out",
            "_W, _H = _base.width, _base.height"]
    if payload:
        out += [f"_payload = core.bs.VideoSource({payload!r})",
                "_payload = core.resize.Spline36(_payload, format=_base.format.id, matrix_s='170m', range_s='limited',",
                "                                chromaloc_s='left', dither_type='error_diffusion')",
                "_payload = core.std.AssumeFPS(_payload, fpsnum=_base.fps_num, fpsden=_base.fps_den)",
                "assert (_payload.width, _payload.height) == (_W, _H), ('fix payload size', _payload.width, _payload.height)"]
    out += ["def _mask(x, y, w, h):",
            "    one = core.std.BlankClip(_base, width=w, height=h, length=1, format=_base.format.replace("
            "color_family=vs.GRAY, subsampling_w=0, subsampling_h=0), color=[(1 << _base.format.bits_per_sample) - 1])",
            "    return core.std.AddBorders(one, left=x, top=y, right=_W - x - w, bottom=_H - y - h, color=[0])",
            "def _region(cur, src, rect):",
            "    x, y, w, h = rect",
            "    return core.std.MaskedMerge(cur, src, _mask(x, y, w, h), first_plane=True)",
            "def _interp(first, last):",
            "    pair = _base[first - 1] + _base[last + 1]",
            "    sup = core.mv.Super(pair, pel=2, sharp=2, rfilter=4)",
            "    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=8, search=3, truemotion=True)",
            "    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=8, search=3, truemotion=True)",
            "    n = last - first + 1",
            "    return [core.mv.FlowInter(pair, sup, bv, fv, time=100.0 * k / (n + 1), ml=100)[0:1]"
            " for k in range(1, n + 1)]",
            "_replace = {}",
            "for _a, _b, _act, _rect, _src in MANUAL_FIXES:",
            "    if _act == 'interpolate':",
            "        for _k, _c in enumerate(_interp(_a, _b)):",
            "            _replace[_a + _k] = _c",
            "        continue",
            "    for _n in range(_a, _b + 1):",
            "        if _act == 'ld-patch':",
            "            _from = _base[(_a - 1) if _src < 0 else (_b + 1)]",
            "            _replace[_n] = _region(_base[_n], _from, _rect)",
            "        elif _act == 'dvd':",
            "            _d = _payload[DVD_SLOTS[_n]:DVD_SLOTS[_n] + 1]",
            "            _replace[_n] = _region(_base[_n], _d, _rect) if _rect else _d",
            "_pieces, _at = [], 0",
            "for _n in sorted(_replace):",
            "    if _n > _at:",
            "        _pieces.append(_base[_at:_n])",
            "    _pieces.append(_replace[_n])",
            "    _at = _n + 1",
            "if _at < _base.num_frames:",
            "    _pieces.append(_base[_at:])",
            "out = _pieces[0]",
            "for _p in _pieces[1:]:",
            "    out = out + _p",
            f"assert out.num_frames == {int(expected_frames)}, ('frame fixes changed the frame count', out.num_frames)",
            TAIL]
    return "\n".join(out) + "\n"
