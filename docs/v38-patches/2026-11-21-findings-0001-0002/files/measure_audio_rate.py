#!/usr/bin/env python3
"""Independent multi-window LD-EFM vs DVD-stereo rate diagnostic.

This is corroboration, not the primary asetrate decision: the DVD stereo mix may
differ from the LD mix. It uses the independently measured picture map to center
each search, then fits matched DVD-audio time against LD WAV time. It never writes
audio or edits the episode manifest. ``--raw-provisional`` permits this diagnostic
before an asetrate/origin decision, using the geometric skip only to center windows;
the fitted intercept absorbs a constant origin error.

  python scripts\\measure_audio_rate.py --episode S01E05
  python scripts\\measure_audio_rate.py --episode S01E07 --from-report <saved-diagnostic.tsv> --summary-out <robust-summary.json>

Prerequisites: measured video.delta_s/duration, audio.pad_frames, files.ld_native,
DVD and files.align_npy. Normal mode also requires the recorded AudioSkip/origin check.
Raw-provisional mode derives a geometric window center but does not approve the rate.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402

FPS = 30000.0 / 1001.0
SR = 8000
HOP = 0.05
WINDOW = 50.0
SEARCH = 8.0
RUNNER_EXCLUDE = 1.0
MIN_FIT_CORR = 0.50   # report-screen only; never by itself approves a correction


def diagnostic_skip(delta_s: float, pad_frames: int, override: float | None = None) -> float:
    """Window-center estimate only; a constant error is absorbed by the fit intercept."""
    return float(override) if override is not None else float(delta_s) - int(pad_frames) / FPS


def decode(ld: Ld, path: Path, selector: str):
    import numpy as np
    p = subprocess.run([ld.ffmpeg(), "-hide_banner", "-v", "error", "-i", str(path),
                        "-map", selector, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True)
    if p.returncode:
        raise PipelineError(f"audio decode failed for {path} {selector}: "
                            + p.stderr.decode("utf-8", "replace")[-800:])
    return np.frombuffer(p.stdout, dtype="<i2").astype(np.float32)


def envelope(samples):
    import numpy as np
    hop = int(SR * HOP)
    n = len(samples) // hop
    if n == 0:
        return np.empty(0, np.float64)
    frames = samples[:n*hop].reshape(n, hop).astype(np.float64)
    return np.log1p(np.sqrt(np.mean(frames * frames, axis=1)))


def slide_corr(x, y):
    """Pearson correlation of x with each valid-length window in y."""
    import numpy as np
    x = x.astype(np.float64, copy=True)
    y = y.astype(np.float64, copy=False)
    x -= x.mean()
    n, m = len(x), len(y)
    if m < n or np.std(x) == 0:
        raise PipelineError("audio correlation window is too short or has no variation")
    nfft = 1 << (n + m - 2).bit_length()
    conv = np.fft.irfft(np.fft.rfft(y, nfft) * np.fft.rfft(x[::-1], nfft), nfft)
    dot = conv[n-1:n-1+(m-n+1)]
    sums0 = np.concatenate(([0.0], np.cumsum(y)))
    sums1 = np.concatenate(([0.0], np.cumsum(y*y)))
    sums = sums0[n:] - sums0[:-n]
    energy = sums1[n:] - sums1[:-n] - sums*sums/n
    denom = np.sqrt(float(np.dot(x,x))*np.maximum(energy,1.0))
    return dot/denom


def offset_at(align, master_s: float) -> float:
    import numpy as np
    frame = master_s * FPS
    k = int(np.searchsorted(align[:,0], frame))
    lo, hi = max(0,k-2), min(len(align),k+3)
    if lo == hi:
        raise PipelineError(f"alignment path does not cover master time {master_s:.3f}s")
    return float(np.median(align[lo:hi,1]))


def make_points(ep, requested: list[float] | None) -> list[float]:
    import numpy as np
    duration = ep.duration_s
    if duration <= WINDOW + 2*SEARCH:
        raise PipelineError(f"episode duration {duration:.2f}s is too short for {WINDOW:.0f}s diagnostic windows")
    candidates = requested or list(np.linspace(100.0, duration-WINDOW-100.0, 15))
    blocked = []
    r = ep.data.get("retime") or {}
    for group in (r.get("breaks") or [], r.get("merged_music_through") or []):
        for event in group:
            black = event.get("ld_black")
            if black:
                blocked.append((float(black[0])-ep.delta_s-5.0, float(black[1])-ep.delta_s+5.0))
    points = [float(t) for t in candidates if 0 <= float(t) <= duration-WINDOW]
    points = [t for t in points if not any(t < hi and t+WINDOW > lo for lo,hi in blocked)]
    if len(points) < 4:
        raise PipelineError(f"only {len(points)} usable 50s content windows remain; need >=4 for a rate fit")
    return points


def robust_line_fit(x, y) -> tuple[float, float, float, tuple[float, float]]:
    """Theil-Sen slope + median intercept; return slope, intercept, residual MAD,
    and the leave-one-window-out slope range. This is evidence only, not approval.
    """
    import numpy as np
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    slopes = [(y[j] - y[i]) / (x[j] - x[i])
              for i in range(len(x)) for j in range(i + 1, len(x))
              if x[j] != x[i]]
    if not slopes:
        raise PipelineError("not enough distinct windows for a robust rate fit")
    slope = float(np.median(slopes))
    intercept = float(np.median(y - slope * x))
    residual = y - (slope * x + intercept)
    center = float(np.median(residual))
    residual_mad = 1.4826 * float(np.median(np.abs(residual - center)))
    leave_one_out = []
    if len(x) > 4:
        for omit in range(len(x)):
            xx = np.delete(x, omit)
            yy = np.delete(y, omit)
            ss = [(yy[j] - yy[i]) / (xx[j] - xx[i])
                  for i in range(len(xx)) for j in range(i + 1, len(xx))
                  if xx[j] != xx[i]]
            if ss:
                leave_one_out.append(float(np.median(ss)))
    loo = (min(leave_one_out), max(leave_one_out)) if leave_one_out else (slope, slope)
    return slope, intercept, residual_mad, loo


def unique_report_pair(out: Path, summary: Path) -> tuple[Path, Path]:
    """Allocate a fresh, paired immutable report name after a rerun."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for index in range(10000):
        suffix = stamp if index == 0 else f"{stamp}-{index}"
        candidate_out = out.with_name(f"{out.stem}-{suffix}{out.suffix}")
        candidate_summary = summary.with_name(f"{summary.stem}-{suffix}{summary.suffix}")
        if not candidate_out.exists() and not candidate_summary.exists():
            return candidate_out, candidate_summary
    raise PipelineError(f"could not allocate a unique report pair beside {out}")


