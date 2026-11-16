#!/usr/bin/env python3
"""Second-by-second LD-vs-DVD audio comparison (amplitude, spectrum, content).

Reusable, episode-parameterized replacement for the one-off E08 script
(`ld-dvd-audio-compare-s01e08.py`), which hard-coded E08 paths and must not be
reused for another episode (job prompt: no `*-s01e05`/`*-e04`-style scripts).

Independent of the picture map: cross-correlates the two programmes' short-time
envelopes directly and compares per-second spectral band energy as a content
check.  Used to corroborate an asetrate/rate decision and to verify that the two
sources really are the same programme at the same content times.

This is a REPORT-ONLY diagnostic.  It selects no rate, edits no manifest, and
approves nothing -- the runbook keeps `measure_audio_rate.py`'s fit (measurement)
separate from `asetrate` (approved decision).

  python scripts/ld_dvd_audio_compare.py --episode S01E08
  python scripts/ld_dvd_audio_compare.py --episode S01E08 --wav corrected
  python scripts/ld_dvd_audio_compare.py --episode S01E08 --ld-wav X.wav --dvd Y.mkv

Outputs: <probe-root>/<code>-ld-dvd-audio-compare.json plus a console summary.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ldcommon import (Ld, PipelineError, assert_writable_path, load_episode,
                      redact_shared_text)  # noqa: E402

SR = 44100
BANDS = [(20, 160), (160, 630), (630, 2500), (2500, 6300), (6300, 12000), (12000, 20000)]
REPORT_SCHEMA = "ld-dvd-audio-compare-v3"


def load_ld(path: Path) -> "object":
    """Mono float32 mean of the LD WAV (native or rate-corrected)."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        if sw != 2:
            raise PipelineError(f"{path}: expected 16-bit PCM, got width {sw}")
        if sr != SR:
            raise PipelineError(f"{path}: expected {SR} Hz, got {sr}")
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
    return a.mean(axis=1).astype(np.float32)


def load_dvd(ffmpeg: Path, dvd: Path, selector: str) -> "object":
    """Decode the selected DVD mix to mono 44.1 kHz s16le raw, sequentially (never -ss)."""
    import numpy as np
    p = subprocess.run([str(ffmpeg), "-hide_banner", "-v", "error", "-i", str(dvd),
                        "-map", selector, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise PipelineError(f"DVD decode failed: {p.stderr.decode(errors='replace')[:400]}")
    if not p.stdout:
        raise PipelineError("DVD decode produced no samples")
    if len(p.stdout) % 2:
        raise PipelineError("DVD decoder returned a partial 16-bit audio sample")
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)


def per_second_rms(x, block=128):
    """Per-second RMS in dBFS, blockwise so no (n, 44100) float64 array is built."""
    import numpy as np
    n = len(x) // SR
    out = np.empty(n, dtype=np.float64)
    for s in range(0, n, block):
        e = min(n, s + block)
        seg = x[s * SR: e * SR].astype(np.float64).reshape(e - s, SR)
        r = np.sqrt((seg ** 2).mean(axis=1))
        out[s:e] = 20.0 * np.log10(np.maximum(r, 1.0) / 32768.0)
    return out, n


def per_second_bands(x, frame=8192, per_sec=4):
    """Fractional band energy per second from `per_sec` windowed frames per second.

    8192 samples @ 44.1 kHz ~= 186 ms; four offsets per second are averaged.
    Kept memory-bounded (~n x frame float64, one offset at a time).
    """
    import numpy as np
    n = len(x) // SR
    freqs = np.fft.rfftfreq(frame, 1 / SR)
    masks = [((freqs >= lo) & (freqs < hi)) for lo, hi in BANDS]
    acc = np.zeros((n, len(BANDS)), dtype=np.float64)
    win = np.hanning(frame)
    for k in range(per_sec):
        start = k * (SR // per_sec)
        if start + frame > SR:
            break
        seg = np.empty((n, frame), dtype=np.float64)
        for i in range(n):
            seg[i] = x[i * SR + start: i * SR + start + frame]
        mag = np.abs(np.fft.rfft(seg * win, axis=1)) ** 2
        for i, m in enumerate(masks):
            acc[:, i] += mag[:, m].sum(axis=1)
        del seg, mag
    tot = acc.sum(axis=1, keepdims=True)
    return acc / np.maximum(tot, 1e-12), n


def best_lag(a, b, max_lag):
    """Coarse lag search. Positive lag means LD has extra leading seconds vs DVD."""
    import numpy as np
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if max_lag < 0:
        raise PipelineError("maximum lag must be non-negative")
    a = a - a.mean()
    b = b - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0, 0.0
    best, bl = -2.0, 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[lag:], b[: len(b) - lag] if lag else b
        else:
            x, y = a[: len(a) + lag], b[-lag:]
        if len(x) < 100:
            continue
        c = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-12))
        if c > best:
            best, bl = c, lag
    return bl, best


