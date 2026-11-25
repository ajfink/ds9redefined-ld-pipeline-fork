"""Stream-copy remux of the verified encode with LD stereo (+ optional retimed DVD 5.1 / SRT)."""
from __future__ import annotations

import os
from pathlib import Path

from fstk.core.errors import ArtifactError, FstkError, GateError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso
from fstk.media.remux import (
    check_layout,
    ld_audio_key,
    remux_tracks_args,
    tracks_from_settings,
    transcode_args,
)

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register


@register
class Remux(Stage):
    """Stream-copy only; LD stereo default; packet-identity proof per copied stream (v38 remux)."""

    id = "remux"
    title = "Remux (stream copy)"
    description = ("Combine the verified encode with LD stereo, the retimed DVD 5.1 and SRT when present, and a "
                   "compatibility stereo track, each in the codec the output settings ask for.")
    writes = ("deliverable.audio_layout",)
    requires = (ArtifactExists("padfixed_video"), ValueSet("deliverable.mux_tracks"),
                ValueSet("deliverable.ld_audio_source"), ValueSet("video.duration_s"), ValueSet("audio.capture"))
    writes_artifacts = ("padfixed",)
    resource = "prep"

    def plan(self, ctx, params):
        m = ctx.manifest
        want_51, want_srt = tracks_from_settings(m.require("deliverable.mux_tracks"))
        ld_key = ld_audio_key(m)
        inputs = {"video": ctx.artifact_path("padfixed_video", m), "ld": ctx.artifact_path(ld_key, m)}
        if want_51:
            inputs["fiveone"] = ctx.artifact_path("retimed_51", m)
        if want_srt:
            inputs["srt"] = ctx.artifact_path("retimed_srt", m)
        missing = [str(p) for p in inputs.values() if not p.is_file()]
        if missing:
            raise GateError(f"remux inputs missing: {missing}")
        from fstk.config.output import audio_tracks, describe_audio, effective

        try:
            settings = effective(ctx.profile, m)
        except FstkError as exc:
            raise GateError(str(exc)) from None
        tracks = audio_tracks(settings, has_51=want_51)
        # A/V-check reruns make new versions; nothing is ever overwritten.
        stem = f"{ctx.episode.code}-23.976-final-output"
        version = 1
        out = ctx.root("out") / f"{stem}.mkv"
        while out.exists() or out.with_name(out.stem + ".tmp.mkv").exists():
            version += 1
            out = ctx.root("out") / f"{stem}-r{version}.mkv"
        report = ctx.root("reports") / ("remux.json" if version == 1 else f"remux-r{version}.json")
        return StepPlan(self.id, params, inputs=list(inputs.values()), outputs=[out, report],
                        summary=describe_audio(settings, has_51=want_51)
                        + (" · SDH subtitles" if want_srt else ""),
                        data={**{k: str(v) for k, v in inputs.items()}, "out": str(out), "report": str(report),
                              "tmp": str(out.with_name(out.stem + ".tmp" + out.suffix)),
                              "duration": m.require("video.duration_s"), "capture": m.require("audio.capture"),
                              "want_51": want_51, "want_srt": want_srt,
                              "layout": [t.to_dict() for t in tracks],
                              "encoded_dir": str(ctx.root("audio") / "delivery" / out.stem)})

    def run(self, ctx, plan):
        from fstk.config.output import AUDIO_CODECS, AudioTrack

        d = plan.data
        ff = ctx.tool("ffmpeg")
        sources = {"ld": Path(d["ld"]), "compat": Path(d["ld"])}
        if d.get("fiveone"):
            sources["51"] = Path(d["fiveone"])
        delivered: list[tuple[Path, AudioTrack]] = []
        encoded_dir = Path(d["encoded_dir"])
        for n, t in enumerate(d["layout"]):
            track = AudioTrack(t["role"], AUDIO_CODECS[t["codec"]], t["channels"], t["bitrate_kbps"], t["title"],
                               t["default"])
            src = sources[t["role"]]
            if track.copy:              # delivered as the pipeline built it (FLAC; never re-encode a lossy source)
                found = ff.probe(src).of_type("audio")
                if found and found[0].codec_name:
                    d["layout"][n] = {**t, "codec_name": found[0].codec_name}
                delivered.append((src, track))
                continue
            ctx.check_cancel()
            ctx.report(n / (len(d["layout"]) + 2), f"encoding {track.title} as {track.codec.label}")
            encoded_dir.mkdir(parents=True, exist_ok=True)
            target = encoded_dir / f"{t['role']}-{track.codec.id}.mka"
            res = ff.run(transcode_args(src, track, target), name=f"audio-{t['role']}", cancel=ctx.cancel)
            if not target.is_file() or target.stat().st_size == 0:
                raise ArtifactError(f"{track.title}: audio encode produced no output ({res.describe()})")
            delivered.append((target, track))
        ctx.check_cancel()
        ctx.report(0.8, "muxing")
        args, pairs = remux_tracks_args(video=Path(d["video"]), audio=delivered,
                                        srt=Path(d["srt"]) if d.get("srt") else None, capture=d["capture"],
                                        duration_s=d["duration"], out=Path(d["tmp"]))
        Path(d["tmp"]).parent.mkdir(parents=True, exist_ok=True)
        res = ff.run(args, name="remux", cancel=ctx.cancel)
        tmp = Path(d["tmp"])
        if not tmp.is_file():
            raise ArtifactError(f"remux produced no output ({res.describe()})")
        info = ff.probe(tmp)
        issues = check_layout([s.raw for s in info.streams], d["layout"], want_srt=d["want_srt"])
        files = [Path(d["video"])] + [p for p, _ in delivered]
        identity = []
        for (label, idx, src_sel, out_sel) in pairs:
            a, b = ff.packet_md5(files[idx], src_sel), ff.packet_md5(tmp, out_sel)
            identity.append({"stream": label, "source_md5": a, "output_md5": b, "match": a == b})
            if a != b:
                issues.append(f"{label} packet hash changed: source={a} output={b}")
        layout = [{**t, "file": str(p)} for t, (p, _) in zip(d["layout"], delivered, strict=True)]
        atomic_write_json(d["report"], {"schema": "fstk-remux-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "issues": issues, "identity": identity,
                                        "audio_layout": layout,
                                        "status": "PASS" if not issues else "FAIL"})
        if not issues:
            os.replace(tmp, d["out"])
        out = StepOutput(report=Path(d["report"]))
        out.data["issues"] = issues
        out.artifacts["padfixed"] = {"root": "out", "name": Path(d["out"]).name}
        if not issues:
            out.measure("deliverable.audio_layout", layout, note=" · ".join(t["title"] for t in layout))
        return out

    def verify(self, ctx, plan, output):
        return list(output.data["issues"])
