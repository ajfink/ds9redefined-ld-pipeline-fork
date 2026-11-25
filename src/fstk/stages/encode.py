"""Final encode stage (video only; encoder and quality from the output settings).

``encode-final`` (v38 ``encode_episode_mac.py --pass final`` + ``validate_encoded_video.py``):
* the chain encoded is ``encode_script`` plus any operator frame fixes and the measured finishing, each verified
  against the script it was built on (there is no PAD splice any more: ADR 0004);
* the chain must report exactly the measured ``video.frames``;
* the encode goes to ``<name>.tmp.mkv`` and is moved into place only after the encoded stream
  passes every check (profile, pixel format, rate, raster, decode-counted frames, a clean full
  decode, duration within 2 ms); an existing deliverable is never overwritten;
* audio is handled separately (remux), so audio fixes never re-run x265;
* encoder, quality, preset and tune come from the output settings (machine defaults or this episode's
  own, ``fstk.config.output``); x265 always uses tune grain; average-bitrate mode runs two passes;
  the settings used and the stream properties expected are recorded as ``deliverable.video_encoding``;
* no human review (owner decision 2026-11-21): the settings are fixed and every check above is
  automatic, so a verified encode goes straight on to remux.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fstk.core.errors import ArtifactError, FstkError, GateError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso
from fstk.media.encode import (
    check_encoded_stream,
    display_aspect,
    final_encode_args,
)
from fstk.media.finish import finish_matches_base
from fstk.tools import process

from .base import ArtifactExists, SpaceNeed, Stage, StepOutput, StepPlan, ValueSet, register


def finished_anyway(res, served: dict, frames: int) -> bool:
    """vspipe ended with STATUS_CONTROL_C_EXIT that fstk did not send, after reporting it served every frame,
    and FFmpeg exited 0 (the encoded stream is still fully verified afterwards)."""
    stages = res.stages or [res]
    return (len(stages) == 2 and process.interrupted_unasked(stages[0]) and stages[1].returncode == 0
            and served.get("frames") == frames)

@register
class EncodeFinal(Stage):
    id = "encode-final"
    title = "Final encode (video only)"
    description = ("One fresh encode from the measured IVTC script with the episode's output settings "
                   "(default x265 CRF 14 / slow / grain, 10-bit 4:2:2); verified before use.")
    requires = (ValueSet("video.frames"), ValueSet("video.duration_s"), ValueSet("audio.capture"),
                ArtifactExists("encode_script"), ArtifactExists("stack_master"))
    writes_artifacts = ("padfixed_video",)
    writes = ("deliverable.video_encoding",)
    resource = "compute"

    def plan(self, ctx, params):
        try:
            planned, basis = float(params["planned_gib"]), str(params["estimate_basis"])
        except (KeyError, TypeError, ValueError):
            raise GateError("encode-final needs planned_gib and estimate_basis (a defensible estimate)") from None
        m = ctx.manifest
        out = ctx.root("out") / f"{ctx.episode.code}-23.976-padfixed-video.mkv"
        rev = 2
        while out.exists():          # a fresh encode after review choices: never overwrite, number the revision
            out = ctx.root("out") / f"{ctx.episode.code}-23.976-padfixed-video-r{rev}.mkv"
            rev += 1
        tmp = out.with_name(out.stem + ".tmp" + out.suffix)
        set_aside = None
        if tmp.exists():
            # a failed or cut-off attempt left its file: kept for inspection beside the new one, not in its way
            # (S01E20 2026-11-25: the leftover of a failed encode blocked the next encode for the whole night)
            set_aside = out.with_name(out.stem + ".previous-attempt" + out.suffix)
            os.replace(tmp, set_aside)
        from .framefix import resolve_chain

        script = resolve_chain(ctx, m)   # encode script → operator fixes, verified
        if m.artifact("finish_script"):   # measured finishing (crop, concealment, optional chroma denoise)
            fin = ctx.artifact_path("finish_script", m)
            if not finish_matches_base(fin.read_text(encoding="utf-8"), script.read_text(encoding="utf-8")):
                raise GateError("finish_script was built from an older chain; re-run video-quality")
            script = fin
        master = ctx.artifact_path("stack_master", m)
        report = ctx.root("reports") / "encode-final.json"
        from fstk.config.output import OutputSettings, describe_video, effective, validate_group

        try:
            settings = effective(ctx.profile, m)
        except FstkError as exc:
            raise GateError(str(exc)) from None
        video = dict(settings.video)
        chosen_chroma = m.get("mode.chroma_output")
        if chosen_chroma and "video" not in settings.overridden:
            video["chroma"] = chosen_chroma        # the episode's earlier chroma decision wins over a default
        if "preset" in params:
            video["preset"] = str(params["preset"])   # smoke runs only; recorded as a recipe override
        problems = validate_group("video", video)
        if problems:
            raise GateError("; ".join(problems))
        settings = OutputSettings(video, settings.audio, settings.overridden)
        frames = int(m.require("video.frames"))
        estimate = frames * settings.encoder.bytes_per_frame / 2**30
        if estimate > planned:
            planned = round(estimate, 2)
            basis = f"{frames} frames × {settings.encoder.bytes_per_frame} B ({settings.encoder.label})"
        return StepPlan(self.id, params, inputs=[script, master], outputs=[out, report],
                        space=SpaceNeed("out", planned, basis),
                        summary=f"{describe_video(settings)} → {out.name}"
                                + (f" (the previous attempt's file was kept as {set_aside.name})" if set_aside else ""),
                        data={"script": str(script), "master": str(master), "out": str(out), "tmp": str(tmp),
                              "report": str(report), "frames": m.require("video.frames"),
                              "duration": m.require("video.duration_s"), "capture": m.require("audio.capture"),
                              "locked_dar": m.get("quality.dar"),
                              "chroma": video["chroma"], "settings": settings.to_dict(),
                              "description": describe_video(settings),
                              # anything but the settings' preset is a recorded override (smoke runs only)
                              "preset": str(video["preset"]),
                              "recipe_override": "preset" in params})

    def run(self, ctx, plan):
        d = plan.data
        vs, ff = ctx.tool("vapoursynth"), ctx.tool("ffmpeg")
        info = vs.info(Path(d["script"]))
        if info["frames"] != d["frames"]:
            raise ArtifactError(f"script reports {info['frames']} frames but video.frames={d['frames']}")
        src = ff.probe(Path(d["master"])).only("video")
        aspect = d.get("locked_dar") or display_aspect(src.raw.get("sample_aspect_ratio", ""), int(src.raw["width"]),
                                                       int(src.raw["height"]))
        Path(d["tmp"]).parent.mkdir(parents=True, exist_ok=True)
        ctx.report(0.0, f"encoding {d['frames']} frames")
        from fstk.config.output import OutputSettings, expected_video, two_pass
        from fstk.tools.progress_lines import LineProgress

        sd = d["settings"]
        settings = OutputSettings(sd["video"], sd["audio"], tuple(sd.get("overridden") or ()))
        common = dict(aspect=aspect, frames=d["frames"], duration_s=d["duration"], capture=d["capture"],
                      out=d["tmp"], settings=settings)
        served: dict[str, int] = {}

        def watched(progress):
            def on_line(name: str, line: str) -> None:
                hit = re.match(r"Output (\d+) frames", line) if name.startswith("vspipe") else None
                if hit:
                    served["frames"] = int(hit.group(1))
                progress(name, line)
            return on_line

        if two_pass(settings):
            stats = str(Path(d["tmp"]).with_suffix(".passlog"))
            first = LineProgress(ctx.report, total_frames=d["frames"], span=0.44, label="pass 1 of 2:")
            res = vs.encode(Path(d["script"]), final_encode_args(**common, pass_no=1, stats=stats), name="encode-pass1",
                            cancel=ctx.cancel, on_line=watched(first))
            ctx.check_cancel()
            if res.returncode not in (0,) and not finished_anyway(res, served, d["frames"]):
                raise ArtifactError(f"first pass failed ({res.describe()})")
            second = LineProgress(ctx.report, total_frames=d["frames"], base=0.44, span=0.44, label="pass 2 of 2:")
            served.clear()
            res = vs.encode(Path(d["script"]), final_encode_args(**common, pass_no=2, stats=stats),
                            cancel=ctx.cancel, on_line=watched(second))
        else:
            encode_progress = LineProgress(ctx.report, total_frames=d["frames"], span=0.88, label="encoding")
            res = vs.encode(Path(d["script"]), final_encode_args(**common), cancel=ctx.cancel,
                            on_line=watched(encode_progress))
        expected = expected_video(settings)
        ctx.check_cancel()
        tmp = Path(d["tmp"])
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise ArtifactError(f"encoder produced no output ({res.describe()})")
        ctx.report(0.9, "verifying encoded stream (full decode)")
        info_out = ff.probe(tmp)
        stream = info_out.only("video")
        decode = ff.null_decode(tmp, progress=LineProgress(ctx.report, total_frames=d["frames"], base=0.9, span=0.09,
                                                           label="verifying"))
        issues = check_encoded_stream(stream.raw, probe_frames=ff.count_frames(tmp), decoded_frames=decode.frames,
                                      decode_errors=decode.errors, duration_s=info_out.duration_s,
                                      expected_frames=d["frames"], expected_duration_s=d["duration"],
                                      expected_raster=(int(info.get("width", src.raw["width"])), int(info.get("height", src.raw["height"]))),
                                      chroma=d["chroma"], expected=expected)
        notes: list[str] = []
        if res.returncode not in (0,):
            if finished_anyway(res, served, d["frames"]):
                notes.append(f"vspipe ended with 0xC000013A (a console control event fstk did not send) after it had "
                             f"served all {d['frames']} frames; FFmpeg finished cleanly and the stream verified above")
            else:
                issues.append(f"encode pipeline exit codes {[s.returncode for s in res.stages]}")
        atomic_write_json(d["report"], {"schema": "fstk-encode-final-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "aspect": aspect, "chroma": d["chroma"], "preset": d["preset"],
                                        "settings": d["settings"], "description": d["description"],
                                        "expected_stream": expected,
                                        "recipe_override": d["recipe_override"], "issues": issues, "notes": notes,
                                        "status": "PASS" if not issues else "FAIL",
                                        "encoded": str(d["out"]) if not issues else str(tmp)})
        if not issues:
            os.replace(tmp, d["out"])
        out = StepOutput(report=Path(d["report"]))
        out.data["issues"] = issues
        out.artifacts["padfixed_video"] = {"root": "out", "name": Path(d["out"]).name}
        if not issues:
            out.measure("deliverable.video_encoding", {**expected, "settings": d["settings"]["video"],
                                                       "description": d["description"],
                                                       "recipe_override": d["recipe_override"]},
                        note=d["description"])
        return out

    def verify(self, ctx, plan, output):
        return list(output.data["issues"])