def compare_envelopes(ld_env, dvd_env, max_lag):
    """Estimate coarse/refined content lag and summarize the first 400 aligned seconds."""
    import numpy as np

    ld_env = np.asarray(ld_env, dtype=np.float64)
    dvd_env = np.asarray(dvd_env, dtype=np.float64)
    if not len(ld_env) or not len(dvd_env):
        raise PipelineError("audio comparison needs at least one complete second per source")
    lag, corr = best_lag(ld_env, dvd_env, max_lag)
    if lag >= 0:
        overlap = min(len(ld_env) - lag, len(dvd_env))
        a, b = ld_env[lag:lag + overlap], dvd_env[:overlap]
    else:
        overlap = min(len(ld_env), len(dvd_env) + lag)
        a, b = ld_env[:overlap], dvd_env[-lag:-lag + overlap]
    if overlap < 100:
        raise PipelineError("audio sources have fewer than 100 overlapping seconds")
    lag2, corr2 = best_lag(a, b, min(10, max_lag))
    offset = lag + lag2

    if offset >= 0:
        start_ld, start_dvd = offset, 0
    else:
        start_ld, start_dvd = 0, -offset
    count = min(400, len(ld_env) - start_ld, len(dvd_env) - start_dvd)
    if count <= 0:
        raise PipelineError(f"refined lag {offset}s leaves no aligned audio seconds")
    diff = ld_env[start_ld:start_ld + count] - dvd_env[start_dvd:start_dvd + count]
    return {
        "envelope_best_lag_s": int(lag),
        "envelope_best_corr": float(corr),
        "envelope_refined_lag_s": int(offset),
        "envelope_refined_corr": float(corr2),
        "aligned_seconds": int(count),
        "envelope_diff_db": {
            "median": float(np.median(diff)),
            "mean": float(diff.mean()),
            "sd": float(diff.std()),
            "within_3db": int((np.abs(diff) < 3).sum()),
        },
    }


def summarize_bands(ld_bands, dvd_bands, lag_s):
    """Preserve the v2 same-start whole-source band statistics with explicit scope."""
    import numpy as np

    count = min(len(ld_bands), len(dvd_bands))
    if count <= 0:
        raise PipelineError("audio comparison has no complete seconds for band analysis")
    return {
        "band_names": [f"{lo}-{hi}" for lo, hi in BANDS],
        "band_scope": ("unshifted whole-source mean over same-start seconds; "
                       f"NOT corrected for the refined content lag ({int(lag_s)} s). "
                       "Use for mix-EQ characterisation only."),
        "band_lag_corrected": False,
        "band_lag_s": int(lag_s),
        "band_seconds_compared": int(count),
        "ld_band_mean": [round(float(x), 5) for x in np.asarray(ld_bands[:count]).mean(axis=0)],
        "dvd_band_mean": [round(float(x), 5) for x in np.asarray(dvd_bands[:count]).mean(axis=0)],
    }


def resolve_dvd_selector(episode_data: dict, override: str | None) -> tuple[str, str]:
    """Prefer explicit CLI, then the manifest QC selector, then the documented default."""
    if override is not None:
        if not override.strip():
            raise PipelineError("DVD audio selector must not be empty")
        return override.strip(), "command-line override"
    qc = episode_data.get("qc") or {}
    configured = qc.get("dvd_audio_map")
    if isinstance(configured, str) and configured.strip():
        return configured.strip(), "manifest qc.dvd_audio_map"
    return "0:a:1", "default (discrete DVD 5.1 stream)"


def serialize_report(document: dict, ld: Ld) -> str:
    """Serialize and redact local paths before a report can enter shared storage."""
    text = json.dumps(document, ensure_ascii=False, indent=2)
    safe = redact_shared_text(text, ld)
    try:
        json.loads(safe)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"shared-path redaction produced invalid JSON: {exc}") from exc
    return safe


