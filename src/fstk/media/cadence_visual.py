"""Pictures for the cadence review: where the film pattern breaks, and what those frames look like.

The cadence check holds an episode for review when its TFM numbers fall outside the ranges of finished episodes.
Numbers mean nothing to someone deciding by eye, so this module picks the flagged places (unmatched fields first,
then sustained combing, video-like inserts, pattern breaks), and for each shows a close-up of the most combed part of
the frame at full resolution (doubled, never smoothed, so the field "teeth" stay visible), the next frame at the same
place (Blink between the two shows whether the picture is a blend or a jump), and the whole frame for context. Two
clean places are added for comparison. Pure numpy/Pillow; frames come from the caller.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from fstk.media.cadence import FPS

TILE = (360, 270)
CROP = (180, 135)               # native pixels, shown ×2
HEAD, PAD, LABEL = 64, 10, 30
MAX_SPOTS = 8
VIEWS = ("Close-up", "Next frame", "Whole frame")
PRIORITY = ("unmatched-frames", "sustained-combing", "interlaced-insert-density", "period-5 deviations",
            "combed frames", "unmatched frames", "native-30p-or-hybrid-check")
PLAIN = {"unmatched-frames": "fields that match no neighbour", "unmatched frames": "fields that match no neighbour",
         "sustained-combing": "combing that lasts", "combed frames": "combed frames",
         "interlaced-insert-density": "looks like a video insert", "period-5 deviations": "film pattern breaks",
         "native-30p-or-hybrid-check": "possibly shot on video"}


@dataclass
class CadenceSpot:
    frame: int                  # TFM frame (after the pad)
    label: str
    tone: str
    reasons: list[str]

    @property
    def time_s(self) -> float:
        return self.frame / FPS


def plain_reasons(reasons) -> str:
    seen = []
    for r in reasons:
        t = PLAIN.get(r, r)
        if t not in seen:
            seen.append(t)
    return ", ".join(seen)


def choose_spots(analysis: dict, mics: list[int], limit: int = MAX_SPOTS) -> list[CadenceSpot]:
    """The flagged intervals, worst kind first, each at its most combed frame; then two clean places."""
    count = len(mics)

    def rank(iv) -> int:
        return min((PRIORITY.index(r) for r in iv.get("reasons", []) if r in PRIORITY), default=len(PRIORITY))

    flagged = sorted(analysis.get("review_intervals", []), key=lambda iv: (rank(iv), iv["start_frame"]))
    spots: list[CadenceSpot] = []
    used: set[int] = set()
    for iv in flagged[: max(limit - 2, 1)]:
        a, b = int(iv["start_frame"]), min(int(iv["end_frame_inclusive"]), count - 2)
        if b < a:
            continue
        f = a + int(np.argmax(mics[a:b + 1]))
        if f in used:
            continue
        used.add(f)
        bad = any("unmatched" in r for r in iv.get("reasons", []))
        spots.append(CadenceSpot(f, f"flagged: {plain_reasons(iv.get('reasons', []))}", "bad" if bad else "warn",
                                 list(iv.get("reasons", []))))
    spots.sort(key=lambda s: s.frame)
    for iv in analysis.get("clean_samples", [])[:2]:
        f = min(int(iv["start_frame"]), count - 2)
        if f not in used and f >= 0:
            spots.append(CadenceSpot(f, "clean place, for comparison", "good", ["clean"]))
    return spots


def frames_needed(spots: list[CadenceSpot], pad: int) -> list[int]:
    """Stack-master frame indices (pad + frame, and the next one) the pictures need."""
    return sorted({pad + s.frame + k for s in spots for k in (0, 1)})


def comb_energy(rgb: np.ndarray) -> np.ndarray:
    """Per-pixel combing: how much a line differs from the average of the lines above and below."""
    g = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    e = np.zeros_like(g)
    e[1:-1] = np.abs(2 * g[1:-1] - g[:-2] - g[2:])
    return e


def worst_crop(rgb: np.ndarray) -> tuple[int, int]:
    """Top-left (x, y) of the CROP window with the most combing (y even, so fields stay in order)."""
    h, w = rgb.shape[:2]
    cw, ch = min(CROP[0], w), min(CROP[1], h)
    e = comb_energy(rgb)
    step = 16
    best, at = -1.0, (0, 0)
    sat = e.cumsum(0).cumsum(1)
    sat = np.pad(sat, ((1, 0), (1, 0)))
    for y in range(0, h - ch + 1, step):
        for x in range(0, w - cw + 1, step):
            v = sat[y + ch, x + cw] - sat[y, x + cw] - sat[y + ch, x] + sat[y, x]
            if v > best:
                best, at = float(v), (x, y)
    return at[0], at[1] - at[1] % 2


def tile_rect(row: int, col: int) -> list[int]:
    tw, th = TILE
    return [PAD + col * (tw + PAD), HEAD + row * (th + LABEL) + LABEL, tw, th]


def render_sheet(spots: list[CadenceSpot], frames: dict[int, np.ndarray], pad: int, path, *, title: str,
                 summary: str):
    """One row per spot: close-up ×2, the next frame's close-up, the whole frame."""
    from PIL import Image, ImageDraw

    tw, th = TILE
    width = PAD + len(VIEWS) * (tw + PAD)
    height = HEAD + len(spots) * (th + LABEL) + PAD
    img = Image.new("RGB", (width, max(height, HEAD + 40)), (24, 24, 24))
    draw = ImageDraw.Draw(img)
    draw.text((PAD, 8), title, fill=(255, 255, 255))
    draw.text((PAD, 26), summary[:160], fill=(190, 190, 190))
    for c, name in enumerate(VIEWS):
        draw.text((PAD + c * (tw + PAD), 46), name, fill=(150, 200, 255))
    colours = {"bad": (240, 110, 110), "warn": (240, 190, 80), "good": (120, 210, 120)}
    for r, s in enumerate(spots):
        y = HEAD + r * (th + LABEL)
        m, s_min = divmod(s.time_s, 60)
        draw.text((PAD, y + 8), f"{int(m)}:{s_min:04.1f}  {s.label}"[:120], fill=colours.get(s.tone, (230, 230, 230)))
        now, nxt = frames.get(pad + s.frame), frames.get(pad + s.frame + 1)
        if now is None:
            continue
        x0, y0 = worst_crop(now)
        cw, ch = min(CROP[0], now.shape[1]), min(CROP[1], now.shape[0])
        for c, src in enumerate((now, nxt if nxt is not None else now)):
            crop = Image.fromarray(np.ascontiguousarray(src[y0:y0 + ch, x0:x0 + cw]))
            img.paste(crop.resize(TILE, Image.Resampling.NEAREST), tuple(tile_rect(r, c)[:2]))
        whole = Image.fromarray(now).resize(TILE, Image.Resampling.BILINEAR)
        ImageDraw.Draw(whole).rectangle((x0 * tw / now.shape[1], y0 * th / now.shape[0],
                                         (x0 + cw) * tw / now.shape[1], (y0 + ch) * th / now.shape[0]),
                                        outline=(255, 220, 0), width=2)
        img.paste(whole, tuple(tile_rect(r, 2)[:2]))
    img.save(path, format="PNG")
    return path


