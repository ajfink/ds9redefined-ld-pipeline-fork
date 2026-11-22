"""Stream-copy remux of the verified encode with LD stereo (+ optional retimed DVD 5.1 / SRT)."""
from __future__ import annotations

import os
from pathlib import Path

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso
from fstk.media.remux import check_inventory, remux_args, tracks_from_settings

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register


@register
class Remux(Stage):
    """Stream-copy only; LD stereo default; packet-identity proof per copied stream (v38 remux)."""

    id = "remux"
    title = "Remux (stream copy)"
    description = "Combine the verified encode with LD stereo and optional retimed DVD 5.1/SRT."
    requires = (ArtifactExists("padfixed_video"), ValueSet("deliverable.mux_tracks"),
                ValueSet("deliverable.ld_audio_source"), ValueSet("video.duration_s"), ValueSet("audio.capture"))
    writes_artifacts = ("padfixed",)
    resource = "prep"
    review = "remux"

    def plan(self, ctx, params):
        m = ctx.manifest
        want_51, want_srt = tracks_from_settings(m.require("deliverable.mux_tracks"))
        ld_key = m.require("deliverable.ld_audio_source")
        inputs = {"video": ctx.artifact_path("padfixed_video", m), "ld": ctx.artifact_path(ld_key, m)}
        if want_51:
            inputs["fiveone"] = ctx.artifact_path("retimed_51", m)
        if want_srt:
            inputs["srt"] = ctx.artifact_path("retimed_srt", m)
        missing = [str(p) for p in inputs.values() if not p.is_file()]
        if missing:
            raise GateError(f"remux inputs missing: {missing}")
        out = ctx.root("out") / f"{ctx.episode.code}-23.976-padfixed.mkv"
        if out.exists():
            raise GateError(f"refusing to overwrite {out}")
        report = ctx.root("reports") / "remux.json"
        return StepPlan(self.id, params, inputs=list(inputs.values()), outputs=[out, report],
                        summary=f"remux tracks {m.get('deliverable.mux_tracks')}",
                        data={**{k: str(v) for k, v in inputs.items()}, "out": str(out), "report": str(report),
                              "tmp": str(out.with_name(out.stem + ".tmp" + out.suffix)),
                              "duration": m.require("video.duration_s"), "capture": m.require("audio.capture"),
                              "want_51": want_51, "want_srt": want_srt})

    def run(self, ctx, plan):
        d = plan.data
        ff = ctx.tool("ffmpeg")
        args, pairs = remux_args(video=Path(d["video"]), ld_audio=Path(d["ld"]),
                                 fiveone=Path(d["fiveone"]) if d.get("fiveone") else None,
                                 srt=Path(d["srt"]) if d.get("srt") else None, capture=d["capture"],
                                 duration_s=d["duration"], out=Path(d["tmp"]))
        Path(d["tmp"]).parent.mkdir(parents=True, exist_ok=True)
        res = ff.run(args, name="remux", cancel=ctx.cancel)
        tmp = Path(d["tmp"])
        if not tmp.is_file():
            raise ArtifactError(f"remux produced no output ({res.describe()})")
        info = ff.probe(tmp)
        issues = check_inventory([s.raw for s in info.streams], want_51=d["want_51"], want_srt=d["want_srt"])
        sources = [Path(d["video"]), Path(d["ld"])] + ([Path(d["fiveone"])] if d.get("fiveone") else [])
        identity = []
        for (label, idx, src_sel, out_sel) in pairs:
            a, b = ff.packet_md5(sources[idx], src_sel), ff.packet_md5(tmp, out_sel)
            identity.append({"stream": label, "source_md5": a, "output_md5": b, "match": a == b})
            if a != b:
                issues.append(f"{label} packet hash changed: source={a} output={b}")
        atomic_write_json(d["report"], {"schema": "fstk-remux-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "issues": issues, "identity": identity,
                                        "status": "PASS" if not issues else "FAIL"})
        if not issues:
            os.replace(tmp, d["out"])
        out = StepOutput(report=Path(d["report"]))
        out.data["issues"] = issues
        out.artifacts["padfixed"] = {"root": "out", "name": Path(d["out"]).name}
        return out

    def verify(self, ctx, plan, output):
        return list(output.data["issues"])
