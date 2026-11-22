"""``dvd-ld-audio``: the LD stereo track for DVD episodes, timed against the DVD.

Replaces v38 ``measure_audio_rate.py`` (a diagnostic only), the manual rate/origin decision, and
``build_ld_synced.py``. The owner wants the time-aligned LD stereo as the default track of every
DVD episode.

1. **Measure the LD audio clock** (``fstk.media.dvd_audio.measure_clock``). Windows of the native
   EFM audio are matched against the DVD stereo track, mapped to master time through the
   accepted picture alignment's act curves, and fitted (centre-referenced, robust) to give
   ``master = intercept + slope·wav``. ``rate = 44100 / slope``; ``intercept`` is the master time
   of WAV sample 0.
2. **Accept automatically** only if the fit is precise: enough windows across the episode, origin
   standard error ≤ 5 ms, slope standard error ≤ 5 ppm, and a stable leave-one-out rate.
   Otherwise the stage is blocked with the numbers. Cross-checks are recorded, not gating: the
   origin against the capture-pad geometry (S01E20: +1 frame for both M1 and P2), and the rate
   against 44100·1000/1001.
3. **Build** corrected + sample-exact synced 24-bit FLAC at the exact rate
   (``stages.ldaudio_build``), with ``AudioSkip = video.delta_s − intercept``. Any A/V-check
   correction (``audio.av_correction``) is applied on top: rate ÷ (1 + drift),
   skip·(1 + drift) − offset.

The DVD is decoded sequentially (never seeked).
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fstk.core.errors import GateError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.hashing import sha256_file
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import dvd_audio as da
from fstk.media.audio_timing import no_dvd_audio_plan_gib
from fstk.qc.dvdfree import corrected_skip, ld_correction, start_sample, target_samples

from .base import ArtifactExists, Custom, SpaceNeed, Stage, StepOutput, StepPlan, ValueIs, ValueSet, register
from .ldaudio_build import build_corrected_and_synced

RATE_METHOD = "dvd-referenced-clock"
REPORT_SCHEMA = "fstk-dvd-ld-audio-v1"
ACCEPT = {"min_windows": 40, "min_coverage": 0.8, "max_intercept_stderr_ms": 5.0, "max_slope_stderr_ppm": 5.0,
          "max_loo_spread_ppm": 5.0}
NOMINAL_1001_HZ = 44100.0 * 1000.0 / 1001.0


def _alignment_accepted(ctx, manifest):
    rec = manifest.get("video.dvd_alignment") or {}
    ok = bool(rec.get("acts"))
    return ok, "accepted DVD alignment with act curves" if ok else "video.dvd_alignment (accepted) is required"


def clock_issues(fit: da.ClockFit) -> list[str]:
    issues = []
    if fit.windows_used < ACCEPT["min_windows"]:
        issues.append(f"only {fit.windows_used} usable windows (need {ACCEPT['min_windows']})")
    if fit.coverage < ACCEPT["min_coverage"]:
        issues.append(f"windows cover only {fit.coverage:.0%} of the side")
    if fit.intercept_stderr_ms > ACCEPT["max_intercept_stderr_ms"]:
        issues.append(f"origin standard error {fit.intercept_stderr_ms:.1f} ms")
    if fit.slope_stderr_ppm > ACCEPT["max_slope_stderr_ppm"]:
        issues.append(f"rate standard error {fit.slope_stderr_ppm:.1f} ppm")
    spread = fit.loo_rate_ppm_range[1] - fit.loo_rate_ppm_range[0]
    if spread > ACCEPT["max_loo_spread_ppm"]:
        issues.append(f"leave-one-out rate spread {spread:.1f} ppm")
    return issues


@register
class DvdLdAudio(Stage):
    id = "dvd-ld-audio"
    title = "LD stereo timed against the DVD (rate + origin)"
    description = ("Measures the LD audio clock against the DVD stereo through the picture alignment, "
                   "accepts it automatically when precise, and builds the exact-rate, sample-exact LD track.")
    requires = (ValueIs("mode.dvd_reference_available", True), Custom("dvd-alignment", _alignment_accepted),
                ValueSet("audio.capture"), ValueSet("audio.pad_frames"), ValueSet("video.frames"),
                ValueSet("video.delta_s"), ArtifactExists("ld_native"), ArtifactExists("dvd"))
    writes = ("audio.rate_method", "audio.asetrate", "audio.resampler", "audio.skip_s", "audio.skip_method",
              "audio.wav0_master_frame", "audio.dvd_clock", "audio.corrected_duration_s",
              "deliverable.ld_audio_source")
    writes_artifacts = ("ld_corrected", "ld_synced", "audio_timing_report")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        source, dvd = ctx.artifact_path("ld_native", m), ctx.artifact_path("dvd", m)
        for p in (source, dvd):
            if not p.is_file():
                raise GateError(f"input missing: {p}")
        run = f"{utc_stamp()[:15].lower()}-{secrets.token_hex(3)}"
        audio_dir = ctx.root("audio")
        names = {"corrected": audio_dir / f"{ctx.episode.code}-LD20-dvdref-corrected-{run}.flac",
                 "synced": audio_dir / f"{ctx.episode.code}-LD20-dvdref-synced-{run}.flac",
                 "report": ctx.root("reports") / f"dvd-ld-audio-{run}.json"}
        return StepPlan(self.id, params, inputs=[source, dvd], outputs=list(names.values()),
                        space=SpaceNeed("audio", no_dvd_audio_plan_gib(source.stat().st_size),
                                        "corrected + synced 24-bit FLAC of the native WAV"),
                        summary=f"measure the LD clock against {dvd.name}, then build the synced LD track",
                        data={"source": str(source), "dvd": str(dvd), "run": run,
                              "stream": params.get("dvd_stereo_stream"), **{k: str(v) for k, v in names.items()}})

    def _dvd_stereo(self, ff, dvd: Path, explicit) -> str:
        if explicit is not None:
            return f"0:a:{int(explicit)}"
        streams = ff.probe(dvd).of_type("audio")
        stereo = [i for i, s in enumerate(streams) if int(s.raw.get("channels") or 0) == 2]
        if len(stereo) != 1:
            raise GateError(f"expected one stereo DVD audio stream, found channel counts "
                            f"{[s.raw.get('channels') for s in streams]}; set param dvd_stereo_stream")
        return f"0:a:{stereo[0]}"

    def run(self, ctx, plan):
        d, m = plan.data, ctx.manifest
        ff = ctx.tool("ffmpeg")
        source, dvd = Path(d["source"]), Path(d["dvd"])
        selector = self._dvd_stereo(ff, dvd, d["stream"])
        ctx.report(0.02, "decoding LD audio (mono 8 kHz analysis copy)")
        ld_pcm = ff.pcm(source, "0:a:0", da.SR, cancel=ctx.cancel)
        ctx.report(0.06, "decoding DVD stereo sequentially")
        dvd_pcm = ff.pcm(dvd, selector, da.SR, cancel=ctx.cancel)
        ctx.check_cancel()
        try:
            fit = da.measure_clock(ld_pcm, dvd_pcm, m.require("video.dvd_alignment")["acts"],
                                   progress=lambda f: ctx.report(0.1 + 0.2 * f, "matching LD to DVD audio"))
        except ValueError as exc:
            raise GateError(f"the LD audio clock could not be measured against the DVD: {exc}") from None
        issues = clock_issues(fit)
        if issues:
            raise GateError("DVD-referenced LD clock not precise enough to accept automatically: "
                            + "; ".join(issues))
        offset_ms, drift_ppm = ld_correction({"audio.av_correction": m.get("audio.av_correction")})
        rate = fit.rate_hz / (1.0 + drift_ppm * 1e-6)
        base_skip = float(m.require("video.delta_s")) - fit.intercept_s
        skip = corrected_skip(base_skip, offset_ms, drift_ppm)
        frames = int(m.require("video.frames"))
        start, target = start_sample(skip), target_samples(frames)
        built = build_corrected_and_synced(ctx, source, rate_hz=rate, start_sample=start, target_samples=target,
                                           corrected=Path(d["corrected"]), synced=Path(d["synced"]),
                                           base=0.35, span=0.6)
        pad = int(m.require("audio.pad_frames"))
        wav0 = fit.intercept_s * da.FPS
        cross = {"origin_minus_pad_geometry_frames": round(wav0 - pad, 3),
                 "rate_minus_44100x1000/1001_ppm": round((fit.rate_hz / NOMINAL_1001_HZ - 1) * 1e6, 2)}
        report = {"schema": REPORT_SCHEMA, "created_utc": utc_now_iso(), "episode": str(ctx.episode), "run": d["run"],
                  "rate_method": RATE_METHOD, "dvd_stereo_stream": selector, "clock": fit.summary(),
                  "acceptance": ACCEPT, "cross_checks": cross, "video_delta_s": m.get("video.delta_s"),
                  "base_skip_s": base_skip, "av_correction": {"offset_ms": offset_ms, "drift_ppm": drift_ppm},
                  "effective_rate_hz": rate, "skip_s": skip, "start_sample": start, "target_samples": target,
                  "source_audio": str(source), "source_audio_sha256": sha256_file(source), "dvd": str(dvd),
                  "resample": built["resample"], "corrected": built["corrected"], "synced": built["synced"]}
        atomic_write_json(d["report"], report)
        out = StepOutput(report=Path(d["report"]))
        note = (f"LD clock {fit.rate_hz:.4f} Hz ({fit.rate_ppm:+.1f} ppm ±{fit.slope_stderr_ppm:.1f}), WAV0 at master "
                f"frame {wav0:.2f} (±{fit.intercept_stderr_ms:.1f} ms); {fit.windows_used} windows")
        out.measure("audio.rate_method", RATE_METHOD, note=note)
        out.measure("audio.asetrate", rate, note="exact applied rate (DVD-referenced clock, A/V correction included)")
        out.measure("audio.resampler", built["resample"]["resampler"])
        out.measure("audio.skip_s", skip)
        out.measure("audio.skip_method", "video.delta_s − DVD-referenced WAV0 master time (+ A/V correction)",
                    note="measured by dvd-ld-audio against the DVD stereo through the accepted picture alignment")
        out.measure("audio.wav0_master_frame", wav0, note=note)
        out.measure("audio.dvd_clock", {k: v for k, v in fit.summary().items() if k != "windows"} | {"cross_checks": cross})
        dur = float(built["corrected"]["stream"].get("duration") or 0.0)
        if dur > 0:
            out.measure("audio.corrected_duration_s", dur)
        out.measure("deliverable.ld_audio_source", "ld_synced")
        audio_root, reports_root = ctx.root("audio"), ctx.root("reports")
        out.artifacts["ld_corrected"] = {"root": "audio", "name": Path(d["corrected"]).relative_to(audio_root).as_posix()}
        out.artifacts["ld_synced"] = {"root": "audio", "name": Path(d["synced"]).relative_to(audio_root).as_posix()}
        out.artifacts["audio_timing_report"] = {"root": "reports",
                                                "name": Path(d["report"]).relative_to(reports_root).as_posix()}
        return out

    def verify(self, ctx, plan, output):
        d, rep = plan.data, read_json(plan.data["report"])
        issues = []
        for key in ("corrected", "synced"):
            p = Path(d[key])
            if not p.is_file() or sha256_file(p) != rep[key]["output_sha256"]:
                issues.append(f"{p.name} missing or changed after the report was written")
        if rep["resample"]["synced_samples"] != rep["target_samples"]:
            issues.append("synced sample count differs from the target")
        return issues


