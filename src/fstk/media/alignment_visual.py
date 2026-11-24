"""Spot checks and a picture sheet for the DVD alignment, so the review is something a person can *look at*.

The alignment itself is measured on 48×36 descriptors (``fstk.media.alignment``). Here the aligned pairs are
re-checked on real pictures: at chosen master frames the LaserDisc frame is compared with the DVD frames around
the frame the alignment says it matches (±``SEARCH``), at 160×120 grey. A spot check *agrees* when the best match
is the predicted frame (±1) and the pictures are alike. The sheet shows LD | DVD | difference for each check, the
suspicious stretches first. Pure numpy/Pillow; the frames come from the caller.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from fstk.media.alignment import FPS, AlignmentResult

SEARCH = 3                  # DVD frames either side of the predicted one that are compared
CELL = (240, 180)           # picture size in the sheet
SHOT = (240, 180)           # size the frames are extracted at (4:3, like the descriptors; the sheet size)
LIKE = 0.80                 # grey-picture correlation that counts as "the same picture"
UNLIKE = 0.60               # below this nothing around the predicted frame resembles the LD frame
MAX_POINTS = 14
SHEET_PAD, SHEET_HEAD = 10, 64
VIEWS = ("LaserDisc", "DVD", "Difference")
GOOD, SHIFTED, UNSURE = "agrees", "off by frames", "no similar picture"


@dataclass
class Spot:
    master_frame: int
    dvd_frame: int              # the frame the alignment predicts (rounded)
    kind: str                   # "suspect" | "act" | "start" | "end"
    label: str
    verdict: str = ""
    best_shift: int = 0         # DVD frames from the prediction where the picture matches best
    ncc: float = float("nan")   # at the best shift
    ncc_predicted: float = float("nan")

    @property
    def time_s(self) -> float:
        return self.master_frame / FPS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time_s"] = round(self.time_s, 2)
        d["ncc"] = None if np.isnan(self.ncc) else round(float(self.ncc), 3)
        d["ncc_predicted"] = None if np.isnan(self.ncc_predicted) else round(float(self.ncc_predicted), 3)
        return d


def clock(seconds: float) -> str:
    m, s = divmod(max(seconds, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


def choose_spots(res: AlignmentResult, dvd_frames: int, limit: int = MAX_POINTS) -> list[Spot]:
    """Checkpoints: the stretches the alignment could not explain first, then every act's start, middle and
    end of the programme, always on textured pictures the alignment trusted."""
    ok = np.flatnonzero(res.evidenced)
    if not len(ok) or not res.acts:
        return []
    frames = res.idxs[ok].astype(np.int64)

    def snap(master_frame: float) -> int | None:
        j = int(np.argmin(np.abs(frames - master_frame)))
        return int(frames[j])

    wanted: list[tuple[float, str, str]] = []
    for n, act in enumerate(res.acts, 1):
        for s0, s1 in act.inconsistent_spans:
            if s1 - s0 >= 0.5:
                for frac, what in ((0.15, "start"), (0.5, "middle"), (0.85, "end")):
                    wanted.append(((s0 + (s1 - s0) * frac) * FPS, "suspect",
                                   f"act {n}: stretch {clock(s0)}–{clock(s1)} that the checks flagged ({what})"))
    wanted.append((float(res.idxs[ok[0]]), "start", "first picture matched to the DVD"))
    for n, act in enumerate(res.acts, 1):
        span = act.end_frame - act.start_frame
        for frac, what in ((0.25, "first quarter"), (0.5, "middle"), (0.75, "last quarter")):
            wanted.append((act.start_frame + span * frac, "act", f"act {n}: {what}"))
    wanted.append((float(res.idxs[ok[-1]]), "end", "last picture matched to the DVD"))

    spots: list[Spot] = []
    seen: set[int] = set()
    suspects = [w for w in wanted if w[1] == "suspect"][:6]
    rest = [w for w in wanted if w[1] != "suspect"]
    # thin the ordinary points evenly if there are too many, keeping the first and the last
    room = max(limit - len(suspects), 2)
    if len(rest) > room:
        pick = np.unique(np.linspace(0, len(rest) - 1, room).round().astype(int))
        rest = [rest[i] for i in pick]
    for master_frame, kind, label in suspects + rest:
        m = snap(master_frame)
        if m is None or m in seen:
            continue
        act = res.act_for(m) or min(res.acts, key=lambda a: min(abs(m - a.start_frame), abs(m - a.end_frame)))
        d = round(m + float(act.offset(float(m))))
        if d - SEARCH < 0 or d + SEARCH >= dvd_frames:
            continue
        seen.add(m)
        spots.append(Spot(m, d, kind, label))
    return spots


def wanted_indices(spots: list[Spot]) -> tuple[list[int], list[int]]:
    """(master frame indices, DVD frame indices) the spot checks need."""
    master = sorted({s.master_frame for s in spots})
    dvd = sorted({s.dvd_frame + k for s in spots for k in range(-SEARCH, SEARCH + 1)})
    return master, dvd


def _grey(rgb: np.ndarray) -> np.ndarray:
    """160×120 grey, zero mean, unit norm."""
    g = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    g = g.reshape(g.shape[0] // 2, 2, g.shape[1] // 2, 2).mean(axis=(1, 3))
    g -= g.mean()
    n = float(np.linalg.norm(g))
    return g / n if n > 0 else g


def judge(spot: Spot, ld: np.ndarray, dvd_by_index: dict[int, np.ndarray]) -> Spot:
    """Fill in the verdict of one spot check from the LD frame and the DVD frames around the prediction."""
    ref = _grey(ld)
    scores = {k: float((ref * _grey(dvd_by_index[spot.dvd_frame + k])).sum())
              for k in range(-SEARCH, SEARCH + 1) if spot.dvd_frame + k in dvd_by_index}
    if not scores:
        spot.verdict = UNSURE
        return spot
    best = max(scores, key=lambda k: (scores[k], -abs(k)))
    spot.best_shift, spot.ncc, spot.ncc_predicted = best, scores[best], scores.get(0, float("nan"))
    if spot.ncc < UNLIKE:
        spot.verdict = UNSURE
    elif abs(best) <= 1 and spot.ncc >= LIKE:
        spot.verdict = GOOD
    elif spot.ncc >= LIKE:
        spot.verdict = SHIFTED
    else:
        spot.verdict = UNSURE
    return spot


def assess(spots: list[Spot]) -> dict:
    """Plain-language outcome of all the spot checks."""
    n = len(spots)
    good = [s for s in spots if s.verdict == GOOD]
    bad = [s for s in spots if s.verdict != GOOD]
    suspect_bad = [s for s in bad if s.kind == "suspect"]
    if not n:
        verdict, text = "NOT CHECKED", "No picture spot checks could be made."
    elif not bad:
        verdict = "LOOKS GOOD"
        text = (f"All {n} picture spot checks agree: at every checked point the DVD frame the alignment chose is "
                "the best-matching picture for the LaserDisc frame.")
    else:
        verdict = "CHECK THE FLAGGED POINTS"
        parts = [f"{len(good)} of {n} spot checks agree; {len(bad)} need a look"]
        for s in bad[:4]:
            how = (f"best match is {s.best_shift:+d} DVD frame(s) away" if s.verdict == SHIFTED
                   else "no DVD picture nearby resembles it")
            parts.append(f"{clock(s.time_s)} ({how})")
        text = "; ".join(parts[:1]) + (": " + ", ".join(parts[1:]) if len(parts) > 1 else "") + "."
        if not suspect_bad and any(s.kind == "suspect" for s in spots):
            text += " The stretches the numeric checks flagged look right on the pictures."
    return {"verdict": verdict, "text": text, "checked": n, "agree": len(good),
            "recommendation": ("approve" if verdict == "LOOKS GOOD" else "look at the sheet before deciding")}


def render_sheet(spots: list[Spot], master_frames: dict[int, np.ndarray], dvd_frames: dict[int, np.ndarray],
                 path, *, title: str, summary: str):
    """Write the picture sheet (PNG): one row per spot check, LaserDisc | DVD at the predicted frame | difference."""
    from PIL import Image, ImageDraw

    cw, ch = CELL
    pad, head, row_h = SHEET_PAD, SHEET_HEAD, ch + 34
    width = pad + 3 * (cw + pad)
    height = head + len(spots) * row_h + pad
    img = Image.new("RGB", (width, max(height, head + 40)), (24, 24, 24))
    draw = ImageDraw.Draw(img)
    draw.text((pad, 8), title, fill=(255, 255, 255))
    draw.text((pad, 26), summary[:150], fill=(190, 190, 190))
    for c, name in enumerate(("LaserDisc (stack master)", "DVD (predicted frame)", "difference (bright = unlike)")):
        draw.text((pad + c * (cw + pad), 46), name, fill=(150, 200, 255))
    colours = {GOOD: (120, 210, 120), SHIFTED: (240, 190, 80), UNSURE: (240, 110, 110)}
    for r, s in enumerate(spots):
        y = head + r * row_h
        ld, dv = master_frames.get(s.master_frame), dvd_frames.get(s.dvd_frame)
        note = f"{clock(s.time_s)}  {s.label}"
        draw.text((pad, y), note[:110], fill=(235, 235, 235))
        draw.text((pad, y + 14), f"{s.verdict or 'not checked'}"
                  + ("" if np.isnan(s.ncc) else f"  (similarity {s.ncc_predicted:.2f} at the predicted frame; "
                                                f"best {s.ncc:.2f} at {s.best_shift:+d})"),
                  fill=colours.get(s.verdict, (200, 200, 200)))
        if ld is None or dv is None:
            continue
        a = Image.fromarray(ld).resize(CELL)
        b = Image.fromarray(dv).resize(CELL)
        diff = np.abs(_norm_luma(ld) - _norm_luma(dv))
        d = Image.fromarray(np.clip(diff * 2.0, 0, 255).astype(np.uint8)).resize(CELL)
        for c, tile in enumerate((a, b, d.convert("RGB"))):
            img.paste(tile, (pad + c * (cw + pad), y + 30))
        outline = colours.get(s.verdict, (200, 200, 200))
        draw.rectangle((pad + cw + pad - 1, y + 29, pad + cw + pad + cw, y + 30 + ch), outline=outline)
    img.save(path, format="PNG")
    return path


def tile_rect(row: int, col: int) -> list[int]:
    """Where picture ``col`` of spot ``row`` sits in the sheet (x, y, w, h)."""
    cw, ch = CELL
    y = SHEET_HEAD + row * (ch + 34) + 30
    return [SHEET_PAD + col * (cw + SHEET_PAD), y, cw, ch]


def tone(verdict: str) -> str:
    return {GOOD: "good", SHIFTED: "warn", UNSURE: "bad"}.get(verdict, "neutral")


def review_view(res: AlignmentResult, spots: list[Spot], fit: dict, *, sheet: str, duration_s: float,
                rendered: bool) -> dict:
    """What the app shows for this review (``fstk.ui.visual``): verdict, timeline and the spot pictures."""
    overall = {"LOOKS GOOD": "good", "CHECK THE FLAGGED POINTS": "warn"}.get(fit.get("verdict", ""), "neutral")
    marks = []
    for n, act in enumerate(res.acts, 1):
        marks.append({"start_s": act.start_frame / FPS, "end_s": act.end_frame / FPS, "tone": "info", "alpha": 120,
                      "label": f"act {n}: matched to the DVD"})
        for s0, s1 in act.inconsistent_spans:
            marks.append({"start_s": s0, "end_s": max(s1, s0 + 0.5), "tone": "warn",
                          "label": f"act {n}: the numbers flagged this stretch"})
    spot_rows = []
    for r, s in enumerate(spots):
        detail = ("" if np.isnan(s.ncc) else
                  f"Similarity {s.ncc_predicted:.2f} at the chosen DVD frame"
                  + ("" if s.best_shift == 0 else f"; best {s.ncc:.2f} {s.best_shift:+d} frame(s) away"))
        spot_rows.append({"time_s": round(s.time_s, 2), "label": s.label, "verdict": s.verdict or "not checked",
                          "tone": tone(s.verdict), "detail": detail,
                          "views": ([{"name": v, "rect": tile_rect(r, c)} for c, v in enumerate(VIEWS)]
                                    if rendered else [])})
    return {"title": "DVD alignment", "verdict": fit.get("verdict", ""), "tone": overall,
            "headline": fit.get("text", ""), "duration_s": duration_s, "timeline": marks,
            "sheet": sheet, "spots": spot_rows,
            "how_to_look": "Pick a spot and press Blink: if the alignment is right the LaserDisc and DVD pictures "
                           "show the same moment and only the colours/sharpness change; a jump means a shift."}


def _norm_luma(rgb: np.ndarray) -> np.ndarray:
    """Luma stretched to the same mean/spread, so a brightness difference between the transfers is not 'unlike'."""
    g = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    s = float(g.std())
    return (g - g.mean()) * (48.0 / s if s > 1e-3 else 1.0) + 128.0


def render_note(path, text: str) -> None:
    """A small PNG saying why the sheet could not be built (keeps the declared evidence file present)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 90), (24, 24, 24))
    ImageDraw.Draw(img).text((10, 10), text[:200], fill=(240, 190, 80))
    img.save(path, format="PNG")
