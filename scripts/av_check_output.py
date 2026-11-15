#!/usr/bin/env python3
r"""Absolute A/V gate for a delivered episode: LD stereo vs the DVD, referenced
through the episode's own picture map.

For an output window at time t:
    D_v = (DELTA + t) + off(master)/FPS     picture-map DVD time of the frame
    D_a = t + k                             measured DVD-audio match
    A/V = D_a - D_v = k - DELTA - off/FPS   positive = audio content later (LATE)

The offset model is rebuilt from episodes\<code>.json breaks + the cached
alignment path, fitting only content windows >=2 s clear of every fade; music-
through blacks listed in retime.merged_music_through are excluded from fitting.

  python scripts\\av_check_output.py --episode S01E05
  python scripts\\av_check_output.py --episode S01E05 --video <file>
  python scripts\\av_check_output.py --episode S01E07 --video <picture-reference> --audio <candidate-ld.flac> --out <candidate-report.tsv>

Gates (runbook section 10): |constant| <= 100 ms, |slope| <= 30 ppm, rms <= 40 ms.
Writes <code>-av-check.tsv for the measured file.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402

FPS = 30000.0 / 1001.0
SR = 16000
HOP = 0.02
WIN = 60.0
EDGE_CLEAR_S = 2.0
DEFAULT_POINTS = (100.0, 500.0, 900.0, 1400.0, 1900.0, 2400.0, 2600.0)
GATE_CONST_MS, GATE_SLOPE_PPM, GATE_RMS_MS = 100.0, 30.0, 40.0


def decode(ld: Ld, path: Path, selector: str):
    import numpy as np
    cmd = [ld.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-map", selector, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode:
        raise PipelineError(f"decode failed {path} {selector}: "
                            + p.stderr.decode("utf-8", "replace")[:800])
    return np.frombuffer(p.stdout, np.int16).astype(np.float32)


def envelope(x, start_s, len_s):
    import numpy as np
    n = int(SR * HOP)
    seg = x[int(start_s * SR):int(start_s * SR) + int(len_s * SR)]
    m = len(seg) // n
    if m < 10:
        raise PipelineError(f"window at {start_s}s is too short ({len(seg)} samples)")
    return np.sqrt((seg[: m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)


def corr_peak(a, b):
    """Normalised correlation of a against every aligned window of b."""
    import numpy as np
    a = (a - a.mean()) / a.std()
    b = (b - b.mean()) / (b.std() + 1e-12)
    n, m = len(a), len(b)
    nfft = 1 << (n + m - 2).bit_length()
    conv = np.fft.irfft(np.fft.rfft(b, nfft) * np.fft.rfft(a[::-1], nfft), nfft)
    dot = conv[n - 1:n - 1 + (m - n + 1)]
    cum = np.concatenate(([0.0], np.cumsum(b)))
    cum2 = np.concatenate(([0.0], np.cumsum(b * b)))
    s = cum[n:] - cum[:-n]
    e = cum2[n:] - cum2[:-n] - s * s / n
    return dot / np.sqrt((a * a).sum() * np.maximum(e, 1e-9))


def fit_windows(ep) -> list[tuple[float, float]]:
    """Content windows for the offset model, built from the manifest.

    Windows are the spans between fades, each pulled back by EDGE_CLEAR_S from
    both sides. Windows shorter than MIN_SPAN_S are dropped: a short span fits
    noise into a huge bogus slope that would poison any extrapolation.
    """
    MIN_SPAN_S = 30.0
    blacks = sorted(
        [tuple(b["ld_black"]) for b in (ep.data.get("retime", {}).get("breaks") or [])
         if b.get("ld_black")]
        + [tuple(m["ld_black"]) for m in (ep.data.get("retime", {}).get("merged_music_through") or [])
           if m.get("ld_black")])
    # optional: the episode's final fade-to-black, which would otherwise leak
    # all-black samples (meaningless offsets) into the tail fit
    tail = (ep.data.get("retime") or {}).get("end_black_ld")
    if tail:
        blacks.append(tuple(tail))
    blacks.sort()
    if not blacks:
        raise PipelineError(f"{ep.code}: manifest has no retime.breaks ld_black entries")
    end = ep.stack_frames / FPS
    if tail:
        end = min(end, float(tail[0]))
    wins, cursor = [], 0.0
    for s, e in blacks:
        if s - EDGE_CLEAR_S > cursor + 5:
            wins.append((cursor, s - EDGE_CLEAR_S))
        cursor = max(cursor, e + EDGE_CLEAR_S)
    if end - cursor > 5:
        wins.append((cursor, end))
    kept = [w for w in wins if w[1] - w[0] >= MIN_SPAN_S]
    dropped = [w for w in wins if w[1] - w[0] < MIN_SPAN_S]
    if dropped:
        print(f"  ignoring {len(dropped)} window(s) shorter than {MIN_SPAN_S:.0f}s: "
              + ", ".join(f"{a:.1f}-{b:.1f}" for a, b in dropped))
    return kept


def require_alignment_sample_coverage(sampled_start: float, sampled_end: float,
                                      master_time: float) -> None:
    """Prevent a head/tail or QC query from silently extrapolating an alignment path."""
    if not sampled_start <= master_time <= sampled_end:
        raise PipelineError(
            f"requested master time {master_time:.3f}s is outside the sampled alignment range "
            f"{sampled_start:.3f}..{sampled_end:.3f}s; rerun measure_dvd_alignment.py "
            "with --first-master-frame covering this anchor or obtain an independent local match")


def build_offset_model(ep, align_path: Path):
    import numpy as np
    if not align_path.exists():
        raise PipelineError(
            f"alignment path missing: {align_path} - generate the candidate path with "
            "extract_video_descriptors.py and measure_dvd_alignment.py first")
    a = np.load(align_path)
    t, o = a[:, 0] / FPS, a[:, 1].astype(float)
    sampled_start, sampled_end = float(t.min()), float(t.max())
    o_s = o.copy()
    for k in range(2, len(o_s) - 2):
        o_s[k] = np.median(o[k - 2:k + 3])
    wins = fit_windows(ep)
    fits = []
    for lo, hi in wins:
        m = (t >= lo) & (t <= hi)
        if m.sum() < 20:
            continue
        slope, icept = np.polyfit(t[m], o_s[m], 1)
        fits.append((lo, hi, float(slope), float(icept)))
    if not fits:
        raise PipelineError("no content window had enough samples to fit an offset model")
    print("offset model (content-only windows):")
    print(f"  sampled alignment master range: {sampled_start:.3f}..{sampled_end:.3f}s")
    for lo, hi, sl, ic in fits:
        print(f"  {lo:8.1f}-{hi:8.1f}s  rate {sl/FPS*1e6:+7.1f} ppm  "
              f"offset({lo:.0f})={sl*lo+ic:+.1f}")

    def off(m: float) -> float:
        """Offset at master time m: the containing window, else the nearest one.

        Fade clearance and the deliberate gaps around long blacks mean m can fall
        between windows; using the last fit for everything past the first gap
        once produced a 1 s A/V error, so distance decides.
        """
        require_alignment_sample_coverage(sampled_start, sampled_end, m)

        def dist(f):
            lo, hi, _, _ = f
            if lo <= m <= hi:
                return 0.0
            return min(abs(m - lo), abs(m - hi))

        best = min(fits, key=dist)
        return best[2] * m + best[3]
    return off


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--video", help="override the delivered file to measure")
    ap.add_argument("--audio", help="measure a separate audio file against the same DVD picture-map model")
    ap.add_argument("--dvd-audio", help="DVD audio selector, default manifest qc.dvd_audio_map or 0:a:1")
    ap.add_argument("--points", type=float, nargs="+", default=list(DEFAULT_POINTS))
    ap.add_argument("--out", help="override report path (must not already exist)")
    ap.add_argument("--offset-only", action="store_true",
                    help="print the offset model and exit (no audio decode)")
    args = ap.parse_args()

    import numpy as np
    ld = Ld()
    ep = load_episode(args.episode, ld)
    dkey = ep.data.get("deliverable") or "padfixed"
    video = Path(args.video) if args.video else ep.path(dkey)
    if not video.exists():
        raise PipelineError(f"missing deliverable: {video}")
    audio_path = Path(args.audio) if args.audio else video
    if not audio_path.exists():
        raise PipelineError(f"missing LD audio input: {audio_path}")
    dvd = ep.path("dvd")
    align = ep.path("align_npy")
    # DELTA is the raw master time of output frame 0, taken from the manifest
    delta = ep.delta_s
    dvd_sel = args.dvd_audio or (ep.data.get("qc") or {}).get("dvd_audio_map") or "0:a:1"

    off = build_offset_model(ep, align)
    if args.offset_only:
        return 0

    print(f"measuring LD audio {audio_path} against picture reference {video} and DVD {dvd} ({dvd_sel}) ...", flush=True)
    ld_audio = decode(ld, audio_path, "0:a:0")
    dvd_audio = decode(ld, dvd, dvd_sel)
    print(f"  LD {len(ld_audio)/SR:.3f}s  DVD {len(dvd_audio)/SR:.3f}s  "
          f"DELTA={delta:.6f}s (audio is measured from the delivered file; AudioSkip is already applied)", flush=True)

    rows = []
    for t in args.points:
        master = delta + t
        d_v = master + off(master) / FPS
        k0 = d_v - t
        a = envelope(ld_audio, t, WIN)
        lo, hi = k0 - 6.0, k0 + 6.0
        b = envelope(dvd_audio, t + lo, (hi - lo) + WIN)
        sc = corr_peak(a, b)
        i = int(np.argmax(sc))
        if 0 < i < len(sc) - 1:
            y0, y1, y2 = sc[i - 1], sc[i], sc[i + 1]
            den = y0 - 2 * y1 + y2
            refine = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        else:
            refine = 0.0
        k = lo + (i + refine) * HOP
        av = k - delta - off(master) / FPS
        rows.append((t, master, off(master), k0, k, float(sc[i]), av))
        print(f"  t={t:6.0f}s master={master:8.1f} off={off(master):+7.1f} "
              f"k={k:+7.3f} (expected {k0:+6.3f}) corr={sc[i]:.3f} A/V={av*1000:+7.1f} ms", flush=True)

    x = np.array([r[0] for r in rows])
    y = np.array([r[6] for r in rows]) * 1000
    slope, const = np.polyfit(x, y, 1)
    res = y - (slope * x + const)
    rms = float(res.std(ddof=2)) if len(y) > 2 else 0.0
    ppm = slope * 1000.0          # y in ms, x in s -> ppm is slope * 1000
    out = Path(args.out) if args.out else ld.probe() / f"{ep.code}-av-check-{Path(audio_path).stem}.tsv"
    assert_writable_path(out, ld)
    if out.exists():
        raise PipelineError(f"refusing to overwrite report: {out}")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(("out_time_s", "master_s", "offset_frames", "expected_lag_s",
                    "measured_lag_s", "corr", "av_ms"))
        for r in rows:
            w.writerow((f"{r[0]:.3f}", f"{r[1]:.6f}", f"{r[2]:.3f}", f"{r[3]:.6f}",
                        f"{r[4]:.6f}", f"{r[5]:.6f}", f"{r[6]*1000:.3f}"))
    print(f"\nA/V: constant {const:+.1f} ms  slope {ppm:+.1f} ppm  rms {rms:.1f} ms  n={len(y)}")
    print(f"range {y.min():+.1f}..{y.max():+.1f} ms  report: {out}")
    gates = {
        "constant": abs(const) <= GATE_CONST_MS,
        "slope": abs(ppm) <= GATE_SLOPE_PPM,
        "rms": rms <= GATE_RMS_MS,
    }
    print(f"GATES |constant|<={GATE_CONST_MS:.0f}ms: {gates['constant']}  "
          f"|slope|<={GATE_SLOPE_PPM:.0f}ppm: {gates['slope']}  "
          f"rms<={GATE_RMS_MS:.0f}ms: {gates['rms']}")
    if not all(gates.values()):
        print("FAIL - absolute A/V outside the runbook gate", file=sys.stderr)
        return 1
    print("PASS - absolute A/V within gate")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
