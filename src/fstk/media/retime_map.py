"""DVD → LD output timeline for retiming the DVD 5.1 and subtitles (audio-precise).

v38 placed DVD material on the LD timeline from the picture alignment path and adjudicated act
breaks, and stretched each segment with ``atempo``. This version works in **LD audio time**,
which is the output's own clock (``t = I + S · wav − delta``, from ``dvd-ld-audio``):

* **Segments from audio.** Every window that ``dvd-ld-audio`` matched gives a pair (LD WAV
  centre, DVD centre). Within a stretch of identical programme, ``dvd − β·wav`` is constant.
  Every edit between the two editions (act breaks, and also 3-frame trims mid-act: S01E20 has one
  near DVD 295 s that the picture model missed) is a step of it. Steps > 15 ms, confirmed by the
  next window, split segments; unconfirmed single windows are dropped.
* **Edits located by a fine scan.** Between the last window of one segment and the first of the
  next, short LD windows (3 s every 0.25 s) are matched where each segment's line predicts. The
  switch point is where the second line starts to explain the audio better (ties → the quietest
  LD point).
* **Nothing reused, nothing silent mid-scene.** DVD-only material at an edit is dropped.
  LD-only material is a *fill* span: the 5.1 is filled there from the time-aligned LD audio, and
  subtitles have no DVD cue in it.

The picture act curves are only used upstream to predict where to search.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

FPS = 30000.0 / 1001.0
JUMP_MS = 15.0
SCAN_WINDOW_S = 3.0
SCAN_STEP_S = 0.25
SCAN_SEARCH_S = 0.05
MIN_CORR = 0.6


@dataclass
class Line:
    """dvd = alpha + beta · wav over [wav_start, wav_end] (LD WAV seconds)."""
    wav_start: float
    wav_end: float
    alpha: float
    beta: float
    windows: int
    residual_ms: float

    def dvd(self, wav: float) -> float:
        return self.alpha + self.beta * wav

    def wav(self, dvd: float) -> float:
        return (dvd - self.alpha) / self.beta


@dataclass
class Piece:
    """One stretch of the output: DVD material (``source="dvd"``) or LD fill (``source="ld"``)."""
    source: str
    out_start_s: float
    out_end_s: float
    dvd_start_s: float | None = None
    dvd_end_s: float | None = None

    @property
    def ratio(self) -> float:
        """DVD seconds per output second (≈ 0.999 for 1000:1001)."""
        return (self.dvd_end_s - self.dvd_start_s) / (self.out_end_s - self.out_start_s)


@dataclass
class RetimeMap:
    clock_intercept_s: float
    clock_slope: float
    delta_s: float
    total_s: float
    lines: list[Line]
    pieces: list[Piece]
    switches: list[dict]

    def out_of_wav(self, wav: float) -> float:
        return self.clock_intercept_s + self.clock_slope * wav - self.delta_s

    def wav_of_out(self, t: float) -> float:
        return (t + self.delta_s - self.clock_intercept_s) / self.clock_slope

    def dvd_to_out(self, d: float) -> float | None:
        for p in self.pieces:
            if p.source == "dvd" and p.dvd_start_s <= d <= p.dvd_end_s:
                return p.out_start_s + (d - p.dvd_start_s) / p.ratio
        return None

    def out_to_dvd(self, t: float) -> float | None:
        for p in self.pieces:
            if p.source == "dvd" and p.out_start_s <= t <= p.out_end_s:
                return p.dvd_start_s + (t - p.out_start_s) * p.ratio
        return None

    def summary(self) -> dict:
        return {"clock_intercept_s": self.clock_intercept_s, "clock_slope": self.clock_slope, "delta_s": self.delta_s,
                "total_s": self.total_s, "lines": [asdict(x) for x in self.lines],
                "pieces": [asdict(p) | ({"ratio": p.ratio} if p.source == "dvd" else {}) for p in self.pieces],
                "switches": self.switches,
                "dvd_seconds": sum(p.out_end_s - p.out_start_s for p in self.pieces if p.source == "dvd"),
                "ld_fill_seconds": sum(p.out_end_s - p.out_start_s for p in self.pieces if p.source == "ld")}


def _fit(x: np.ndarray, y: np.ndarray, beta: float | None = None) -> tuple[float, float, float]:
    w = np.ones_like(x)
    alpha, b = 0.0, beta if beta is not None else 1.0
    for _ in range(20):
        if beta is None and len(x) >= 4:
            A = np.vstack([np.ones_like(x), x]).T
            alpha, b = np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0]
        else:
            alpha = float(np.sum(w * (y - b * x)) / np.sum(w))
        r = y - (alpha + b * x)
        scale = 1.4826 * float(np.median(np.abs(r))) + 1e-5
        w = 1.0 / np.maximum(1.0, np.abs(r) / (2 * scale))
    r = y - (alpha + b * x)
    return float(alpha), float(b), float(1000 * 1.4826 * np.median(np.abs(r)))


def lines_from_windows(windows: list[dict]) -> list[Line]:
    """Split matched windows into constant-relation segments (steps > JUMP_MS, confirmed)."""
    pts = sorted((w["wav_centre_s"], w["dvd_centre_s"]) for w in windows
                 if w.get("dvd_centre_s") is not None and w.get("corr", 0) >= MIN_CORR)
    if len(pts) < 3:
        raise ValueError("too few matched windows to retime")
    x, y = np.array(pts).T
    slopes = np.diff(y) / np.diff(x)
    beta0 = float(np.median(slopes[np.isfinite(slopes)]))
    u = (y - beta0 * x) * 1000.0
    groups: list[list[int]] = [[0]]
    for i in range(1, len(u)):
        level = float(np.median(u[groups[-1][-3:]]))
        if abs(u[i] - level) <= JUMP_MS:
            groups[-1].append(i)
        elif i + 1 < len(u) and abs(u[i + 1] - u[i]) <= JUMP_MS:
            groups.append([i])               # confirmed step: a new segment
        # else: a lone window off both levels (straddles an edit) — dropped
    lines = []
    for g in groups:
        gx, gy = x[g], y[g]
        alpha, beta, res = _fit(gx, gy, None if len(g) >= 4 else beta0)
        lines.append(Line(float(gx[0]), float(gx[-1]), alpha, beta, len(g), res))
    return lines


def _match(ld_env, dvd_env, hop: float, wav_c: float, dvd_c: float) -> float:
    n = round(SCAN_WINDOW_S / hop)
    a = round((wav_c - SCAN_WINDOW_S / 2) / hop)
    x = ld_env[a:a + n]
    lo = round((dvd_c - SCAN_WINDOW_S / 2 - SCAN_SEARCH_S) / hop)
    y = dvd_env[max(lo, 0):max(lo, 0) + n + int(2 * SCAN_SEARCH_S / hop)]
    if len(x) < n or len(y) < n or np.std(x) < 1e-3:
        return float("nan")
    from fstk.media.dvd_audio import slide_corr

    return float(np.max(slide_corr(x, y)))


def _lag(ld_env, dvd_env, hop: float, wav_c: float, dvd_c: float, search: float) -> tuple[float, float]:
    """(DVD lag in seconds from ``dvd_c`` that best matches the LD window at ``wav_c``, its correlation)."""
    n = round(SCAN_WINDOW_S / hop)
    a = round((wav_c - SCAN_WINDOW_S / 2) / hop)
    lo = round((dvd_c - SCAN_WINDOW_S / 2 - search) / hop)
    x = ld_env[max(a, 0):max(a, 0) + n]
    y = dvd_env[lo:lo + n + round(2 * search / hop)] if lo >= 0 else np.empty(0)
    if a < 0 or len(x) < n or len(y) < n + round(2 * search / hop) or np.std(x) < 1e-3:
        return float("nan"), float("nan")
    from fstk.media.dvd_audio import _parabolic, slide_corr

    sc = slide_corr(x, y)
    k = int(np.argmax(sc))
    return (k + _parabolic(sc, k)) * hop - search, float(sc[k])


def locate_switch(a: Line, b: Line, ld_env: np.ndarray, dvd_env: np.ndarray, hop: float) -> tuple[float, dict]:
    """LD WAV time where line ``b`` takes over from line ``a`` (fine scan between their windows).

    A step no larger than the match's own search (``SCAN_SEARCH_S`` each way) cannot be told apart by "which line
    matches better": each match slides onto the best lag and both score the same. For those the scan measures the
    lag itself and asks which line predicts it (S02E02: a one-frame, 33 ms step at 31:03 was placed ~10 s early by
    the score scan, "clear": false, and the retimed audio sat 30 ms late until the real edit)."""
    lo, hi = a.wav_end - SCAN_WINDOW_S, b.wav_start + SCAN_WINDOW_S
    cs = np.arange(lo, hi + 1e-9, SCAN_STEP_S)
    mid = (a.wav_end + b.wav_start) / 2
    step = b.dvd(mid) - a.dvd(mid)
    method = "score"
    if abs(step) <= 2 * SCAN_SEARCH_S:
        method = "lag"
        z = np.zeros(len(cs))
        for i, c in enumerate(cs):
            lag, corr = _lag(ld_env, dvd_env, hop, c, a.dvd(c), abs(step) + SCAN_SEARCH_S)
            if np.isfinite(lag) and corr >= MIN_CORR:
                z[i] = (abs(lag) - abs(lag - step)) / max(abs(step), hop) * corr   # >0: b's lag, <0: a's
    else:
        za = np.array([_match(ld_env, dvd_env, hop, c, a.dvd(c)) for c in cs])
        zb = np.array([_match(ld_env, dvd_env, hop, c, b.dvd(c)) for c in cs])
        z = np.nan_to_num(zb - za)
    # best split: before it a explains better, after it b does
    cost = [np.sum(np.maximum(z[:k], 0)) + np.sum(np.maximum(-z[k:], 0)) for k in range(len(z) + 1)]
    k = int(np.argmin(cost))
    best = [i for i, c in enumerate(cost) if c <= cost[k] + 1e-9]
    if len(best) > 1:                       # a flat stretch (silence/black): cut at the quietest LD point
        qs = []
        for i in best:
            c = cs[min(i, len(cs) - 1)]
            j = round(c / hop)
            qs.append(float(np.mean(ld_env[max(j - 4, 0):j + 4])) if j < len(ld_env) else 1e9)
        k = best[int(np.argmin(qs))]
    w = float(cs[min(k, len(cs) - 1)]) if k < len(cs) else float(hi)
    w = min(max(w, a.wav_end - SCAN_WINDOW_S / 2), b.wav_start + SCAN_WINDOW_S / 2)
    if method == "lag":
        total = float(np.sum(np.abs(z)))
        clear = bool(total > 0 and cost[k] <= 0.2 * total and np.any(z[:k] < 0) and np.any(z[k:] > 0))
    else:
        clear = bool(np.nanmax(np.abs(z)) > 0.05) if len(z) else False
    return w, {"scan_points": len(cs), "clear": clear, "method": method}


def build_map(windows: list[dict], *, intercept: float, slope: float, delta_s: float, total_s: float,
              ld_env: np.ndarray, dvd_env: np.ndarray, env_hop: float, dvd_duration_s: float) -> RetimeMap:
    """Pieces covering the output [0, total_s]: DVD material where the editions match, LD fill elsewhere."""
    lines = lines_from_windows(windows)

    def out_of(w: float) -> float:
        return intercept + slope * w - delta_s

    def wav_of(t: float) -> float:
        return (t + delta_s - intercept) / slope

    pieces: list[Piece] = []
    switches: list[dict] = []
    w_cursor = wav_of(0.0)
    w_end_total = wav_of(total_s)

    def add_dvd(line: Line, w0: float, w1: float) -> None:
        d0, d1 = line.dvd(w0), line.dvd(w1)
        if d0 < 0:                           # before the DVD starts
            wz = line.wav(0.0)
            add_fill(w0, min(wz, w1))
            w0, d0 = max(w0, wz), 0.0
        if d1 > dvd_duration_s:              # after the DVD ends
            we = line.wav(dvd_duration_s)
            if we > w0:
                pieces.append(Piece("dvd", out_of(w0), out_of(we), d0, dvd_duration_s))
            add_fill(max(w0, we), w1)
            return
        if w1 > w0:
            pieces.append(Piece("dvd", out_of(w0), out_of(w1), d0, d1))

    def add_fill(w0: float, w1: float) -> None:
        if w1 > w0 + 1e-6:
            if pieces and pieces[-1].source == "ld" and abs(pieces[-1].out_end_s - out_of(w0)) < 1e-9:
                pieces[-1].out_end_s = out_of(w1)
            else:
                pieces.append(Piece("ld", out_of(w0), out_of(w1)))

    for i, line in enumerate(lines):
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            w_sw, info = locate_switch(line, nxt, ld_env, dvd_env, env_hop)
            w_sw = min(max(w_sw, w_cursor), w_end_total)
            add_dvd(line, w_cursor, w_sw)
            d_end = line.dvd(w_sw)
            w_next = max(w_sw, nxt.wav(d_end))            # never reuse DVD material
            dropped = max(0.0, nxt.dvd(w_sw) - d_end)     # DVD-only material at this edit
            add_fill(w_sw, min(w_next, w_end_total))
            switches.append({"out_s": round(out_of(w_sw), 4), "wav_s": round(w_sw, 4),
                             "ld_only_s": round(w_next - w_sw, 4), "dvd_only_s": round(dropped, 4),
                             "step_ms": round(1000 * (nxt.dvd(w_sw) - d_end), 2), **info})
            w_cursor = min(w_next, w_end_total)
        else:
            add_dvd(line, w_cursor, w_end_total)
    pieces = [p for p in pieces if p.out_end_s - p.out_start_s > 1e-6]
    if pieces:
        pieces[0].out_start_s = max(pieces[0].out_start_s, 0.0)
        pieces[-1].out_end_s = min(pieces[-1].out_end_s, total_s)
    return RetimeMap(intercept, slope, delta_s, total_s, lines, pieces, switches)


# ------------------------------------------------------------------------------ subtitles
def parse_srt(text: str) -> list[tuple[float, float, list[str]]]:
    import re

    def sec(v: str) -> float:
        h, m, rest = v.strip().split(":")
        s, ms = rest.replace(".", ",").split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.splitlines()
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->", 1)
        cues.append((sec(a), sec(b.split()[0]), lines[idx + 1:]))
    return cues


def fmt_srt(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def retime_srt(text: str, rmap: RetimeMap) -> tuple[str, list[dict]]:
    """Map every cue onto the output; cues whose DVD material is not on the LD are reported, not hidden."""
    out, dropped = [], []
    for start, end, lines in parse_srt(text):
        a, b = rmap.dvd_to_out(start), rmap.dvd_to_out(end)
        if a is None and b is None:
            dropped.append({"dvd_start_s": start, "dvd_end_s": end, "text": " / ".join(lines)})
            continue
        dvd_pieces = [p for p in rmap.pieces if p.source == "dvd"]
        if a is None:   # starts in dropped DVD material: begin where the material resumes
            a = next((p.out_start_s for p in dvd_pieces if p.dvd_start_s >= start), rmap.total_s)
        if b is None:
            b = max((p.out_end_s for p in dvd_pieces if p.dvd_end_s <= end), default=0.0)
        a, b = max(0.0, a), min(rmap.total_s, b)
        if b - a < 0.05:
            dropped.append({"dvd_start_s": start, "dvd_end_s": end, "text": " / ".join(lines)})
            continue
        out.append(f"{len(out) + 1}\n{fmt_srt(a)} --> {fmt_srt(b)}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n", dropped