def centre_referenced_fit(x_start, y_centre, use) -> dict:
    """Fit window-CENTRE pairs: (WAV window start + WINDOW/2, master-equivalent of matched centre).

    Correlating a 50 s window between clocks that differ by r ppm aligns the windows' centres,
    so start-referenced pairs carry an intercept (origin) bias of about r*WINDOW/2 while the
    slope is unchanged (fstk docs/findings/0001-audio-window-reference-bias.md). Report-only,
    like every fit here: never an approval."""
    import numpy as np
    xc = np.asarray(x_start, dtype=np.float64) + WINDOW / 2.0
    yc = np.asarray(y_centre, dtype=np.float64)
    slope, intercept = np.polyfit(xc[use], yc[use], 1)
    r_slope, r_intercept, r_mad, loo = robust_line_fit(xc[use], yc[use])
    return {"convention": "window-centre pairs", "window_s": WINDOW,
            "ols": {"slope_master_s_per_wav_s": float(slope), "intercept_s": float(intercept),
                    "rate_delta_ppm": float((slope - 1.0) * 1e6)},
            "theil_sen": {"slope_master_s_per_wav_s": float(r_slope), "intercept_s": float(r_intercept),
                          "rate_delta_ppm": float((r_slope - 1.0) * 1e6), "residual_mad_s": float(r_mad)}}