def render_note(path, title: str, text: str) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 90), (24, 24, 24))
    d = ImageDraw.Draw(img)
    d.text((10, 10), title[:120], fill=(255, 255, 255))
    d.text((10, 40), text[:200], fill=(190, 190, 190))
    img.save(path, format="PNG")


def headline(issues_plain: list[str]) -> str:
    if not issues_plain:
        return "The film pattern is as steady as in the finished episodes; nothing needs a look."
    return ("Held for a look because " + "; ".join(issues_plain) + ". Combing in the close-ups (comb-like 'teeth' "
            "on moving edges) is what the repair will smooth out.")


def review_view(analysis: dict, spots: list[CadenceSpot], *, sheet: str, rendered: bool, issues_plain: list[str],
                held: bool) -> dict:
    duration = analysis.get("frame_count", 0) / FPS
    marks = []
    for iv in analysis.get("insert_regions", []):
        marks.append({"start_s": iv["start_s"], "end_s": iv["end_s"], "tone": "info", "label": "looks like a video insert"})
    for iv in analysis.get("review_intervals", []):
        bad = any("unmatched" in r for r in iv.get("reasons", []))
        marks.append({"start_s": iv["start_s"], "end_s": max(iv["end_s"], iv["start_s"] + 1.0),
                      "tone": "bad" if bad else "warn", "label": plain_reasons(iv.get("reasons", []))})
    rows = [{"time_s": round(s.time_s, 2), "label": s.label,
             "verdict": "clean" if s.tone == "good" else ("needs a look" if s.tone == "bad" else "check"),
             "tone": s.tone, "detail": "",
             "views": [{"name": v, "rect": tile_rect(r, c)} for c, v in enumerate(VIEWS)] if rendered else []}
            for r, s in enumerate(spots)]
    return {"title": "Film cadence", "verdict": "CHECK THE FLAGGED PLACES" if held else "LOOKS GOOD",
            "tone": "warn" if held else "good", "headline": headline(issues_plain), "duration_s": duration,
            "timeline": marks, "sheet": sheet, "spots": rows,
            "how_to_look": "Compare a flagged close-up with a clean one. Approve if the flagged places are real "
                           "video-shot or edited moments; reject if the whole picture is torn or the capture looks "
                           "broken."}
