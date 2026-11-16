#!/usr/bin/env python3
"""Fade-midpoint LD audio-rate fit (the runbook's named primary method).

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

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ldcommon import (Ld, PipelineError, assert_writable_path, load_episode,
                      redact_shared_text)  # noqa: E402

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


def run_stderr(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    # Keep the complete detector output: this pipeline has known FFmpeg false
    # nonzero exits when artifacts are complete. Callers/report reviewers must
    # validate parsed evidence rather than treating the return code alone as a fit.
    return p.stderr


def detect_blacks(cmd: list[str], minimum: float) -> list[tuple[float, float, float]]:
    out = []
    for line in run_stderr(cmd).splitlines():
        m = BLACK_RE.search(line)
        if m:
            s, e, d = float(m.group(1)), float(m.group(2)), float(m.group(3))
            if d >= minimum:
                out.append((s, e, d))
    return out


def detect_silences(cmd: list[str], minimum: float) -> list[tuple[float, float, float]]:
    out, start = [], None
    for line in run_stderr(cmd).splitlines():
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
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
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
                  for x, y, r in zip(xs, ys, resid)],
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
    for d, m, wm in cands:
        if m in used_b or wm in used_w:
            continue
        used_b.add(m)
        used_w.add(wm)
        pairs.append((wm, m))     # x = WAV time, y = black time
    return sorted(pairs, key=lambda p: p[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True, help="manifest code, e.g. S01E12")
    ap.add_argument("--master-min-t", type=float,
                    help="skip master black midpoints before this; default = the "
                         "measured video.delta_s, which marks the DVD-origin black and "
                         "therefore the end of LD-only front matter")
    ap.add_argument("--pair-window", type=float, default=PAIR_WINDOW)
    ap.add_argument("--skip-dvd", action="store_true",
                    help="skip pairing B (DVD blackdetect) for a faster run")
    ap.add_argument("--out", help="override the JSON report path")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)

    master = ep.path("stack_master")
    dvd = ep.optional_path("dvd")
    wav = ep.path("ld_native")
    delta_s = (ep.data.get("video") or {}).get("delta_s")
    min_t = args.master_min_t
    if min_t is None:
        if delta_s is None:
            raise PipelineError(
                "video.delta_s is not measured, so the LD-only front-matter boundary is "
                "unknown; measure it first or pass --master-min-t")
        min_t = float(delta_s)
    for p, what in ((master, "stack_master"), (wav, "ld_native")):
        if not p.exists():
            raise PipelineError(f"{what} missing: {p}")

    out = Path(args.out) if args.out else ld.probe() / f"{ep.code}-fade-midpoint-fit.json"
    out = assert_writable_path(out, ld)
    if out.exists():
        raise PipelineError(f"refusing to overwrite existing report: {out}")

    ff, fp = Path(ld.tool("ffmpeg")), Path(ld.tool("ffprobe"))
    blackvf = f"blackdetect=d=0:pix_th={BLACK_PX_TH}:pic_th={BLACK_PIC_TH}"
    silaf = f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_DUR}"

    print(f"episode            : {ep.code}")
    print(f"master             : {redact_shared_text(str(master), ld)}")
    print(f"LD WAV (native)    : {redact_shared_text(str(wav), ld)}")
    print(f"DVD                : {redact_shared_text(str(dvd), ld) if dvd else '(none)'}")
    print(f"silencedetect      : noise={SILENCE_NOISE} d={SILENCE_DUR} min={MIN_SIL}s")
    print(f"blackdetect        : {blackvf} min={MIN_BLK}s")
    print(f"front-matter bound : {min_t:.6f} s (video.delta_s={delta_s})")
    print(f"pair window        : {args.pair_window} s")
    print("units/sign         : x = native 44100 Hz WAV seconds, y = master seconds at "
          "30000/1001; b>1 means the WAV runs slow (asetrate < 44100)")

    print("\n[1/3] master blackdetect (sequential, full decode)", flush=True)
    mblk = detect_blacks([str(ff), "-hide_banner", "-v", "info", "-nostats",
                          "-i", str(master), "-vf", blackvf, "-an", "-f", "null", "-"],
                         MIN_BLK)
    print(f"  {len(mblk)} blacks >= {MIN_BLK}s")

    print("[2/3] LD WAV silencedetect", flush=True)
    wsil = detect_silences([str(ff), "-hide_banner", "-v", "info", "-nostats",
                            "-i", str(wav), "-af", silaf, "-f", "null", "-"], MIN_SIL)
    print(f"  {len(wsil)} silences >= {MIN_SIL}s")

    duration_probe = subprocess.run([str(fp), "-v", "error", "-show_entries",
                                     "format=duration", "-of", "csv=p=0", str(wav)],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace")
    if duration_probe.returncode:
        raise PipelineError(f"ffprobe failed reading WAV duration: {duration_probe.stderr[-500:]}")
    try:
        wav_dur = float(duration_probe.stdout.strip())
    except ValueError as exc:
        raise PipelineError(f"ffprobe returned an invalid WAV duration: {duration_probe.stdout!r}") from exc
    print(f"  WAV duration: {wav_dur:.6f} s (silences running into EOF are excluded)")

    dblk: list[tuple[float, float, float]] = []
    if not args.skip_dvd and dvd is not None:
        print("[3/3] DVD video blackdetect (sequential; DVD PTS may be non-monotonic)",
              flush=True)
        dblk = detect_blacks([str(ff), "-hide_banner", "-v", "info", "-nostats",
                              "-i", str(dvd), "-vf", blackvf, "-an", "-f", "null", "-"],
                             MIN_BLK)
        print(f"  {len(dblk)} blacks >= {MIN_BLK}s")
    else:
        print("[3/3] DVD blackdetect skipped")

    # Representative raw rows before any aggregate (runbook one-off script check).
    print("\nraw master blacks (midpoint >= bound are eligible):")
    for s, e, d in mblk:
        m = (s + e) / 2.0
        print(f"   {s:9.4f}-{e:9.4f} dur {d:7.4f}  mid {m:9.4f}  "
              f"{'ELIGIBLE' if m >= min_t else 'skipped (LD-only head)'}")
    print("raw WAV silences (midpoint):")
    for s, e, d in wsil:
        trunc = " TRUNCATED-at-EOF" if e >= wav_dur - 0.05 else ""
        print(f"   {s:9.4f}-{e:9.4f} dur {d:7.4f}  mid {(s+e)/2:9.4f}{trunc}")

    pairsA = pair_up(mblk, wsil, min_t, wav_dur, args.pair_window)
    pairsB = pair_up(dblk, wsil, 0.5, wav_dur, args.pair_window) if dblk else []

    print(f"\npaired: (A) {len(pairsA)} master<->WAV, (B) {len(pairsB)} DVD<->WAV")
    resA = fit(pairsA, "A: master-video fade midpoint vs LD WAV silence midpoint")
    resB = fit(pairsB, "B: DVD-video fade midpoint vs LD WAV silence midpoint")

    for r in (resA, resB):
        if "error" in r:
            print(f"  {r['label']}: {r['error']}")
            continue
        print(f"\n  {r['label']}")
        print(f"    n={r['n']}  slope b={r['slope_b']:.9f} +/- {r['slope_stderr_b']:.9f}  "
              f"a={r['intercept_a']:.4f} s  resid SD={r['residual_sd_s']:.4f} s")
        print(f"    Theil-Sen b={r['theil_sen_b']:.9f}")
        print(f"    rate delta = {r['rate_delta_ppm']:+.1f} +/- "
              f"{r['rate_delta_stderr_ppm']:.1f} ppm")
        print(f"    asetrate   = {r['asetrate_from_b']:.2f} +/- "
              f"{r['asetrate_stderr_hz']:.2f} Hz   "
              f"(Theil-Sen {r['asetrate_from_theil_sen']:.2f} Hz)")
        print("    pairs (wav_s -> black_s):")
        for p in r["pairs"]:
            print(f"      {p['wav_s']:9.3f} -> {p['y_s']:9.3f}   "
                  f"resid {p['resid_s']:+7.3f} s")

    doc = {
        "schema": "fade-midpoint-fit-v1",
        "workflow_version": (Path(__file__).resolve().parent.parent
                             / "WORKFLOW_VERSION").read_text(encoding="utf-8").strip(),
        "episode": ep.code,
        "method": "fade-midpoint fit (runbook primary)",
        "units_sign_convention": (
            "x = native 44100 Hz LD WAV seconds; y = master seconds at 30000/1001 (fit A) "
            "or DVD seconds (fit B); asetrate = 44100/b; b>1 means the WAV runs slow"),
        "silencedetect": {"noise": SILENCE_NOISE, "d": SILENCE_DUR, "min_s": MIN_SIL},
        "blackdetect": {"pix_th": BLACK_PX_TH, "pic_th": BLACK_PIC_TH, "min_s": MIN_BLK},
        "pair_window_s": args.pair_window,
        "front_matter_bound_s": min_t,
        "front_matter_bound_source": "video.delta_s" if args.master_min_t is None else "cli",
        "video_delta_s": delta_s,
        "native_wav": str(wav),
        "native_wav_duration_s": wav_dur,
        "master": str(master),
        "dvd": str(dvd) if dvd else None,
        "master_blacks": [{"start": s, "end": e, "dur": d} for s, e, d in mblk],
        "wav_silences": [{"start": s, "end": e, "dur": d} for s, e, d in wsil],
        "dvd_blacks": [{"start": s, "end": e, "dur": d} for s, e, d in dblk],
        "fit_A_master_vs_wav": resA,
        "fit_B_dvd_vs_wav": resB,
        "caveat": ("report-only: selects no rate, approves nothing, edits no manifest. "
                   "Prefer Theil-Sen over OLS here -- act-break fade midpoints form a "
                   "staircase (seconds of structural variation against ~2.5 s of rate "
                   "signal across 2500 s), which drags OLS."),
    }
    write_report(out, doc, ld)
    print(f"\nreport: {redact_shared_text(str(out), ld)}")
    return 0


def write_report(out: Path, document: dict, ld: Ld) -> Path:
    out = assert_writable_path(out, ld)
    encoded = redact_shared_text(json.dumps(document, indent=2), ld)
    try:
        json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"shared-path redaction produced invalid JSON: {exc}") from exc
    try:
        with out.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
    except FileExistsError as exc:
        raise PipelineError(f"refusing to overwrite existing report: {out}") from exc
    return out


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