def summarize_saved_report(path: Path, min_corr: float) -> dict:
    """Recompute OLS/robust summaries from a saved window TSV; performs no media I/O."""
    import numpy as np
    if not path.is_file():
        raise PipelineError(f"saved audio-rate report missing: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    required = ("ld_wav_time_s", "master_equivalent_time_s", "corr", "search_edge")
    if not rows or any(key not in rows[0] for key in required):
        raise PipelineError(f"{path} lacks required saved diagnostic columns: {required}")
    x = np.array([float(r["ld_wav_time_s"]) for r in rows], dtype=np.float64)
    y = np.array([float(r["master_equivalent_time_s"]) for r in rows], dtype=np.float64)
    corr = np.array([float(r["corr"]) for r in rows], dtype=np.float64)
    edge = np.array([str(r["search_edge"]).strip().lower() in ("true", "1", "yes") for r in rows])
    use = (corr >= min_corr) & (~edge)
    if int(use.sum()) < 4:
        raise PipelineError(f"only {int(use.sum())} saved windows meet corr>={min_corr} and non-edge; need >=4")
    ols_slope, ols_intercept = np.polyfit(x[use], y[use], 1)
    residual = y[use] - (ols_slope * x[use] + ols_intercept)
    dof = max(1, int(use.sum()) - 2)
    ols_se = float(np.sqrt(np.sum(residual ** 2) / dof / np.sum((x[use] - x[use].mean()) ** 2)))
    robust_slope, robust_intercept, robust_mad, loo = robust_line_fit(x[use], y[use])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if "master_equivalent_centre_time_s" in rows[0]:
        y_centre = np.array([float(r["master_equivalent_centre_time_s"]) for r in rows], dtype=np.float64)
        centre = centre_referenced_fit(x, y_centre, use)
        centre["exact"] = True
    elif all(k in rows[0] for k in ("master_time_s", "dvd_video_time_s")):
        # Older TSV without the centre column: shift by WINDOW/2 through the picture map's
        # mean slope (master s per DVD s); labelled approximate.
        mt = np.array([float(r["master_time_s"]) for r in rows], dtype=np.float64)
        dv = np.array([float(r["dvd_video_time_s"]) for r in rows], dtype=np.float64)
        map_slope = float(np.polyfit(dv, mt, 1)[0]) if len(rows) >= 2 else 1.0
        centre = centre_referenced_fit(x, y + map_slope * WINDOW / 2.0, use)
        centre["exact"] = False
        centre["approximation"] = f"centre = start + WINDOW/2 via picture-map slope {map_slope:.9f}"
    else:
        centre = None
    return {
        "schema": "dvd-picture-audio-rate-summary-v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "saved-report-only",
        "dvd_opened": False,
        "source_tsv": str(path),
        "source_sha256": digest,
        "window_count": len(rows),
        "accepted_window_count": int(use.sum()),
        "min_corr_threshold": min_corr,
        "ols": {"slope_master_s_per_wav_s": float(ols_slope), "intercept_s": float(ols_intercept),
                "rate_delta_ppm": float((ols_slope - 1.0) * 1e6), "slope_stderr_ppm": ols_se * 1e6,
                "residual_sd_s": float(residual.std(ddof=1)),
                "candidate_rate_hz": float(44100.0 / ols_slope) if ols_slope > 0 else None},
        "theil_sen": {"slope_master_s_per_wav_s": float(robust_slope), "intercept_s": float(robust_intercept),
                      "rate_delta_ppm": float((robust_slope - 1.0) * 1e6), "residual_mad_s": robust_mad,
                      "leave_one_window_out_rate_ppm_range": [(loo[0] - 1.0) * 1e6, (loo[1] - 1.0) * 1e6],
                      "candidate_rate_hz": float(44100.0 / robust_slope) if robust_slope > 0 else None},
        "centre_referenced": centre,
        "candidate_is_approval": False,
        "windows": [{"ld_wav_time_s": float(x[i]), "master_equivalent_time_s": float(y[i]),
                     "corr": float(corr[i]), "search_edge": bool(edge[i]), "used": bool(use[i])}
                    for i in range(len(rows))],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--ld-audio", help="override manifest files.ld_native")
    ap.add_argument("--dvd-audio", help="FFmpeg stream selector; auto-selects the unique 2-channel stream by default")
    ap.add_argument("--points", type=float, nargs="+", help="output timeline window starts (seconds)")
    ap.add_argument("--search", type=float, default=SEARCH, help="search radius around picture-map DVD time")
    ap.add_argument("--min-corr", type=float, default=MIN_FIT_CORR, help="screening threshold for the diagnostic fit only")
    ap.add_argument("--raw-provisional", action="store_true",
                    help="run on native ld_native before origin/rate approval; uses geometric skip only as a window center")
    ap.add_argument("--skip-s", type=float, help="explicit provisional window-center skip (requires --raw-provisional)")
    ap.add_argument("--out", help="override report path")
    ap.add_argument("--summary-out", help="override JSON fit-summary path (defaults beside the TSV)")
    ap.add_argument("--from-report", help="recompute robust fit from a previously saved TSV without decoding media/DVD")
    args = ap.parse_args()
    ld = Ld()

    if args.from_report:
        saved = Path(args.from_report)
        summary_out = Path(args.summary_out) if args.summary_out else saved.with_suffix(".robust-summary.json")
        if not args.summary_out and summary_out.exists():
            summary_out = summary_out.with_name(
                f"{summary_out.stem}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                f"{summary_out.suffix}")
        assert_writable_path(summary_out, ld)
        if summary_out.exists():
            raise PipelineError(f"refusing to overwrite summary: {summary_out}")
        summary = summarize_saved_report(saved, args.min_corr)
        summary["episode"] = args.episode
        summary["workflow_version"] = (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip()
        summary["created_utc"] = datetime.now(timezone.utc).isoformat()
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"saved-report-only robust summary: {summary_out}; no audio/DVD media decoded")
        print(f"OLS candidate={summary['ols']['candidate_rate_hz']} Hz; robust candidate={summary['theil_sen']['candidate_rate_hz']} Hz; compare, do not auto-apply")
        return 0

    import numpy as np
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("two-part E01 needs separate per-part audio/time mapping; generic rate diagnostic is one-part only")
    pad = ep.pad_trim
    delta = ep.delta_s
    if args.skip_s is not None and not args.raw_provisional:
        raise PipelineError("--skip-s is only valid with --raw-provisional")
    if args.raw_provisional:
        skip = diagnostic_skip(delta, pad, args.skip_s)
        print(f"RAW-PROVISIONAL: using window-center AudioSkip={skip:.6f}s; intercept/origin are not approved or written")
    else:
        if not ep.data.get("audio", {}).get("origin_verified_by_duration"):
            raise PipelineError("verify raw EFM WAV origin against the duration model, or use --raw-provisional for an uncommitted diagnostic")
        skip = ep.audio_skip_s
        geometric_skip = delta - pad / FPS
        if abs(skip - geometric_skip) > 0.5 / FPS:
            raise PipelineError(f"audio.skip_s={skip:.6f}s disagrees with measured frame origin (DELTA-pad/FPS={geometric_skip:.6f}s); resolve the one-frame/origin convention first")
    wav = Path(args.ld_audio) if args.ld_audio else ep.path("ld_native")
    dvd = ep.path("dvd")
    align_path = ep.path("align_npy")
    for f in (wav,dvd,align_path):
        if not f.exists():
            raise PipelineError(f"required input missing: {f}")
    align = np.load(align_path)
    if align.ndim != 2 or align.shape[1] < 2 or len(align) < 20:
        raise PipelineError(f"bad alignment path shape: {align.shape}")
    master_clock = align[:,0].astype(np.float64) / FPS
    dvd_picture_clock = (align[:,0].astype(np.float64) + align[:,1].astype(np.float64)) / FPS
    order = np.argsort(dvd_picture_clock)
    dvd_picture_clock = dvd_picture_clock[order]
    master_clock = master_clock[order]

    if args.dvd_audio:
        dvd_selector = args.dvd_audio
    else:
        p = subprocess.run([ld.ffprobe(), "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=channels", "-of", "csv=p=0", str(dvd)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode:
            raise PipelineError(f"ffprobe failed: {p.stderr[-500:]}")
        channels = [x.strip() for x in p.stdout.splitlines() if x.strip()]
        stereo = [i for i,c in enumerate(channels) if c == "2"]
        if len(stereo) != 1:
            raise PipelineError(f"expected a unique 2-channel DVD reference stream, found audio channel counts {channels}; provide --dvd-audio after verifying the stream")
        dvd_selector = f"0:a:{stereo[0]}"

    points = make_points(ep,args.points)
    print(f"using measured frame origin: pad={pad} frames, AudioSkip={skip:.6f}s; decoding {wav} and {dvd} {dvd_selector} sequentially to mono {SR} Hz envelopes",flush=True)
    ld_env = envelope(decode(ld,wav,"0:a:0"))
    dvd_env = envelope(decode(ld,dvd,dvd_selector))
    rows=[]
    win_n=round(WINDOW/HOP)
    search_n=round(args.search/HOP)
    for t in points:
        wav_start=t+skip
        master=delta+t
        offset=offset_at(align,master)
        dvd_video=master+offset/FPS
        a0=round(wav_start/HOP)
        x=ld_env[a0:a0+win_n]
        if len(x)!=win_n:
            raise PipelineError(f"LD audio ends before diagnostic window at output {t:.3f}s (WAV start {wav_start:.3f}s)")
        dvd_start=max(0.0,dvd_video-args.search)
        b0=round(dvd_start/HOP)
        y=dvd_env[b0:round((dvd_video+WINDOW+args.search)/HOP)]
        scores=slide_corr(x,y)
        k=int(np.argmax(scores))
        peak=float(scores[k])
        excluded=round(RUNNER_EXCLUDE/HOP)
        far=scores.copy();far[max(0,k-excluded):min(len(far),k+excluded+1)]=-1
        runner=float(far.max()) if len(far) else -1.0
        refine=0.0
        if 0<k<len(scores)-1:
            y0,y1,y2=scores[k-1],scores[k],scores[k+1]
            den=y0-2*y1+y2
            if abs(den)>1e-12: refine=0.5*(y0-y2)/den
        dvd_audio_start=dvd_start+(k+refine)*HOP
        edge=abs((k+refine)-search_n)<=2
        # Invert the independently measured picture map. This separates the
        # LD-audio-vs-master rate from the DVD-audio-vs-picture offset.
        master_equivalent = float(np.interp(dvd_audio_start,dvd_picture_clock,master_clock))
        master_equivalent_centre = float(np.interp(dvd_audio_start+WINDOW/2.0,dvd_picture_clock,master_clock))
        rows.append((t,wav_start,master,offset,dvd_video,dvd_audio_start,master_equivalent,peak,runner,edge,master_equivalent_centre))
        print(f"out={t:7.1f}s WAV={wav_start:8.3f}s DVD-video={dvd_video:8.3f}s DVD-audio={dvd_audio_start:8.3f}s master-equivalent={master_equivalent:8.3f}s corr={peak:.3f} runner={runner:.3f} edge={edge}",flush=True)

    x=np.array([r[1] for r in rows],dtype=np.float64)
    y=np.array([r[6] for r in rows],dtype=np.float64)
    corr=np.array([r[7] for r in rows])
    use=(corr>=args.min_corr) & (~np.array([r[9] for r in rows],dtype=bool))
    if use.sum()<4:
        raise PipelineError(f"only {int(use.sum())} windows meet corr>={args.min_corr} and non-edge peak; need >=4; do not set asetrate from this diagnostic")
    slope,intercept=np.polyfit(x[use],y[use],1)
    residual=y[use]-(slope*x[use]+intercept)
    dof=max(1,int(use.sum())-2)
    slope_se=float(np.sqrt(np.sum(residual**2)/dof/np.sum((x[use]-x[use].mean())**2)))
    ppm=(slope-1)*1e6
    ppm_se=slope_se*1e6
    print(f"\nDIAGNOSTIC FIT (not an asetrate approval): master-equivalent-time = {intercept:.6f} + {slope:.9f} * LD-WAV-time")
    print(f"rate delta={ppm:+.1f} +/- {ppm_se:.1f} ppm; residual SD={residual.std(ddof=1):.4f}s; points={int(use.sum())}/{len(rows)}")
    robust_slope, robust_intercept, robust_mad, loo = robust_line_fit(x[use], y[use])
    robust_ppm = (robust_slope - 1.0) * 1e6
    robust_loo_ppm = ((loo[0] - 1.0) * 1e6, (loo[1] - 1.0) * 1e6)
    print(f"ROBUST CHECK (Theil-Sen; not an approval): master-equivalent = {robust_intercept:.6f} + "
          f"{robust_slope:.9f} * LD-WAV-time; delta={robust_ppm:+.1f} ppm; "
          f"residual MAD={robust_mad:.4f}s; leave-one-window-out={robust_loo_ppm[0]:+.1f}..{robust_loo_ppm[1]:+.1f} ppm")
    centre = centre_referenced_fit(x, np.array([r[10] for r in rows], dtype=np.float64), use)
    centre["exact"] = True
    print(f"CENTRE-REFERENCED (window centres; not an approval): master-equivalent = "
          f"{centre['theil_sen']['intercept_s']:.6f} + {centre['theil_sen']['slope_master_s_per_wav_s']:.9f} * LD-WAV-time; "
          f"origin shift vs start-referenced = {(centre['theil_sen']['intercept_s'] - robust_intercept) * 1000:+.1f} ms")
    if slope > 0:
        candidate = 44100.0 / slope
        print(f"candidate-only sample-rate label: {candidate:.2f} Hz (do not apply unless the primary fade-midpoint fit/origin check independently agrees)")
    print("Compare with the runbook's fade-midpoint fit and picture-map rate. If they disagree, inspect the mixes/windows or STOP; do not apply this number automatically.")
    out=Path(args.out) if args.out else (ep.path("audio_rate_report") if ep.data.get("files",{}).get("audio_rate_report") else ld.probe()/f"{ep.code}-audio-rate-diagnostic.tsv")
    summary_out = Path(args.summary_out) if args.summary_out else out.with_suffix(".summary.json")
    if not args.out and not args.summary_out and (out.exists() or summary_out.exists()):
        out, summary_out = unique_report_pair(out, summary_out)
    assert_writable_path(out, ld)
    assert_writable_path(summary_out, ld)
    if out.exists() or summary_out.exists():
        raise PipelineError(f"refusing to overwrite audio-rate reports: {out} / {summary_out}")
    out.parent.mkdir(parents=True,exist_ok=True)
    with out.open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f,delimiter="\t")
        w.writerow(("out_time_s","ld_wav_time_s","master_time_s","picture_offset_frames","dvd_video_time_s","matched_dvd_audio_time_s","master_equivalent_time_s","corr","runner_up_corr","search_edge","master_equivalent_centre_time_s"))
        for r in rows:w.writerow([f"{x:.6f}" if isinstance(x,(float,np.floating)) else x for x in r])
    print(f"report: {out}")
    summary = {
        "schema": "dvd-picture-audio-rate-summary-v2",
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "episode": ep.code,
        "mode": "raw-provisional" if args.raw_provisional else "origin-verified",
        "dvd_opened": True,
        "native_ld_wav": str(wav), "dvd_reference": str(dvd), "picture_alignment_path": str(align_path),
        "video_delta_s": delta,
        "audio_pad_frames": pad,
        "window_center_skip_s": skip,
        "origin_verified_by_duration": bool(ep.data.get("audio", {}).get("origin_verified_by_duration")),
        "window_count": len(rows),
        "accepted_window_count": int(use.sum()),
        "min_corr_threshold": args.min_corr,
        "ols": {"slope_master_s_per_wav_s": float(slope), "intercept_s": float(intercept),
                "rate_delta_ppm": float(ppm), "slope_stderr_ppm": float(ppm_se),
                "residual_sd_s": float(residual.std(ddof=1)),
                "candidate_rate_hz": float(44100.0 / slope) if slope > 0 else None},
        "theil_sen": {"slope_master_s_per_wav_s": float(robust_slope), "intercept_s": float(robust_intercept),
                      "rate_delta_ppm": float(robust_ppm), "residual_mad_s": float(robust_mad),
                      "leave_one_window_out_rate_ppm_range": [float(robust_loo_ppm[0]), float(robust_loo_ppm[1])],
                      "candidate_rate_hz": float(44100.0 / robust_slope) if robust_slope > 0 else None},
        "candidate_is_approval": False,
        "centre_referenced": centre,
        "window_tsv": str(out),
        "windows": [{"out_time_s": float(r[0]), "ld_wav_time_s": float(r[1]),
                     "master_equivalent_time_s": float(r[6]), "corr": float(r[7]),
                     "runner_up_corr": float(r[8]), "search_edge": bool(r[9]),
                     "master_equivalent_centre_time_s": float(r[10])} for r in rows],
    }
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"fit summary: {summary_out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}",file=sys.stderr)
        raise SystemExit(2)