def write_report(path: Path, document: dict, ld: Ld) -> Path:
    """Create a report exclusively; never overwrite an earlier diagnostic."""
    path = assert_writable_path(path, ld)
    safe = serialize_report(document, ld)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(safe + "\n")
    except FileExistsError as exc:
        raise PipelineError(f"refusing to overwrite existing report: {path}") from exc
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True, help="manifest code, e.g. S01E08")
    ap.add_argument("--wav", choices=("native", "corrected"), default="native",
                    help="compare files.ld_native (default) or files.ld_corrected")
    ap.add_argument("--ld-wav", help="override the LD WAV path (bypasses the manifest)")
    ap.add_argument("--dvd", help="override the DVD path (bypasses the manifest)")
    ap.add_argument("--dvd-selector",
                    help="override the manifest DVD audio selector (default 0:a:1)")
    ap.add_argument("--max-lag-s", type=int, default=120, help="coarse lag search half-range")
    ap.add_argument("--out", help="override the JSON report path")
    args = ap.parse_args()
    if args.max_lag_s < 0:
        raise PipelineError("--max-lag-s must be non-negative")

    ld = Ld()
    ep = load_episode(args.episode, ld)
    dvd_selector, dvd_selector_source = resolve_dvd_selector(ep.data, args.dvd_selector)

    files = ep.data.get("files") or {}
    if args.ld_wav:
        ld_wav = Path(args.ld_wav)
    else:
        key = "ld_corrected" if args.wav == "corrected" else "ld_native"
        entry = files.get(key)
        if not entry:
            raise PipelineError(
                f"manifest files.{key} is unset; measure it at the audio stage first "
                f"(or pass --ld-wav)")
        ld_wav = ep.path(key)
    if args.dvd:
        dvd = Path(args.dvd)
    else:
        entry = files.get("dvd")
        if not entry:
            raise PipelineError("manifest files.dvd is unset; pass --dvd")
        dvd = ep.path("dvd")

    if not ld_wav.exists():
        raise PipelineError(f"LD WAV missing: {ld_wav}")
    if not dvd.exists():
        raise PipelineError(f"DVD missing: {dvd}")
    ld_wav_kind = "command-line override" if args.ld_wav else args.wav

    ffmpeg = Path(ld.tool("ffmpeg"))
    report = Path(args.out) if args.out else ld.probe() / f"{ep.code}-ld-dvd-audio-compare.json"
    report = assert_writable_path(report, ld)
    if report.exists():
        raise PipelineError(f"refusing to overwrite existing report: {report}")

    print(f"episode   : {ep.code}")
    print(f"LD WAV    : {redact_shared_text(str(ld_wav), ld)}  ({ld_wav_kind})")
    print(f"DVD       : {redact_shared_text(str(dvd), ld)}  selector {dvd_selector} ({dvd_selector_source})")
    print(f"report    : {redact_shared_text(str(report), ld)}")

    print("[1/4] loading LD WAV ...", flush=True)
    ldm = load_ld(ld_wav)
    print(f"  {len(ldm)} samples ({len(ldm)/SR:.3f} s)")
    print("[2/4] decoding DVD -> mono 44.1k (sequential, no -ss) ...", flush=True)
    dvdm = load_dvd(ffmpeg, dvd, dvd_selector)
    print(f"  {len(dvdm)} samples ({len(dvdm)/SR:.3f} s)")

    print("[3/4] per-second RMS envelopes ...", flush=True)
    ld_env, n1 = per_second_rms(ldm)
    dvd_env, n2 = per_second_rms(dvdm)
    n = min(n1, n2)
    if n <= 0:
        raise PipelineError("audio sources have no complete seconds to compare")
    ld_env, dvd_env = ld_env[:n], dvd_env[:n]

    print("[4/4] per-second band-energy fractions ...", flush=True)
    ld_b, _ = per_second_bands(ldm)
    dvd_b, _ = per_second_bands(dvdm)
    m = min(len(ld_b), len(dvd_b))
    ld_b, dvd_b = ld_b[:m], dvd_b[:m]

    comparison = compare_envelopes(ld_env, dvd_env, args.max_lag_s)
    off = comparison["envelope_refined_lag_s"]
    print(f"\nenvelope best lag = {comparison['envelope_best_lag_s']} s   "
          f"corr = {comparison['envelope_best_corr']:.4f}")
    print(f"refined lag = {off} s   corr = {comparison['envelope_refined_corr']:.4f}")
    diff = comparison["envelope_diff_db"]
    k = comparison["aligned_seconds"]
    print(f"envelope difference over first {k} aligned seconds: "
          f"median {diff['median']:+.2f} dB, mean {diff['mean']:+.2f} dB, "
          f"sd {diff['sd']:.2f} dB, |d|<3 dB: {diff['within_3db']}/{k}")

    print("\nmean fractional band energy (LD vs DVD, "
          "UNSHIFTED whole-source - same-start seconds, NOT lag-aligned):")
    band_summary = summarize_bands(ld_b, dvd_b, off)
    for i, name in enumerate(band_summary["band_names"]):
        ld_mean = band_summary["ld_band_mean"][i]
        dvd_mean = band_summary["dvd_band_mean"][i]
        print(f"  {name:>10} Hz : LD {ld_mean:.4f}   DVD {dvd_mean:.4f}   "
              f"diff {ld_mean - dvd_mean:+.4f}")

    doc = {
        "schema": REPORT_SCHEMA,
        "episode": ep.code,
        "method": "second-by-second LD vs DVD amplitude + band-energy comparison (report-only)",
        "ld_wav": str(ld_wav),
        "ld_wav_kind": ld_wav_kind,
        "dvd": str(dvd),
        "dvd_selector": dvd_selector,
        "dvd_selector_source": dvd_selector_source,
        "lag_convention": ("positive means LD has extra leading seconds relative to DVD; "
                           "the aligned comparison uses LD[lag:] against DVD[0:]"),
        "ld_samples": int(len(ldm)),
        "dvd_samples": int(len(dvdm)),
        "seconds_compared": int(n),
        **comparison,
        **band_summary,
        "ld_env_db": [round(float(x), 2) for x in ld_env],
        "dvd_env_db": [round(float(x), 2) for x in dvd_env],
        "caveat": ("report-only; selects no rate and edits no manifest. The two mixes are "
                   "genuinely different (LD Dolby Surround vs DVD AC-3), so a lag fitted on "
                   "their envelopes inherits that EQ difference and is corroboration, not a "
                   "rate measurement."),
    }
    write_report(report, doc, ld)
    print(f"\nreport: {redact_shared_text(str(report), ld)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
