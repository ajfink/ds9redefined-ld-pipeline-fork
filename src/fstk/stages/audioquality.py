"""``audio-quality``: measure the delivered audio and make the only repairs the owner allowed.

* LD stereo: clicks and digital gaps are detected per channel; a *short* gap or a single click that
  the signal demonstrably continues past is filled by linear prediction from both sides (written as a new
  24-bit FLAC, bit-identical everywhere else); anything longer or ambiguous is reported and left as captured;
* channel/phase/layout, loudness (EBU R128, true peak) and the DVD 5.1 codec path are measured and
  reported; loudness is never normalised and dynamic range is never reduced;
* retimed subtitles are checked against the speech envelope of the delivered LD audio.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media.remux import tracks_from_settings
from fstk.qc.gates import parse_srt
from fstk.quality import audio as qa

from .base import Stage, StepOutput, StepPlan, ValueSet, register

SCHEMA = "fstk-audio-quality-v1"
CHUNK_S = 60.0
OVERLAP_S = 0.5
WINDOWS = 3
WINDOW_S = 90.0
LABELS_51 = ["FL", "FR", "FC", "LFE", "BL", "BR"]
LOSSLESS = {"flac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "truehd", "alac"}


@register
class AudioQuality(Stage):
    id = "audio-quality"
    title = "Audio quality: clicks/gaps, channels/phase, loudness, DVD 5.1 path, subtitle alignment"
    description = ("Detects clicks and digital gaps in the LD stereo (fills only short, clearly-defective ones by "
                   "prediction, bit-identical elsewhere), checks channels/phase/layout, measures R128 loudness and "
                   "true peak without normalising, and verifies subtitle timing against speech.")
    requires = (ValueSet("deliverable.ld_audio_source"), ValueSet("deliverable.mux_tracks"), ValueSet("video.duration_s"))
    writes = ("quality.audio_events_found", "quality.audio_repaired", "quality.ld_lufs", "quality.ld_lra",
              "quality.ld_true_peak", "quality.fiveone_lufs", "quality.subtitle_offset_s")
    writes_artifacts = ("ld_audio_finished", "audio_quality_report")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        want_51, want_srt = tracks_from_settings(m.require("deliverable.mux_tracks"))
        key = m.require("deliverable.ld_audio_source")           # never the finished file: re-runs start from source
        ld = ctx.artifact_path(key, m)
        inputs = {"ld": ld}
        if want_51:
            inputs["fiveone"] = ctx.artifact_path("retimed_51", m)
        if want_srt:
            inputs["srt"] = ctx.artifact_path("retimed_srt", m)
        missing = [str(p) for p in inputs.values() if not p.is_file()]
        if missing:
            raise GateError(f"audio-quality inputs missing: {missing}")
        run = utc_stamp()[:15].lower()
        out = {"finished": ctx.root("audio") / f"{ctx.episode.code}-LD20-finished-{run}.flac",
               "report": ctx.root("reports") / f"audio-quality-{run}.json"}
        for p in out.values():
            if p.exists():
                raise GateError(f"refusing to overwrite {p}")
        return StepPlan(self.id, params, inputs=list(inputs.values()), outputs=[out["report"]],
                        summary="audio measurements and short-defect repair",
                        data={**{k: str(v) for k, v in inputs.items()}, **{f"out_{k}": str(v) for k, v in out.items()},
                              "duration": m.require("video.duration_s"), "repair": bool(params.get("repair", True))})

    # ------------------------------------------------------------------ LD stereo scan / repair
    def _scan_ld(self, ctx, ff, ld: Path, total_s: float, repair: bool, raw: Path):
        events, repaired, skipped = [], [], []
        rate = 48000
        t, written = 0.0, 0
        with raw.open("wb") as fh:
            while t < total_s - 1e-6:
                lo = max(0.0, t - OVERLAP_S)
                x = ff.pcm_f32(ld, "0:a:0", 2, start_s=lo, duration_s=CHUNK_S + OVERLAP_S + (t - lo), sample_rate=rate,
                               cancel=ctx.cancel)
                first, last = round((t - lo) * rate), min(len(x), round((t - lo + CHUNK_S) * rate))
                found: list[qa.Event] = []
                for ch in (0, 1):
                    for e in qa.find_events(x[:, ch], rate, ch):
                        if first <= e.start < last:
                            found.append(e)
                y = x.copy()
                if repair:
                    for ch in (0, 1):
                        evs = [e for e in found if e.channel == ch]
                        y[:, ch], done, left = qa.repair(x[:, ch], evs, rate)
                        repaired += [(lo, e) for e in done]
                        skipped += [(lo, e, why) for e, why in left]
                events += [(lo, e) for e in found]
                fh.write(y[first:last].astype("<f4").tobytes())
                written += last - first
                ctx.report(min(0.7, 0.7 * t / max(total_s, 1.0)), f"audio scan {t:.0f}/{total_s:.0f} s")
                t += CHUNK_S
        return events, repaired, skipped, written

    def run(self, ctx, plan):
        d = plan.data
        ff = ctx.tool("ffmpeg")
        ld = Path(d["ld"])
        info = ff.probe(ld).only("audio")
        total_s = float(ff.probe(ld).duration_s or d["duration"])
        raw = Path(d["out_report"]).with_suffix(".raw")
        events, repaired, skipped, _written = self._scan_ld(ctx, ff, ld, total_s, d["repair"], raw)

        report: dict = {"schema": SCHEMA, "created_utc": utc_now_iso(), "episode": str(ctx.episode),
                        "ld": {"codec": info.raw.get("codec_name"), "events_found": len(events), "repaired": len(repaired),
                               "left_as_captured": [{"time_s": round(b + e.start / 48000, 3), "kind": e.kind,
                                                     "amount": round(e.amount, 2), "why": why} for b, e, why in skipped][:300]}}
        finished = None
        if repaired:
            ctx.report(0.75, "writing repaired LD audio (24-bit FLAC)")
            finished = Path(d["out_finished"])
            tmp = finished.with_name(finished.stem + ".tmp.flac")
            res = ff.run(["-y", "-v", "error", "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", str(raw), "-c:a", "flac",
                          "-compression_level", "8", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", "-f", "flac",
                          str(tmp)], name="audio-finish", cancel=ctx.cancel)
            if not tmp.is_file():
                raise ArtifactError(f"could not write the repaired LD audio ({res.describe()})")
            tmp.replace(finished)
            report["ld"]["finished_file"] = str(finished)
        raw.unlink(missing_ok=True)

        # ---- channels, loudness
        ld_for_measure = finished or ld
        findings = []
        starts = np.linspace(0, max(total_s - WINDOW_S, 0), WINDOWS)
        for s0 in starts:
            w = ff.pcm_f32(ld_for_measure, "0:a:0", 2, start_s=float(s0), duration_s=WINDOW_S, sample_rate=48000)
            findings.append(qa.channel_findings(w, ["L", "R"]))
        report["ld"]["channels"] = findings[len(findings) // 2]
        loud = qa.parse_ebur128(ff.ebur128(ld_for_measure))
        report["ld"]["loudness"] = loud
        out = StepOutput(report=Path(d["out_report"]))
        issues: list[str] = []
        if "fiveone" in d:
            five = Path(d["fiveone"])
            fi = ff.probe(five).only("audio")
            lossless = fi.raw.get("codec_name") in LOSSLESS
            f51: dict = {"codec": fi.raw.get("codec_name"), "channel_layout": fi.raw.get("channel_layout"),
                         "path": "decoded once from the DVD, exact-ratio resampled, written lossless "
                                 "(no second lossy generation); not passthrough because the time base changes",
                         "lossless_delivery": lossless,
                         "layout_ok": str(fi.raw.get("channel_layout", "")).startswith("5.1")}
            wins = []
            for s0 in starts:
                six = ff.pcm_f32(five, "0:a:0", 6, start_s=float(s0), duration_s=WINDOW_S, sample_rate=48000)
                wins.append(six)
            six = wins[len(wins) // 2]
            f51["channels"] = qa.channel_findings(six, LABELS_51)
            f51["lfe"] = qa.lfe_findings(six[:, 3], 48000)
            ldw = ff.pcm_f32(ld_for_measure, "0:a:0", 2, start_s=float(starts[len(starts) // 2]), duration_s=WINDOW_S,
                             sample_rate=48000)
            f51["ld_vs_front_pair"] = qa.swap_hint(ldw, six[:, :2])
            f51["loudness"] = qa.parse_ebur128(ff.ebur128(five))
            report["fiveone"] = f51
            if f51["loudness"]:
                out.measure("quality.fiveone_lufs", f51["loudness"]["integrated_lufs"])
        if "srt" in d:
            cues = list(zip(*parse_srt(Path(d["srt"]).read_text(encoding="utf-8", errors="replace"))[1:3], strict=True))
            mono = ff.pcm(ld_for_measure, "0:a:0", 8000)
            sub = qa.subtitle_offset(cues, qa.speech_envelope(mono, 8000)) if cues else {"status": "SKIP"}
            report["subtitles"] = sub
            if "best_shift_s" in sub:
                out.measure("quality.subtitle_offset_s", float(sub["best_shift_s"]))
        flags = [r for sec in (report["ld"].get("channels", []), report.get("fiveone", {}).get("channels", []))
                 for r in sec if r.get("status") == "FLAG"]
        report["flags"] = flags + ([{"check": "LD vs DVD front pair", **report["fiveone"]["ld_vs_front_pair"]}]
                                   if report.get("fiveone", {}).get("ld_vs_front_pair", {}).get("status") == "FLAG" else [])
        atomic_write_json(d["out_report"], report)
        out.measure("quality.audio_events_found", len(events))
        out.measure("quality.audio_repaired", len(repaired))
        if loud:
            out.measure("quality.ld_lufs", loud["integrated_lufs"])
            out.measure("quality.ld_lra", loud["lra_lu"])
            out.measure("quality.ld_true_peak", loud["true_peak_dbfs"])
        out.artifacts["audio_quality_report"] = {"root": "reports", "name": Path(d["out_report"]).name}
        if finished:
            out.artifacts["ld_audio_finished"] = {"root": "audio", "name": finished.name}
            if abs((ff.probe(finished).duration_s or 0) - total_s) > 0.002:
                issues.append("repaired LD audio differs in duration from its source")
        out.data["issues"] = issues
        return out

    def verify(self, ctx, plan, output):
        return list(output.data["issues"])
