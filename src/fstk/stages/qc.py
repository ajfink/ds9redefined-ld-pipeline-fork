"""``qc-report``: delivery QC of the final file, then a human delivery review.

Ported behaviour of v38 ``qc_report.py``. The stage always writes
the full evidence (``reports/qc-<run>.json`` and ``.md``), then *fails* unless every hard gate
passed and nothing required was left unverified — so a delivery approval can never be recorded
against a failing report. The structured JSON feeds the delivery review window
(``candidate_is_approval`` is always false: the human review decides).

Gates (v38 numbering in brackets):

* [1] full sequential decode with blackdetect: decoder errors fail; a single-frame black (a picture
  missing from every capture) is a warning listed for the A/V review; [7] the same decode's frame
  count == ``video.frames``;
* [2] (v38's PAD replacement identity is gone: the stack master has no PAD frames, ADR 0004);
* [3] absolute A/V against the DVD through the picture map (|constant| ≤ 100 ms, |slope| ≤ 30 ppm,
  rms ≤ 40 ms), when ``mode.dvd_reference_available`` is true (``fstk.qc.sync``);
* [4] between-track sync LD vs DVD 5.1 (rms ≤ 40 ms, |slope| ≤ 30 ppm) when 5.1 is delivered;
* [5] stream inventory (codec/profile/raster/rate, track order, defaults, languages, titles,
  container duration within 2 ms of ``video.duration_s``);
* [6] audio seekability: input-seek and output-seek control decodes at four timeline points for
  every audio track;
* [8] packet hashes: deliverable streams vs their sources and any recorded ``qc.*_md5``;
* [9] retimed SRT: cue count, sequential numbering, monotonic, inside the timeline;
* [10] retime output gaps (LIMIT stays WARN, never PASS);
* [11] manifest cross-check (warnings only).

* [3b] DVD-free timeline (no-DVD episodes): the delivered LD audio is exactly what ``dvd-free-audio``
  built and recorded — operator choice, duration rate and endpoint residual, capture-pad
  AudioSkip, native/corrected/synced hashes, exact sample budget (``fstk.qc.dvdfree``).
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import FstkError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.hashing import sha256_file
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.qc.dvdfree import check_dvd_free
from fstk.qc.gates import (
    STANDING_WARNINGS,
    Gate,
    allowed_skips,
    av_check_chunks,
    av_prescreen,
    black_review_intervals,
    expected_raster,
    render_markdown,
    retime_gap_status,
    retime_review_intervals,
    seekability,
    single_black_frames,
    stream_inventory,
    subtitle_timeline,
    summarize,
)
from fstk.qc.sync import (
    AV_SR,
    SYNC_SR,
    SYNC_WIN,
    OffsetModel,
    absolute_av,
    alignment_cuts,
    between_track_sync,
    break_gaps,
    fit_windows,
    more_sync_points,
    retime_switch_gaps,
    sync_sample_points,
)
from fstk.qc.sync import FPS as AV_FPS

from .base import Custom, Stage, StepOutput, StepPlan, ValueSet, register

QC_SCHEMA = "fstk-delivery-qc-v1"


def _deliverable_key(manifest) -> str:
    return manifest.get("deliverable.key") or "padfixed"


def _deliverable_present(ctx, manifest):
    key = _deliverable_key(manifest)
    try:
        p = ctx.artifact_path(key, manifest)
    except GateError as exc:
        return False, str(exc)
    return p.is_file(), str(p) if p.is_file() else f"deliverable {key} missing: {p}"


@register
class QcReport(Stage):
    id = "qc-report"
    title = "Delivery QC report"
    description = ("Full decode + single black frames, frame count, stream inventory, seekability, packet hashes, "
                   "subtitles and retime gaps; evidence for the human delivery review.")
    requires = (Custom("deliverable", _deliverable_present), ValueSet("video.frames"), ValueSet("video.duration_s"))
    writes = ("qc.video_md5", "qc.ld_audio_md5", "qc.fiveone_md5", "qc.srt_md5",
              "qc.full_decode_errors", "qc.delivered_bytes",
              "qc.av_constant_ms", "qc.av_slope_ppm", "qc.av_rms_ms", "qc.track_offset_ms")
    resource = "compute"
    review = "delivery"
    review_when = "the A/V check: several 15 s chunks marked in sync / audio early / audio late"

    def plan(self, ctx, params):
        m = ctx.manifest
        key = _deliverable_key(m)
        deliverable = ctx.artifact_path(key, m)
        run = utc_stamp().lower()
        base = ctx.root("reports") / f"qc-{run}"
        json_out, md_out = base.with_suffix(".json"), base.with_suffix(".md")
        for p in (json_out, md_out):
            if p.exists():
                raise GateError(f"refusing to overwrite QC report {p}; run again in a moment")
        return StepPlan(self.id, params, inputs=[deliverable], outputs=[json_out, md_out],
                        summary=f"QC {deliverable.name}",
                        data={"key": key, "deliverable": str(deliverable), "json": str(json_out),
                              "md": str(md_out), "run": run})

    # -- gates
    def _decode_gates(self, ctx, m, deliverable: Path, evidence: dict, review: list) -> tuple[list[Gate], dict]:
        ff = ctx.tool("ffmpeg")
        ctx.report(0.05, "full decode + blackdetect")
        from fstk.tools.progress_lines import LineProgress

        frames_expected = m.get("video.frames", None)
        scan = ff.blackdetect_decode(deliverable, cancel=ctx.cancel,
                                     progress=LineProgress(ctx.report, total_frames=frames_expected, base=0.05,
                                                           span=0.5, label="decoding"))
        ctx.check_cancel()
        measured: dict = {"full_decode_errors": len(scan.errors), "frames": scan.frames,
                          "black_events": list(scan.events)}
        gates = []
        name = "full decode + single black frames"
        if scan.frames is None:
            gates.append(Gate(name, "ERROR", f"decode produced no frame count (exit {scan.returncode})"))
        else:
            blips = single_black_frames(scan.events)
            measured["single_black_frames"] = len(blips)
            review += black_review_intervals(blips)
            detail = f"blackdetect events={len(scan.events)}; single black frames={len(blips)}"
            if scan.errors:
                gates.append(Gate(name, "FAIL", f"{len(scan.errors)} decoder error line(s): {scan.errors[:3]}; {detail}"))
            elif blips:
                gates.append(Gate(name, "WARN", f"single black frame(s) at {[h['frame'] for h in blips[:20]]}: a "
                                                f"picture missing from every capture; look at them in the A/V check "
                                                f"and fix with Frame fixes; {detail}", {"frames": blips}))
            else:
                gates.append(Gate(name, "PASS", f"no decoder errors and no single black frames; {detail}"))
        want = m.require("video.frames")
        if scan.frames is None:
            gates.append(Gate("frame count", "ERROR", "could not determine the frame count"))
        elif scan.frames == want:
            gates.append(Gate("frame count", "PASS", f"{scan.frames} == manifest video.frames"))
        else:
            gates.append(Gate("frame count", "FAIL", f"{scan.frames} != manifest {want}"))
        return gates, measured

    def _absolute_av_gate(self, ctx, m, deliverable: Path, measured: dict, review: list) -> Gate:
        import numpy as np

        name = "absolute A/V"
        try:
            for key in ("dvd", "align_npy"):
                if not m.artifact(key) or not ctx.artifact_path(key, m).is_file():
                    return Gate(name, "ERROR", f"artifact {key} is required for the absolute A/V gate")
            delta = float(m.require("video.delta_s"))
            breaks = m.get("retime.breaks") or []
            music = m.get("retime.merged_music_through") or []
            tail = m.get("retime.end_black_ld")
            blacks_master = [(float(b["ld_black"][0]), float(b["ld_black"][1]))
                             for b in [*breaks, *music] if b.get("ld_black")]
            cuts_from = "recorded LD blacks"
            if not blacks_master and not tail:
                # fstk's own retime (retime map) records no LD blacks: cut the offset model where the measured
                # DVD alignment path jumps instead
                cuts = alignment_cuts(m.get("video.dvd_alignment"))
                if cuts is None:
                    return Gate(name, "ERROR", "neither retime.breaks LD blacks nor video.dvd_alignment breaks "
                                               "are recorded; the offset model cannot be cut at the act breaks")
                blacks_master, cuts_from = cuts, f"{len(cuts)} DVD alignment break(s)"
            windows, _short = fit_windows(blacks_master, int(m.require("stack.frames")) / AV_FPS,
                                          tuple(tail) if tail else None,
                                          require_cuts=cuts_from == "recorded LD blacks")
            model = OffsetModel(np.load(ctx.artifact_path("align_npy", m)), windows)
            ff = ctx.tool("ffmpeg")
            ld_audio = ff.pcm(deliverable, "0:a:0", AV_SR, cancel=ctx.cancel)
            dvd_audio = ff.pcm(ctx.artifact_path("dvd", m), m.get("qc.dvd_audio_map") or "0:a:1", AV_SR,
                               cancel=ctx.cancel)
            blacks_out = sorted([(a - delta, z - delta) for a, z in blacks_master]
                                + ([(float(tail[0]) - delta, float(tail[1]) - delta)] if tail else []))
            result = absolute_av(ld_audio=ld_audio, dvd_audio=dvd_audio, delta_s=delta, offset=model,
                                 blacks_out=blacks_out)
        except FstkError as exc:
            return Gate(name, "ERROR", str(exc))
        measured["av"] = result
        detail = (f"constant {result['constant_ms']:+.1f} ms, slope {result['slope_ppm']:+.1f} ppm, "
                  f"rms {result['rms_ms']:.1f} ms over {len(result['rows'])} windows "
                  f"(gates ±100 ms, ±30 ppm, ≤40 ms); cut at {cuts_from}")
        result["cuts_from"] = cuts_from
        if not result["pass"]:
            for r in result["rows"]:
                review.append({"id": f"absolute-av-{len(review) + 1:03d}", "gate": name, "start_s": r["out_time_s"],
                               "end_s": r["out_time_s"] + r["window_duration_s"],
                               "reason": "A/V fit window from failed gate",
                               "metrics": {"av_ms": r["av_ms"], "corr": r["corr"]}})
            return Gate(name, "FAIL", detail, result)
        return Gate(name, "PASS", detail, result)

    def _dvd_free_gate(self, ctx, m, deliverable: Path) -> Gate:
        name = "DVD-free sync"
        try:
            for key in ("audio_timing_report", "ld_native", "ld_corrected", "ld_synced"):
                if not m.artifact(key) or not ctx.artifact_path(key, m).is_file():
                    return Gate(name, "ERROR", f"artifact {key} is missing; run the dvd-free-audio stage")
            report = read_json(ctx.artifact_path("audio_timing_report", m))
            ff = ctx.tool("ffmpeg")
            keys = ("mode.dvd_reference_available", "mode.no_dvd_proceed_approved", "deliverable.tracks",
                    "audio.dvd_free_approved", "audio.rate_method", "audio.origin_verified_by_duration",
                    "audio.duration_candidate_rate_hz", "audio.endpoint_tolerance_frames", "video.frames",
                    "video.delta_s", "audio.pad_frames", "audio.skip_s", "audio.av_correction")
            issues = check_dvd_free(
                {k: m.get(k) for k in keys}, report,
                native_sha256=sha256_file(ctx.artifact_path("ld_native", m)),
                corrected_sha256=sha256_file(ctx.artifact_path("ld_corrected", m)),
                synced_sha256=sha256_file(ctx.artifact_path("ld_synced", m)),
                synced_stream=ff.probe(ctx.artifact_path("ld_synced", m)).only("audio").raw,
                delivered_stream=ff.probe(deliverable).of_type("audio")[0].raw)
        except (FstkError, OSError, ValueError, KeyError, IndexError) as exc:
            return Gate(name, "ERROR", str(exc))
        if issues:
            return Gate(name, "FAIL", "; ".join(issues))
        return Gate(name, "PASS", "no-DVD timebase, capture-pad AudioSkip and exact endpoint sample budget verified "
                                  "(no external absolute A/V reference exists for this episode)",
                    {"report": str(ctx.artifact_path("audio_timing_report", m))})

    def _between_track_gate(self, ctx, m, deliverable: Path, measured: dict, review: list) -> Gate:
        name = "between-track sync"
        try:
            delta = float(m.require("video.delta_s"))
            gaps = break_gaps(m.get("retime.breaks") or [], delta)
            gaps_from = "recorded breaks"
            if not gaps and m.artifact("retime_map"):
                # fstk's own retime: keep the windows away from where the 5.1 changes source
                gaps = retime_switch_gaps(read_json(ctx.artifact_path("retime_map", m)))
                gaps_from = f"{len(gaps)} retime switch(es)"
            points = sync_sample_points(gaps, float(m.require("video.duration_s")))
            ff = ctx.tool("ffmpeg")
            def grab(sel, t):
                return ff.pcm(deliverable, sel, SYNC_SR, start_s=t, duration_s=SYNC_WIN, cancel=ctx.cancel)

            result = between_track_sync(points, grab)
            first = result["slope"]
            if first["verdict"] == "marginal" or first["ambiguous"]:
                # the slope is within its own uncertainty of the limit: measure more windows, then decide.
                # The limit itself is never relaxed.
                extra = more_sync_points(gaps, float(m.require("video.duration_s")), points)
                if extra:
                    ctx.report(0.9, f"between-track slope is marginal ({first['headroom_ppm']:+.1f} ppm of headroom): "
                                    f"measuring {len(extra)} more windows")
                    first_slope = result["slope_ppm"]
                    result = between_track_sync(sorted([*points, *extra]), grab)
                    result["remeasured"] = {"first": {"windows": len(points), "slope_ppm": first_slope,
                                                      "stderr_ppm": first["stderr_ppm"], "verdict": first["verdict"]},
                                            "windows": len(points) + len(extra)}
        except FstkError as exc:
            return Gate(name, "ERROR", str(exc))
        measured["track_offset_ms"] = result["constant_ms"]
        sv = result["slope"]
        sigma = f" ±{sv['stderr_ppm']:.1f}" if sv["stderr_ppm"] is not None else ""
        detail = (f"lag {result['constant_ms']:+.1f} ms + {result['slope_ppm']:+.1f}{sigma} ppm·t, rms {result['rms_ms']:.1f} ms "
                  f"over {len(result['rows'])} windows away from {gaps_from} "
                  f"(gates ≤40 ms, ±30 ppm; constant offset is expected)"
                  + ("; measured again with more windows because the first fit was marginal" if "remeasured" in result else "")
                  + (f"; MARGINAL: {sv['headroom_ppm']:.1f} ppm of headroom" if sv["verdict"] == "marginal" else ""))
        if not result["pass"]:
            for r in result["rows"]:
                review.append({"id": f"track-sync-{len(review) + 1:03d}", "gate": name, "start_s": r["time_s"],
                               "end_s": r["time_s"] + 8.0, "reason": "between-track correlation window from failed gate",
                               "metrics": {"lag_s": r["lag_s"], "corr": r["corr"]}})
            return Gate(name, "FAIL", detail, result)
        return Gate(name, "PASS", detail, result)

    def _hash_gates(self, ctx, m, deliverable: Path, tracks: list[str]) -> tuple[list[Gate], dict]:
        ff = ctx.tool("ffmpeg")
        gates, hashes = [], {}

        def source(key: str | None) -> Path | None:
            if not key or not m.artifact(key):
                return None
            p = ctx.artifact_path(key, m)
            return p if p.is_file() else None

        try:
            vh = ff.packet_md5(deliverable, "0:v:0")
            rec = (m.get("qc.video_md5") or "").lower()
            gates.append(Gate("video packet hash", "FAIL" if rec and vh != rec else "PASS",
                              f"{vh} != manifest {rec}" if rec and vh != rec else vh))
            hashes["video"] = vh
            ldh = ff.packet_md5(deliverable, "0:a:0")
            ld_key = "ld_audio_finished" if m.artifact("ld_audio_finished") else m.get("deliverable.ld_audio_source")
            delivered = {t["role"]: Path(t["file"]) for t in (m.get("deliverable.audio_layout") or [])
                         if t.get("file") and Path(t["file"]).is_file()}
            src = delivered.get("ld") or source(ld_key)
            src_h = ff.packet_md5(src, "0:a:0") if src else None
            rec = (m.get("qc.ld_audio_md5") or "").lower()
            if src_h and ldh != src_h:
                gates.append(Gate("LD audio hash", "FAIL", f"{ldh} != source {src_h}"))
            elif rec and ldh != rec:
                gates.append(Gate("LD audio hash", "FAIL", f"{ldh} != manifest {rec}"))
            elif not src_h and not rec:
                gates.append(Gate("LD audio hash", "ERROR", "neither the LD audio source nor a recorded hash is "
                                                            "available to compare"))
            else:
                gates.append(Gate("LD audio hash", "PASS", ldh + (" (source match)" if src_h else " (manifest match)")))
            hashes["ld"] = ldh
            if "51" in tracks:
                src = delivered.get("51") or source("retimed_51")
                if src is None:
                    raise FstkError("retimed DVD 5.1 source missing")
                a, b = ff.packet_md5(deliverable, "0:a:1"), ff.packet_md5(src, "0:a:0")
                rec = (m.get("qc.fiveone_md5") or "").lower()
                ok = a == b and (not rec or a == rec)
                gates.append(Gate("DVD 5.1 hash", "PASS" if ok else "FAIL",
                                  f"out={a} src={b}" + (f" manifest={rec}" if rec else "")))
                hashes["fiveone"] = a
            compat = next((i for i, t in enumerate(m.get("deliverable.audio_layout") or [])
                           if t["role"] == "compat"), None)
            if compat is not None:
                src = delivered.get("compat")
                a = ff.packet_md5(deliverable, f"0:a:{compat}")
                b = ff.packet_md5(src, "0:a:0") if src else None
                gates.append(Gate("compatibility audio hash", "PASS" if a == b else ("FAIL" if b else "ERROR"),
                                  f"out={a} src={b}" if b else "the encoded compatibility track is missing"))
                hashes["compat"] = a
            if "srt" in tracks:
                src = source("retimed_srt")
                if src is None:
                    raise FstkError("retimed SRT source missing")
                a, b = ff.packet_md5(deliverable, "0:s:0"), ff.packet_md5(src, "0:s:0")
                rec = (m.get("qc.srt_md5") or "").lower()
                ok = a == b and (not rec or a == rec)
                gates.append(Gate("SRT hash", "PASS" if ok else "FAIL",
                                  f"out={a} src={b}" + (f" manifest={rec}" if rec else "")))
                hashes["srt"] = a
        except FstkError as exc:
            gates.append(Gate("packet hashes", "ERROR", str(exc)))
        return gates, hashes

    def run(self, ctx, plan):
        m = ctx.manifest
        d = plan.data
        deliverable = Path(d["deliverable"])
        tracks = list(m.get("deliverable.tracks") or ["ld"])
        layout = m.get("deliverable.audio_layout")
        if layout:          # what the remux actually delivered under the output settings
            tracks = ["ld", *(["51"] if any(t["role"] == "51" for t in layout) else []),
                      *(["srt"] if "srt" in tracks else [])]
        dvd = m.get("mode.dvd_reference_available")
        duration = float(m.require("video.duration_s"))
        evidence: dict = {}
        review: list[dict] = []
        gates, measured = self._decode_gates(ctx, m, deliverable, evidence, review)
        if dvd is True:
            ctx.report(0.4, "absolute A/V against the DVD")
            gates.append(self._absolute_av_gate(ctx, m, deliverable, measured, review))
        elif dvd is False:
            gates.append(Gate("absolute A/V", "SKIP", "no DVD reference: an external absolute A/V "
                                                      "cross-correlation is unavailable"))
            ctx.report(0.4, "DVD-free timeline")
            gates.append(self._dvd_free_gate(ctx, m, deliverable))
        else:
            gates.append(Gate("absolute A/V", "SKIP", "mode.dvd_reference_available is not recorded"))
        if "51" in tracks:
            ctx.report(0.5, "between-track sync")
            gates.append(self._between_track_gate(ctx, m, deliverable, measured, review))
        else:
            gates.append(Gate("between-track sync", "SKIP", "no DVD 5.1 track in deliverable.tracks"))
        ctx.report(0.6, "stream inventory")
        ff = ctx.tool("ffmpeg")
        info = ff.probe(deliverable)
        raw = [s.raw for s in info.streams]
        encoding = m.get("deliverable.video_encoding") or None
        inventory = stream_inventory(raw, info.format, tracks, duration,
                                     (encoding or {}).get("chroma") or m.get("mode.chroma_output"),
                                     video_expected=encoding, audio_layout=layout,
                                     raster=expected_raster(m.get("quality.crop")))
        gates.append(inventory)
        audio_count = sum(1 for s in raw if s.get("codec_type") == "audio")
        ctx.report(0.7, "audio seekability")
        gates.append(seekability(float(info.format.get("duration") or 0.0), audio_count,
                                 lambda s, at, w, inp: ff.audio_window_seconds(deliverable, s, at, w, input_seek=inp)))
        ctx.report(0.8, "packet hashes")
        hash_gates, hashes = self._hash_gates(ctx, m, deliverable, tracks)
        gates += hash_gates
        if "srt" not in tracks:
            gates.append(Gate("subtitle timeline", "SKIP", "no subtitle track in deliverable.tracks"))
        else:
            srt = ctx.artifact_path("retimed_srt", m) if m.artifact("retimed_srt") else None
            text = srt.read_text(encoding="utf-8-sig") if srt and srt.is_file() else None
            gates.append(subtitle_timeline(text, duration, m.get("retime.srt_cues")))
        gap_path = ctx.root("reports") / "retime-audio-verification.json"
        gap_report = None
        if "51" in tracks and gap_path.is_file():
            evidence["retime_gap_json"] = str(gap_path)
            try:
                gap_report = read_json(gap_path)
            except (OSError, ValueError) as exc:
                gates.append(Gate("retime output gaps", "ERROR", f"cannot read retime gap report: {exc}"))
        if not any(g.name == "retime output gaps" for g in gates):
            if gap_report is not None and gap_report.get("episode") not in (None, ctx.episode.code, str(ctx.episode)):
                gates.append(Gate("retime output gaps", "ERROR", "retime gap report episode identity mismatch"))
            else:
                gates.append(Gate("retime output gaps", *retime_gap_status(gap_report, "51" in tracks)))
                review += retime_review_intervals(gap_report)
        notes = []
        size = inventory.metrics.get("size_bytes")
        rec_bytes = m.get("qc.delivered_bytes")
        if rec_bytes and size != rec_bytes:
            notes.append(f"size {size} != recorded {rec_bytes}")
        gates.append(Gate("manifest cross-check", "WARN" if notes else "PASS",
                          "; ".join(notes) if notes else "recorded size/hash/counters agree"))

        allowed = allowed_skips(dvd_available=dvd, tracks=tracks)
        summary = summarize(gates, allowed)
        chunks = av_check_chunks(float(info.format.get("duration") or duration), measured.get("black_events", []))
        ctx.report(0.9, "H.264 review proxies for the A/V check")
        self._make_proxies(ctx, ff, deliverable, chunks, d["run"])
        result = {"schema": QC_SCHEMA, "episode": str(ctx.episode), "title": m.title, "created_utc": utc_now_iso(),
                  "run": d["run"], "deliverable_key": d["key"], "deliverable_path": str(deliverable),
                  "deliverable_sha256": sha256_file(deliverable), "deliverable_bytes": size,
                  "duration_s": inventory.metrics.get("duration_s"), "frame_count": measured.get("frames"),
                  "tracks": tracks, "streams": raw, "results": [g.to_dict() for g in gates], "summary": summary,
                  "evidence_paths": evidence, "review_intervals": review, "packet_hashes": hashes,
                  "av_check": {"chunks": chunks,
                               "track": "ld", "applied_correction": m.get("audio.av_correction") or {},
                               "prescreen": av_prescreen(measured.get("av"), chunks),
                               "instructions": "Watch each chunk; mark it in sync, audio too early (sound before "
                                               "the lips) or audio too late (sound after the lips)."},
                  "standing_warnings": STANDING_WARNINGS, "candidate_is_approval": False}
        atomic_write_json(d["json"], result)
        atomic_write_bytes(d["md"], render_markdown(f"{ctx.episode} {m.title or ''}".strip(), str(deliverable),
                                                    gates, summary).encode("utf-8"))
        out = StepOutput(report=Path(d["json"]))
        out.data["summary"] = summary
        if summary["fail"] == 0:
            for key, value in (("qc.video_md5", hashes.get("video")), ("qc.ld_audio_md5", hashes.get("ld")),
                               ("qc.fiveone_md5", hashes.get("fiveone")), ("qc.srt_md5", hashes.get("srt"))):
                if value:
                    out.measure(key, value)
            if "av" in measured:
                out.measure("qc.av_constant_ms", measured["av"]["constant_ms"])
                out.measure("qc.av_slope_ppm", measured["av"]["slope_ppm"])
                out.measure("qc.av_rms_ms", measured["av"]["rms_ms"])
            if "track_offset_ms" in measured:
                out.measure("qc.track_offset_ms", measured["track_offset_ms"])
            out.measure("qc.full_decode_errors", measured["full_decode_errors"])
            if size:
                out.measure("qc.delivered_bytes", size)
        return out

    def _make_proxies(self, ctx, ff, deliverable: Path, chunks: list[dict], run: str) -> None:
        """Best-effort H.264/AAC excerpts of each A/V chunk (HEVC Main 10 does not play everywhere).

        Review-only copies: a failure never fails QC, the chunk just has no ``proxy`` and the player
        falls back to the deliverable itself.
        """
        folder = ctx.root("reports") / f"av-proxy-{run}"
        for i, c in enumerate(chunks):
            try:
                out = ff.av_proxy(deliverable, c["start_s"], c["end_s"] - c["start_s"], folder / f"chunk-{i + 1:02d}.mp4",
                                  cancel=ctx.cancel)
                c["proxy"] = str(out)
            except FstkError as exc:
                c["proxy_error"] = str(exc)[:300]

    def verify(self, ctx, plan, output):
        s = output.data["summary"]
        issues = [f"QC gate failed: {g}" for g in s["failed_gates"]]
        issues += [f"QC gate unverified: {g}" for g in s["unverified_gates"]]
        return issues
