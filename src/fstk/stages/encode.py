"""Final encode stage (single x265 pass; video only).

``encode-final`` (v38 ``encode_episode_mac.py --pass final`` + ``validate_encoded_video.py``):
* when ``pad-repair`` has run, its ``padfix_script`` (the base chain plus the PAD splice) is encoded instead of
  ``encode_script``, provided it was built from the current base script;
* the IVTC/PAD-repair script must report exactly the measured ``video.frames``;
* the encode goes to ``<name>.tmp.mkv`` and is moved into place only after the encoded stream
  passes every check (profile, pixel format, rate, raster, decode-counted frames, a clean full
  decode, duration within 2 ms); an existing deliverable is never overwritten;
* audio is handled separately (remux), so audio fixes never re-run x265;
* no human review (owner decision 2026-11-21): the settings are fixed and every check above is
  automatic, so a verified encode goes straight on to remux.
"""
from __future__ import annotations

import os
from pathlib import Path

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso
from fstk.media import pad_repair
from fstk.media.encode import (
    DEFAULT_CHROMA,
    DEFAULT_PRESET,
    check_encoded_stream,
    display_aspect,
    final_encode_args,
)

from .base import ArtifactExists, SpaceNeed, Stage, StepOutput, StepPlan, ValueSet, register


@register
class EncodeFinal(Stage):
    id = "encode-final"
    title = "Final x265 encode (video only)"
    description = ("CRF 14 / slow / grain 10-bit single pass from the measured IVTC script (4:2:2 by default, "
                   "keeping the master's chroma); verified before use.")
    requires = (ValueSet("video.frames"), ValueSet("video.duration_s"), ValueSet("audio.capture"),
                ArtifactExists("encode_script"), ArtifactExists("stack_master"))
    writes_artifacts = ("padfixed_video",)
    resource = "compute"

    def plan(self, ctx, params):
        try:
            planned, basis = float(params["planned_gib"]), str(params["estimate_basis"])
        except (KeyError, TypeError, ValueError):
            raise GateError("encode-final needs planned_gib and estimate_basis (a defensible estimate)") from None
        m = ctx.manifest
        out = ctx.root("out") / f"{ctx.episode.code}-23.976-padfixed-video.mkv"
        if out.exists():
            raise GateError(f"refusing to overwrite existing encode {out}")
        tmp = out.with_name(out.stem + ".tmp" + out.suffix)
        if tmp.exists():
            raise GateError(f"temporary output exists; inspect it before retrying: {tmp}")
        script = ctx.artifact_path("encode_script", m)
        if m.artifact("padfix_script"):   # PAD-repaired chain; must have been built from this very base script
            fixed = ctx.artifact_path("padfix_script", m)
            if not pad_repair.padfix_matches_base(fixed.read_text(encoding="utf-8"),
                                                  script.read_text(encoding="utf-8")):
                raise GateError("padfix_script was built from an older encode script; re-run pad-audit and pad-repair")
            script = fixed
        master = ctx.artifact_path("stack_master", m)
        report = ctx.root("reports") / "encode-final.json"
        return StepPlan(self.id, params, inputs=[script, master], outputs=[out, report],
                        space=SpaceNeed("out", planned, basis), summary=f"x265 → {out.name}",
                        data={"script": str(script), "master": str(master), "out": str(out), "tmp": str(tmp),
                              "report": str(report), "frames": m.require("video.frames"),
                              "duration": m.require("video.duration_s"), "capture": m.require("audio.capture"),
                              "chroma": m.get("mode.chroma_output") or DEFAULT_CHROMA,
                              # anything but the recipe's preset is a recorded override (smoke runs only)
                              "preset": str(params.get("preset", DEFAULT_PRESET))})

    def run(self, ctx, plan):
        d = plan.data
        vs, ff = ctx.tool("vapoursynth"), ctx.tool("ffmpeg")
        info = vs.info(Path(d["script"]))
        if info["frames"] != d["frames"]:
            raise ArtifactError(f"script reports {info['frames']} frames but video.frames={d['frames']}")
        src = ff.probe(Path(d["master"])).only("video")
        aspect = display_aspect(src.raw.get("sample_aspect_ratio", ""), int(src.raw["width"]),
                                int(src.raw["height"]))
        Path(d["tmp"]).parent.mkdir(parents=True, exist_ok=True)
        ctx.report(0.0, f"encoding {d['frames']} frames")
        res = vs.encode(Path(d["script"]), final_encode_args(aspect=aspect, frames=d["frames"],
                                                             duration_s=d["duration"], capture=d["capture"],
                                                             out=d["tmp"], chroma=d["chroma"], preset=d["preset"]),
                         cancel=ctx.cancel)
        ctx.check_cancel()
        tmp = Path(d["tmp"])
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise ArtifactError(f"encoder produced no output ({res.describe()})")
        ctx.report(0.9, "verifying encoded stream (full decode)")
        info_out = ff.probe(tmp)
        stream = info_out.only("video")
        decode = ff.null_decode(tmp)
        issues = check_encoded_stream(stream.raw, probe_frames=ff.count_frames(tmp), decoded_frames=decode.frames,
                                      decode_errors=decode.errors, duration_s=info_out.duration_s,
                                      expected_frames=d["frames"], expected_duration_s=d["duration"],
                                      expected_raster=(int(src.raw["width"]), int(src.raw["height"])),
                                      chroma=d["chroma"])
        if res.returncode not in (0,):
            issues.append(f"encode pipeline exit codes {[s.returncode for s in res.stages]}")
        atomic_write_json(d["report"], {"schema": "fstk-encode-final-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "aspect": aspect, "chroma": d["chroma"], "preset": d["preset"],
                                        "recipe_override": d["preset"] != DEFAULT_PRESET, "issues": issues,
                                        "status": "PASS" if not issues else "FAIL",
                                        "encoded": str(d["out"]) if not issues else str(tmp)})
        if not issues:
            os.replace(tmp, d["out"])
        out = StepOutput(report=Path(d["report"]))
        out.data["issues"] = issues
        out.artifacts["padfixed_video"] = {"root": "out", "name": Path(d["out"]).name}
        return out

    def verify(self, ctx, plan, output):
        return list(output.data["issues"])
