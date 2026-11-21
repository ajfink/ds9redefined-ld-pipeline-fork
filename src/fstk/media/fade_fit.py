"""Fade-midpoint LD audio-rate fit (pure math + log parsing).

Ported from v38 ``scripts/fade_midpoint_fit.py``. ``fit`` and ``pair_up`` are verbatim; the
detectors are split into pure parsers (``parse_blacks``/``parse_silences``) over ffmpeg's
blackdetect/silencedetect log text, so this module never runs a process. Stages run the
detectors through ``fstk.tools.process`` and keep the complete logs as evidence.

Original method notes (v38):

    Fade-midpoint LD audio-rate fit (the runbook's named primary method).

    Reusable, episode-parameterized replacement for E08's one-off
    ``fade-midpoint-fit-s01e08.py``. That script hard-coded one episode's paths and one
    episode's front-matter boundary, so per the job prompt it must not be reused as-is.

    Method
    ------
    Pair act-break FADE MIDPOINTS between two independent sources and fit

        master_time = a + b * wav_time        =>   asetrate = 44100 / b

    (the WAV must be stretched by ``b`` to land on the master timeline -- the same
    convention ``measure_audio_rate`` uses for ``candidate = 44100/slope``).

    Two independent pairings are computed:

      (A) master video ``blackdetect`` vs LD WAV ``silencedetect`` -> LD audio vs LD video
      (B) DVD video ``blackdetect``      vs LD WAV ``silencedetect`` -> LD audio vs DVD video

    Neither uses the picture map nor the stack/WAV duration model, so both are free of the
    PAD-frame degeneracy that makes duration-only estimates ambiguous.

    Sign/units convention (state it before trusting any output): x = LD WAV seconds at the
    NATIVE nominal 44100 Hz clock (never the corrected WAV), y = master seconds at
    30000/1001. ``b > 1`` means the WAV runs slow relative to the master, i.e. the audio
    clock must be SLOWED (asetrate < 44100).

    Only the *detection* parameters are module constants; every episode-specific value
    (front-matter boundary, paths, output) comes from the manifest. OLS is reported but
    Theil-Sen is the statistic to prefer: inside an act break the video-black and
    audio-silence midpoints carry seconds of structural variation (inserted LD black/PAD
    frames, asymmetric fades), which forms a staircase that pulls OLS. This tool reports
    both and selects nothing -- approving ``asetrate`` remains a separate, reasoned step.

    REPORT-ONLY: never writes audio and never edits the episode manifest.
"""
from __future__ import annotations

import re
import statistics

# Detection parameters -- method constants, identical across episodes so results are
# comparable. E08 measured that -45 dB yields only 3 break pairs, -40 dB yields 7, and
# -35 dB yields 102 (dialogue gaps), so -40 dB is the recorded choice.
SILENCE_NOISE = "-40dB"
SILENCE_DUR = "0.5"
BLACK_PX_TH = "0.10"
BLACK_PIC_TH = "0.98"
MIN_SIL = 0.4          # ignore sub-0.4 s gaps
MIN_BLK = 0.4          # ignore 1-frame PAD blips / flashes
PAIR_WINDOW = 6.0      # seconds; midpoints further apart than this are not paired

BLACK_RE = re.compile(
    r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)")
SIL_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SIL_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")




def parse_blacks(text: str, minimum: float) -> list[tuple[float, float, float]]:
    """``blackdetect`` lines → (start, end, duration) with duration ≥ minimum."""
    out = []
    for line in text.splitlines():
        m = BLACK_RE.search(line)
        if m:
            s, e, d = float(m.group(1)), float(m.group(2)), float(m.group(3))
            if d >= minimum:
                out.append((s, e, d))
    return out


def parse_silences(text: str, minimum: float) -> list[tuple[float, float, float]]:
    """``silencedetect`` start/end pairs → (start, end, duration) with duration ≥ minimum."""
    out, start = [], None
    for line in text.splitlines():
        m = SIL_START_RE.search(line)
        if m:
            start = float(m.group(1))
        m = SIL_END_RE.search(line)
        if m and start is not None:
            e, d = float(m.group(1)), float(m.group(2))
            if d >= minimum:
                out.append((start, e, d))
            start = None
    return out


def blackdetect_filter() -> str:
    # v38 detects every black (d=0) and applies MIN_BLK while parsing.
    return f"blackdetect=d=0:pix_th={BLACK_PX_TH}:pic_th={BLACK_PIC_TH}"


def silencedetect_filter() -> str:
    return f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_DUR}"


def fit(pairs: list[tuple[float, float]], label: str) -> dict:
    """OLS + Theil-Sen of y (master/DVD s) on x (LD WAV s)."""
    if len(pairs) < 3:
        return {"label": label, "n": len(pairs), "error": "too few pairs"}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return {"label": label, "n": n, "error": "degenerate WAV times; slope is undefined"}
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    b = sxy / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys, strict=True)]
    sd = (sum(r * r for r in resid) / (n - 2)) ** 0.5
    se_b = sd / (sxx ** 0.5) if sxx > 0 else float("nan")
    slopes = sorted((ys[j] - ys[i]) / (xs[j] - xs[i])
                    for i in range(n) for j in range(i + 1, n) if xs[j] != xs[i])
    if not slopes:
        return {"label": label, "n": n, "error": "no distinct WAV times for Theil-Sen slopes"}
    ts = statistics.median(slopes)
    return {
        "label": label, "n": n,
        "slope_b": b, "slope_stderr_b": se_b,
        "intercept_a": a, "residual_sd_s": sd,
        "theil_sen_b": ts,
        "rate_delta_ppm": (b - 1.0) * 1e6,
        "rate_delta_stderr_ppm": se_b * 1e6,
        "asetrate_from_b": 44100.0 / b,
        "asetrate_stderr_hz": 44100.0 / (b * b) * se_b,
        "asetrate_from_theil_sen": 44100.0 / ts,
        "pairs": [{"wav_s": x, "y_s": y, "resid_s": r}
                  for x, y, r in zip(xs, ys, resid, strict=True)],
    }


def pair_up(blacks, silences, min_mid, wav_dur, window):
    """Greedy one-to-one pairing by smallest midpoint distance within `window`.

    Silences running into EOF are skipped: their midpoint is truncated and biased.
    Returns (x_wav_seconds, y_black_seconds) pairs.
    """
    cands = []
    for b in blacks:
        m = (b[0] + b[1]) / 2.0
        if m < min_mid:
            continue
        for w in silences:
            if w[1] >= wav_dur - 0.05:
                continue
            wm = (w[0] + w[1]) / 2.0
            d = abs(wm - m)
            if d <= window:
                cands.append((d, m, wm))
    cands.sort(key=lambda t: t[0])
    used_b, used_w, pairs = set(), set(), []
    for _d, m, wm in cands:
        if m in used_b or wm in used_w:
            continue
        used_b.add(m)
        used_w.add(wm)
        pairs.append((wm, m))     # x = WAV time, y = black time
    return sorted(pairs, key=lambda p: p[0])
