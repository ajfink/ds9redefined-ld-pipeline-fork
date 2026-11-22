"""``dvd-retime``: DVD 5.1 and English subtitles placed on the LD output timeline.

Replaces v38's ``prepare_retime_breaks`` → break review → ``measure_break_edges`` → edge review
→ ``retime_dvd_assets`` chain (two human reviews) with one automatic stage. Owner decisions
2026-11-21: "done" includes time-aligned DVD 5.1 and subtitles; the only human review is the
A/V check.

1. **Map** (``fstk.media.retime_map``). The windows ``dvd-ld-audio`` already matched (LD WAV ↔ DVD
   stereo) are split into constant-relation segments, each edit is located by a fine scan, and
   everything is placed on the output via the LD clock and ``video.delta_s``. Audio-precise:
   S01E20 verifies to ≤ 6.3 ms over 368 chunks.
2. **Render** (``fstk.media.retime_render``). The DVD 5.1 is resampled per piece at the exact
   ratio into 24-bit FLAC. LD-only spans are filled from the LD track; the length is exact.
3. **Subtitles.** The English text track (``srt_source`` artifact, else the DVD's subrip stream)
   is retimed through the same map. Cues on DVD-only material are reported, never silently lost.
4. **Verify.** The retimed 5.1 (downmixed) is matched against the delivered LD audio across the
   episode and on both sides of every edit. Results go to
   ``reports/retime-audio-verification.json``, which QC's "retime output gaps" gate reads. The
   stage fails if the median offset exceeds 10 ms or fewer than 90% of confident chunks are within
   20 ms.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.hashing import sha256_file
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import dvd_audio as da
from fstk.media.retime_map import build_map, retime_srt
from fstk.media.retime_render import RATE, render_51

from .base import ArtifactExists, Custom, Stage, StepOutput, StepPlan, ValueIs, ValueSet, register

HOP = 0.005
VERIFY = {"chunk_s": 10.0, "every_s": 20.0, "min_corr": 0.6, "max_median_ms": 10.0, "within_ms": 20.0,
          "min_within_fraction": 0.9}


def _clock_report(ctx, manifest):
    ok = manifest.get("audio.rate_method") == "dvd-referenced-clock" and bool(manifest.artifact("audio_timing_report"))
    return ok, "LD audio built by dvd-ld-audio" if ok else "run dvd-ld-audio first (DVD-referenced LD clock)"


def chunk_offsets(ref: np.ndarray, other: np.ndarray, times: list[float], *, chunk_s: float, sr: int = da.SR,
                  search_s: float = 0.3) -> list[dict]:
    """Offset (ms) of ``other`` vs ``ref`` for chunks starting at ``times`` (both on the output timeline)."""
    re_, oe = da.envelope(ref, HOP, sr), da.envelope(other, HOP, sr)
    n = round(chunk_s / HOP)
    out = []
    for t in times:
        a = round(t / HOP)
        x = re_[a:a + n]
        lo = max(0, round((t - search_s) / HOP))
        y = oe[lo:lo + n + round(2 * search_s / HOP)]
        if len(x) < n or len(y) < n or np.std(x) < 1e-3:
            continue
        s = da.slide_corr(x, y)
        k = int(np.argmax(s))
        off = ((lo + k + da._parabolic(s, k)) * HOP - t) * 1000
        out.append({"t_s": round(float(t), 3), "offset_ms": round(float(off), 2), "corr": round(float(s[k]), 3)})
    return out


@register
class DvdRetime(Stage):
    id = "dvd-retime"
    title = "Retime DVD 5.1 + subtitles onto the LD timeline"
    description = ("Audio-precise DVD→LD map (edits located automatically), exact-ratio 5.1 render with LD "
                   "fill, retimed English subtitles, and a self-check against the LD audio.")
    requires = (ValueIs("mode.dvd_reference_available", True), Custom("dvd-ld-audio", _clock_report),
                ValueSet("video.delta_s"), ValueSet("video.frames"), ArtifactExists("dvd"),
                ArtifactExists("ld_native"), ArtifactExists("ld_synced"))
    writes = ("retime.srt_cues", "deliverable.mux_tracks", "deliverable.tracks")
    writes_artifacts = ("retimed_51", "retimed_srt", "retime_map")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        run = utc_stamp()[:15].lower()
        code = ctx.episode.code
        names = {"fiveone": ctx.root("audio") / f"{code}-dvd51-retimed-{run}.flac",
                 "srt": ctx.root("subs") / f"{code}-dvd-eng-retimed-{run}.srt",
                 "map": ctx.root("reports") / f"retime-map-{run}.json",
                 "verify": ctx.root("reports") / "retime-audio-verification.json"}
        if names["verify"].exists():
            names["verify"] = ctx.root("reports") / f"retime-audio-verification-{run}.json"
        srt_src = ctx.artifact_path("srt_source", m) if m.artifact("srt_source") else None
        inputs = [ctx.artifact_path(k, m) for k in ("dvd", "ld_native", "ld_synced", "audio_timing_report")]
        return StepPlan(self.id, params, inputs=inputs + ([srt_src] if srt_src else []),
                        outputs=[names["fiveone"], names["map"], names["verify"]],
                        summary="map DVD→LD by audio, render 5.1, retime subtitles, self-check",
                        data={**{k: str(v) for k, v in names.items()}, "run": run,
                              "srt_source": str(srt_src) if srt_src else None,
                              "frames": int(m.require("video.frames")), "delta": float(m.require("video.delta_s"))})

    def _streams(self, ff, dvd: Path) -> tuple[str, str, str, int | None]:
        info = ff.probe(dvd)
        audio = info.of_type("audio")
        six = [i for i, s in enumerate(audio) if int(s.raw.get("channels") or 0) == 6]
        two = [i for i, s in enumerate(audio) if int(s.raw.get("channels") or 0) == 2]
        if len(six) != 1 or len(two) != 1:
            raise GateError(f"expected one 5.1 and one stereo DVD audio stream; channels "
                            f"{[s.raw.get('channels') for s in audio]}")
        layout = audio[six[0]].raw.get("channel_layout") or "5.1(side)"
        subs = info.of_type("subtitle")
        text = [i for i, s in enumerate(subs) if s.codec_name in ("subrip", "srt", "ass", "mov_text")
                and ((s.raw.get("tags") or {}).get("language") in (None, "eng", "en"))]
        return f"0:a:{six[0]}", f"0:a:{two[0]}", layout, (text[0] if text else None)

    def run(self, ctx, plan):
        d, m = plan.data, ctx.manifest
        ff = ctx.tool("ffmpeg")
        dvd, native, synced = (ctx.artifact_path(k, m) for k in ("dvd", "ld_native", "ld_synced"))
        timing = read_json(ctx.artifact_path("audio_timing_report", m))
        clock = timing["clock"]
        six, two, layout, text_sub = self._streams(ff, dvd)
        ctx.report(0.02, "decoding analysis copies (LD, DVD stereo)")
        ld_pcm = ff.pcm(native, "0:a:0", da.SR, cancel=ctx.cancel)
        dvd_pcm = ff.pcm(dvd, two, da.SR, cancel=ctx.cancel)
        total = d["frames"] * 1001 / 24000          # exact: 2002 samples per frame at 48 kHz
        try:
            rmap = build_map(clock["windows"], intercept=clock["intercept_s"], slope=clock["slope"],
                             delta_s=d["delta"], total_s=total, ld_env=da.envelope(ld_pcm, HOP),
                             dvd_env=da.envelope(dvd_pcm, HOP), env_hop=HOP, dvd_duration_s=len(dvd_pcm) / da.SR)
        except ValueError as exc:
            raise GateError(f"retime map: {exc}") from None
        atomic_write_json(d["map"], {"schema": "fstk-retime-map-v1", "episode": str(ctx.episode),
                                     "created_utc": utc_now_iso(), **rmap.summary()})
        ctx.report(0.2, "rendering the retimed 5.1")
        try:
            render = render_51(rmap.pieces, total_s=total, dvd=dvd, dvd_selector=six, layout=layout,
                               ld_synced=synced, out=Path(d["fiveone"]), ffmpeg=str(ff.ffmpeg), cancel=ctx.cancel,
                               progress=lambda f: ctx.report(0.2 + 0.6 * f, "rendering the retimed 5.1"))
        except (OSError, RuntimeError) as exc:
            raise ArtifactError(f"5.1 render failed: {exc}") from exc
        # subtitles
        srt_text, srt_origin = None, None
        if d["srt_source"]:
            srt_text, srt_origin = Path(d["srt_source"]).read_text(encoding="utf-8-sig"), "srt_source artifact"
        elif text_sub is not None:
            res = subprocess.run([ff.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(dvd), "-map",
                                  f"0:s:{text_sub}", "-f", "srt", "-"], capture_output=True, timeout=600)
            if res.returncode == 0 and res.stdout.strip():
                srt_text, srt_origin = res.stdout.decode("utf-8", "replace"), f"DVD subtitle stream 0:s:{text_sub}"
        cues, dropped = 0, []
        if srt_text:
            out_text, dropped = retime_srt(srt_text, rmap)
            atomic_write_bytes(d["srt"], out_text.encode("utf-8"))
            cues = out_text.count(" --> ")
        # verification against the delivered LD audio
        ctx.report(0.85, "checking the 5.1 against the LD audio")
        ld_out = ff.pcm(synced, "0:a:0", da.SR, cancel=ctx.cancel)
        five = ff.pcm(Path(d["fiveone"]), "0:a:0", da.SR, cancel=ctx.cancel)
        times = list(np.arange(5.0, total - VERIFY["chunk_s"] - 5.0, VERIFY["every_s"]))
        edge_rows = []
        for sw in rmap.switches:
            before = chunk_offsets(ld_out, five, [sw["out_s"] - VERIFY["chunk_s"] - 1.0], chunk_s=VERIFY["chunk_s"])
            after = chunk_offsets(ld_out, five, [sw["out_s"] + 1.0], chunk_s=VERIFY["chunk_s"])
            ok = all(abs(r["offset_ms"]) <= VERIFY["within_ms"] for r in before + after if r["corr"] >= VERIFY["min_corr"])
            edge_rows.append({"label": f"edit@{sw['out_s']:.1f}s", "expected_start_s": sw["out_s"],
                              "expected_end_s": sw["out_s"] + sw["ld_only_s"], "status": "PASS" if ok else "FAIL",
                              "before": before, "after": after, "switch": sw})
        rows = [r for r in chunk_offsets(ld_out, five, times, chunk_s=VERIFY["chunk_s"]) if r["corr"] >= VERIFY["min_corr"]]
        offs = np.array([r["offset_ms"] for r in rows]) if rows else np.array([np.inf])
        median = float(np.median(offs))
        within = float(np.mean(np.abs(offs) <= VERIFY["within_ms"]))
        ok = abs(median) <= VERIFY["max_median_ms"] and within >= VERIFY["min_within_fraction"] and \
            all(r["status"] == "PASS" for r in edge_rows)
        verify = {"schema": "fstk-retime-verification-v1", "episode": str(ctx.episode), "status": "PASS" if ok else "FAIL",
                  "method": "retimed 5.1 downmix vs delivered LD audio, 10 s envelope correlation",
                  "thresholds": VERIFY, "chunks": rows, "median_offset_ms": median, "within_fraction": within,
                  "gaps": edge_rows, "created_utc": utc_now_iso()}
        atomic_write_json(d["verify"], verify)
        if not ok:
            raise ArtifactError(f"retimed 5.1 does not line up with the LD audio (median {median:+.1f} ms, "
                                f"{within:.0%} within {VERIFY['within_ms']:.0f} ms, "
                                f"{sum(r['status'] != 'PASS' for r in edge_rows)} edit(s) failing); see {d['verify']}")
        out = StepOutput(report=Path(d["verify"]))
        out.artifacts["retimed_51"] = {"root": "audio", "name": Path(d["fiveone"]).name}
        out.artifacts["retime_map"] = {"root": "reports", "name": Path(d["map"]).name}
        tracks = ["ld", "51"]
        if srt_text and cues:
            out.artifacts["retimed_srt"] = {"root": "subs", "name": Path(d["srt"]).name}
            out.measure("retime.srt_cues", cues, note=f"from {srt_origin}; {len(dropped)} cue(s) on DVD-only material")
            tracks.append("srt")
        out.measure("deliverable.mux_tracks", tracks, note="owner: LD stereo default + DVD 5.1 + DVD subtitles")
        out.measure("deliverable.tracks", tracks, note="owner: LD stereo default + DVD 5.1 + DVD subtitles")
        out.data.update({"render": render, "dropped_cues": dropped, "fiveone_sha256": sha256_file(Path(d["fiveone"]))})
        return out

    def verify(self, ctx, plan, output):
        rep = read_json(plan.data["verify"])
        r = output.data["render"]
        issues = [] if rep.get("status") == "PASS" else ["verification report is not PASS"]
        if r["samples"] != r["target_samples"]:
            issues.append(f"5.1 has {r['samples']} samples; the output needs {r['target_samples']}")
        if r["samples"] != plan.data["frames"] * 1001 * RATE // 24000:
            issues.append("5.1 length does not match the video")
        return issues
