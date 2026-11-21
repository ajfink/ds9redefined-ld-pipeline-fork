"""``dvd-free-audio``: the operator-chosen no-DVD LD audio route (v38 ``audio_timing_diagnostic.py
--promote-dvd-free``).

Without a DVD there is no external reference for the LD audio clock or its absolute offset. v38's
validated substitute, ported unchanged:

* **Rate from duration.** The native EFM WAV spans the same disc time as the full stack, so
  ``rate = 44100 × audio_duration / stack_duration``; applied as ``aresample 88200 → asetrate
  (2×rate, rounded) → aresample 44100`` (soxr precision 28 when FFmpeg has it).
* **Endpoint check.** The audio's frame-equivalent length must equal the stack minus the PAD frames
  that land inside the output, within ±1 frame — otherwise the duration model is not trusted and the
  stage stops (blocked) before writing anything.
* **Origin from capture-pad geometry.** WAV sample 0 sits ``audio.pad_frames`` master frames into the
  stack (Orc's alignment map for the selected capture), so ``AudioSkip = video.delta_s −
  pad_frames / (30000/1001)``.
* **Exact sample budget.** The synced FLAC is trimmed/padded to exactly
  ``round(video.frames × 1001 × 44100 / 24000)`` samples.

The stage runs only when the operator recorded the no-DVD choice (``mode.dvd_reference_available``
false and ``mode.no_dvd_proceed_approved`` true) and the deliverable is LD stereo only. The DVD and
any picture-alignment path are never opened. Outputs are new files per run (never overwritten); the
timing report is the evidence the ``qc-report`` DVD-free gate re-verifies.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.hashing import sha256_file
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media.audio_timing import (
    BASE_RATE,
    INTERMEDIATE_FACTOR,
    NO_DVD_PAD_TOLERANCE_FRAMES,
    no_dvd_audio_plan_gib,
    rate_filter,
    sample_plan,
    summarize_timing,
    wav_info,
)
from fstk.qc.dvdfree import RATE_METHOD, REPORT_SCHEMA, exact_audio_samples, expected_skip
from fstk.tools.orc_project import reference_map_ranges, source_id_for_capture

from .base import ArtifactExists, Custom, SpaceNeed, Stage, StepOutput, StepPlan, ValueIs, ValueSet, register


def _ld_only(ctx, manifest):
    tracks = manifest.get("deliverable.tracks")
    ok = tracks in (None, ["ld"])
    return ok, "LD stereo only" if ok else f"deliverable.tracks={tracks}: DVD 5.1/SRT are unavailable without a DVD"


def project_pads(pads: list[int], *, source_pad: int, head_trim: int, out_frames: int) -> dict[str, int]:
    """Map reference-map PAD master frames into the output timeline (v38 projection, verbatim)."""
    mapped = [((int(m) - int(source_pad)) * 4 // 5 - head_trim) for m in pads]
    return {"projected": sum(0 <= i < out_frames for i in mapped),
            "trimmed_before_head": sum(i < 0 for i in mapped),
            "after_output_tail": sum(i >= out_frames for i in mapped)}


@register
class DvdFreeAudio(Stage):
    id = "dvd-free-audio"
    title = "LD audio without a DVD (duration/PAD rate + capture-pad origin)"
    description = ("Operator-chosen no-DVD route: rate from native audio vs stack duration, ±1-frame PAD endpoint "
                   "check, AudioSkip from capture-pad geometry, exact output sample budget.")
    requires = (ValueIs("mode.dvd_reference_available", False), ValueIs("mode.no_dvd_proceed_approved", True),
                Custom("ld-only", _ld_only), ValueSet("audio.capture"), ValueSet("audio.pad_frames"),
                ValueSet("stack.reference"), ValueSet("stack.frames"), ValueSet("video.frames"),
                ValueSet("video.head_trim"), ValueSet("video.delta_s"), ArtifactExists("ld_native"),
                ArtifactExists("stack_project"))
    writes = ("audio.rate_method", "audio.duration_candidate_rate_hz", "audio.intermediate_asetrate_hz",
              "audio.asetrate", "audio.resampler", "audio.skip_s", "audio.skip_method", "audio.wav0_master_frame",
              "audio.endpoint_residual_frames", "audio.endpoint_tolerance_frames", "audio.origin_verified_by_duration",
              "audio.absolute_av_reference_available", "audio.dvd_free_approved", "audio.corrected_duration_s",
              "deliverable.ld_audio_source", "deliverable.tracks", "deliverable.mux_tracks")
    writes_artifacts = ("ld_corrected", "ld_synced", "audio_timing_report")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        source = ctx.artifact_path("ld_native", m)
        project = ctx.artifact_path("stack_project", m)
        audio = wav_info(source)
        if audio["sample_rate"] != BASE_RATE or audio["channels"] != 2 or audio["sample_width_bytes"] != 2:
            raise GateError(f"expected native 44.1 kHz/16-bit stereo EFM WAV, got {audio}")
        source_id = source_id_for_capture(project, m.require("stack.reference"))
        pads = reference_map_ranges(project, source_id=source_id)
        if not pads:
            raise GateError("the reference frame map has no PAD_1 positions; the duration/PAD model does not apply")
        frames, head_trim = int(m.require("video.frames")), int(m.require("video.head_trim"))
        pad_frames = int(m.require("audio.pad_frames"))
        proj = project_pads(pads, source_pad=pad_frames, head_trim=head_trim, out_frames=frames)
        timing = summarize_timing(BASE_RATE, audio["frames"], int(m.require("stack.frames")), len(pads),
                                  proj["projected"])
        residual = float(timing["audio_minus_projected_content_frames"])
        if abs(residual) > NO_DVD_PAD_TOLERANCE_FRAMES:
            raise GateError(f"duration/PAD endpoint residual {residual:+.3f} frames exceeds the "
                            f"±{NO_DVD_PAD_TOLERANCE_FRAMES:.1f}-frame no-DVD limit; the duration model is not "
                            "trusted for this episode (nothing was written)")
        rate = timing["duration_candidate_effective_rate_hz"]
        if not BASE_RATE * 0.98 <= rate <= BASE_RATE * 1.02:
            raise GateError(f"duration rate {rate:.3f} Hz is outside the 2 % sanity range")
        rate_2x = round(INTERMEDIATE_FACTOR * rate)
        skip = expected_skip(m.require("video.delta_s"), pad_frames)
        if skip < 0:
            raise GateError(f"DVD-free AudioSkip is negative ({skip:.6f}s); inspect the origin convention")
        start, target = sample_plan(skip, frames)
        run = f"{utc_stamp()[:15].lower()}-{secrets.token_hex(3)}"
        audio_dir = ctx.root("audio")
        corrected = audio_dir / f"{ctx.episode.code}-LD20-dvd-free-{rate_2x}-corrected-{run}.flac"
        synced = audio_dir / f"{ctx.episode.code}-LD20-dvd-free-synced-{run}.flac"
        report = ctx.root("reports") / f"dvd-free-audio-timing-{run}.json"
        planned = no_dvd_audio_plan_gib(source.stat().st_size)
        return StepPlan(self.id, params, inputs=[source, project], outputs=[corrected, synced, report],
                        space=SpaceNeed("audio", planned, "3 × native EFM WAV size (corrected + synced + temp)"),
                        summary=(f"rate {rate:.4f} Hz (2× asetrate {rate_2x}), endpoint residual {residual:+.3f} frames, "
                                 f"AudioSkip {skip:.6f}s, {target} samples"),
                        data={"source": str(source), "project": str(project), "audio": audio, "pads": pads,
                              "projection": proj, "timing": timing, "rate_2x": rate_2x, "skip": skip,
                              "start": start, "target": target, "corrected": str(corrected), "synced": str(synced),
                              "report": str(report), "run": run, "frames": frames, "head_trim": head_trim,
                              "pad_frames": pad_frames, "source_id": source_id})

    # -- FFmpeg passes
    @staticmethod
    def _encode(ff, source: Path, out: Path, af: str, *, compression: list[str], cancel) -> dict:
        if out.exists():
            raise ArtifactError(f"refusing to overwrite {out}")
        tmp = out.with_name(out.stem + ".tmp.flac")
        if tmp.exists():
            raise ArtifactError(f"temporary output exists; inspect it before retrying: {tmp}")
        out.parent.mkdir(parents=True, exist_ok=True)
        args = ["-n", "-v", "error", "-i", str(source), "-map", "0:a:0", "-af", af, "-c:a", "flac", *compression,
                "-sample_fmt", "s16", "-ar", str(BASE_RATE), "-f", "flac", str(tmp)]
        res = ff.run(args, name=out.stem, cancel=cancel)
        if res.returncode != 0 or not tmp.is_file():
            raise ArtifactError(f"FFmpeg failed building {out.name} ({res.describe()}); any partial output is kept "
                                f"at {tmp}")
        stream = ff.probe(tmp).only("audio").raw
        if str(stream.get("sample_rate")) != str(BASE_RATE) or int(stream.get("channels") or 0) != 2:
            raise ArtifactError(f"{tmp.name} has an unexpected stream format: {stream}")
        tmp.rename(out)
        return {"path": str(out), "filter": af, "ffmpeg_args": args, "stream": stream,
                "output_sha256": sha256_file(out)}

    def run(self, ctx, plan):
        d = plan.data
        ff = ctx.tool("ffmpeg")
        source = Path(d["source"])
        use_soxr = ff.supports_soxr()
        rate_af, resampler = rate_filter(d["rate_2x"], use_soxr)
        ctx.report(0.05, f"rate-corrected FLAC ({resampler})")
        corrected = self._encode(ff, source, Path(d["corrected"]), rate_af, compression=[], cancel=ctx.cancel)
        ctx.check_cancel()
        ctx.report(0.5, "sample-synced FLAC")
        synced_af = (f"{rate_af},atrim=start_sample={d['start']},asetpts=PTS-STARTPTS,"
                     f"apad=whole_len={d['target']},atrim=end_sample={d['target']}")
        synced = self._encode(ff, source, Path(d["synced"]), synced_af, compression=["-compression_level", "8"],
                              cancel=ctx.cancel)
        actual = exact_audio_samples(synced["stream"])
        if actual != d["target"]:
            raise ArtifactError(f"synced audio has {actual} samples; the video needs exactly {d['target']}")
        m = ctx.manifest
        report = {
            "schema": REPORT_SCHEMA, "created_utc": utc_now_iso(), "episode": str(ctx.episode), "run": d["run"],
            **d["timing"], "is_approval": False,
            "approval_basis": "operator's recorded no-DVD choice (mode.no_dvd_proceed_approved)",
            "reference_capture": m.get("stack.reference"), "selected_audio_capture": m.get("audio.capture"),
            "selected_capture_alignment_pad_frames": d["pad_frames"], "reference_frame_map_source_id": d["source_id"],
            "full_map_pad_positions_master_frames": d["pads"],
            "map_pads_trimmed_before_output_head": d["projection"]["trimmed_before_head"],
            "map_pads_after_output_tail": d["projection"]["after_output_tail"],
            "measured_head_trim_output_frames": d["head_trim"], "measured_output_frames": d["frames"],
            "measured_delta_s": m.get("video.delta_s"), "source_audio": str(source),
            "source_audio_sha256": sha256_file(source), "source_audio_pcm": d["audio"],
            "stack_project": d["project"], "resampler_backend": resampler, "dvd_opened": False,
            "intermediate_asetrate_hz": d["rate_2x"], "effective_rate_hz": d["rate_2x"] / INTERMEDIATE_FACTOR,
            "corrected": corrected,
            "synced": {**synced, "skip_s": d["skip"], "start_sample": d["start"], "target_samples": d["target"],
                       "skip_method": "measured LD output-frame delta minus selected-capture Orc pad_frames/FPS; "
                                      "no DVD reference"},
            "external_absolute_av_reference_available": False,
            "caveat": ("The duration/PAD and capture-origin model sets the audio timeline and exact endpoint; an "
                       "external absolute A/V cross-correlation is unavailable without a DVD/reference."),
        }
        atomic_write_json(d["report"], report)
        corrected_s = float(corrected["stream"].get("duration") or 0.0)
        out = StepOutput(report=Path(d["report"]))
        out.data["report"] = report
        choice = "operator chose the no-DVD route (mode.no_dvd_proceed_approved); stage applied v38's duration/PAD recipe"
        out.measure("audio.rate_method", RATE_METHOD, note=choice)
        out.measure("audio.duration_candidate_rate_hz", d["timing"]["duration_candidate_effective_rate_hz"])
        out.measure("audio.intermediate_asetrate_hz", float(d["rate_2x"]))
        out.measure("audio.asetrate", d["rate_2x"] / INTERMEDIATE_FACTOR)
        out.measure("audio.resampler", resampler)
        out.measure("audio.skip_s", d["skip"])
        out.measure("audio.skip_method", "dvd-free capture-pad geometry", note=choice)
        out.measure("audio.wav0_master_frame", float(d["pad_frames"]))
        out.measure("audio.endpoint_residual_frames", float(d["timing"]["audio_minus_projected_content_frames"]))
        out.measure("audio.endpoint_tolerance_frames", float(NO_DVD_PAD_TOLERANCE_FRAMES))
        out.measure("audio.origin_verified_by_duration", True)
        out.measure("audio.absolute_av_reference_available", False)
        out.measure("audio.dvd_free_approved", True, note=choice)
        if corrected_s > 0:
            out.measure("audio.corrected_duration_s", corrected_s)
        out.measure("deliverable.ld_audio_source", "ld_synced")
        out.measure("deliverable.tracks", ["ld"])
        out.measure("deliverable.mux_tracks", ["ld"])
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
        if sha256_file(Path(d["source"])) != rep["source_audio_sha256"]:
            issues.append("native EFM WAV changed during the build")
        return issues
