"""Cadence measurement stage (VapourSynth TFM on every platform).

``cadence-evidence`` replaces v38's Windows chain (AviSynth TFM log + ``build_map.ps1``) and the
macOS chain (``generate_cadence_evidence`` + ``build_map_vpy``) with one path:

1. write a TFM-log script over the stack master trimmed by ``audio.pad_frames``;
2. evaluate every frame (vspipe → FFmpeg null); the decoded count must equal
   ``stack.frames − audio.pad_frames`` and both processes must exit cleanly;
3. parse the TFM log, requiring exactly that many contiguous rows (fail closed);
4. analyse cadence (``fstk.media.cadence``) and build the repair map
   (``fstk.media.cadence_map``, v38 build_map.ps1 rules);
5. write the report and di-map as evidence. Metrics are proposed but committed only after the
   ``cadence-review`` approval (v38 recorded cadence metrics only after visual approval).

Re-validating this chain on Windows VapourSynth against v38's AviSynth output is still owed
(REFACTOR-PLAN Phase 4 exit criterion).
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import ArtifactError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, decode_text, read_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media.cadence import analyze_tfm_rows, load_motion_trace, parse_tfm_log
from fstk.media.cadence_map import di_map_text, repair_mask, repair_ranges
from fstk.tools.vapoursynth import tfm_log_script

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register


@register
class CadenceEvidence(Stage):
    id = "cadence-evidence"
    title = "Measure cadence (TFM) and build the repair map"
    description = "Full-side VapourSynth TFM log, cadence analysis, and repair ranges; human review required."
    requires = (ValueSet("stack.frames"), ValueSet("audio.pad_frames"), ArtifactExists("stack_master"))
    writes = ("video.tfm_period5_pct", "video.tfm_unmatched", "video.tfm_combed_count",
              "video.tfm_combed_spans", "video.inserts", "video.repair_frames", "video.repair_pct")
    writes_artifacts = ("tfm_log", "di_map", "cadence_summary")
    resource = "compute"
    review = "cadence-review"

    def plan(self, ctx, params):
        m = ctx.manifest
        master = ctx.artifact_path("stack_master", m)
        pad = m.require("audio.pad_frames")
        stack_frames = m.require("stack.frames")
        expected = stack_frames - pad
        if expected <= 0:
            raise ArtifactError(f"audio.pad_frames={pad} leaves no frames of stack.frames={stack_frames}")
        probe = ctx.root("probe")
        slug = ctx.episode.slug
        files = {"script": ctx.root("scripts") / f"cadence-tfm-{slug}.vpy",
                 "log": probe / f"full-tfm-log-{slug}.txt", "di_map": probe / f"di-map-{slug}.txt",
                 "report": ctx.root("reports") / "cadence.json"}
        motion = params.get("motion_trace")
        inputs = [master] + ([Path(motion)] if motion else [])
        return StepPlan(self.id, params, inputs=inputs,
                        outputs=[files["log"], files["di_map"], files["report"]],
                        summary=f"TFM over {expected} frames (pad {pad})",
                        data={k: str(v) for k, v in files.items()} | {
                            "master": str(master), "pad": pad, "expected": expected,
                            "motion": motion})

    def run(self, ctx, plan):
        vs = ctx.tool("vapoursynth")
        d = plan.data
        log = Path(d["log"])
        log.parent.mkdir(parents=True, exist_ok=True)  # TFM does not create its log folder
        if log.exists():
            # TFM appends to its log: a previous run's rows must never be parsed as this run's
            # evidence. Keep the old log (never delete evidence); move it aside.
            log.rename(log.with_name(f"{log.stem}.superseded-{utc_stamp()}{log.suffix}"))
        atomic_write_bytes(d["script"], tfm_log_script(Path(d["master"]), d["pad"], log).encode())
        ctx.report(0.02, "evaluating every frame through TFM")
        decode = vs.full_decode(Path(d["script"]), name="cadence-tfm", cancel=ctx.cancel)
        ctx.check_cancel()
        if not decode.clean or decode.frames != d["expected"]:
            raise ArtifactError(f"TFM full decode incomplete: frames={decode.frames} expected={d['expected']} "
                                f"vspipe={decode.vspipe_rc} ffmpeg={decode.ffmpeg_rc} "
                                f"errors={decode.errors[:5]}")
        if not log.is_file():
            raise ArtifactError(f"TFM did not write its log: {log}")
        try:
            rows = parse_tfm_log(decode_text(log.read_bytes()), d["expected"])
        except ValueError as exc:
            raise ArtifactError(f"TFM log rejected: {exc}") from None
        motion = load_motion_trace(d["motion"]) if d.get("motion") else None
        analysis = analyze_tfm_rows(rows, motion_values=motion)
        mask = repair_mask([r["match_code"] for r in rows], [int(r["mic"]) for r in rows])
        if sum(mask) != analysis["repair_frame_count"]:
            raise ArtifactError("repair-set implementations disagree; refusing to build a map")
        ranges = repair_ranges(mask)
        atomic_write_bytes(d["di_map"], di_map_text(ranges).encode())
        atomic_write_json(d["report"], {"schema": "fstk-cadence-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "source_pad": d["pad"],
                                        "decoded_frames": decode.frames, "repair_ranges": ranges,
                                        "analysis": analysis})
        out = StepOutput(report=Path(d["report"]))
        out.measure("video.tfm_period5_pct", float(analysis["period5_agreement_pct"]))
        out.measure("video.tfm_unmatched", int(analysis["unmatched_count"]))
        out.measure("video.tfm_combed_count", int(analysis["combed_count"]))
        out.measure("video.tfm_combed_spans", [[s["start_frame"], s["end_frame_inclusive"]]
                                               for s in analysis["combed_spans"]])
        out.measure("video.inserts", int(analysis["insert_count"]))
        out.measure("video.repair_frames", int(analysis["repair_frame_count"]))
        out.measure("video.repair_pct", float(analysis["repair_frame_pct"]))
        probe = ctx.root("probe")
        out.artifacts["tfm_log"] = {"root": "probe", "name": log.relative_to(probe).as_posix()}
        out.artifacts["di_map"] = {"root": "probe", "name": Path(d["di_map"]).relative_to(probe).as_posix()}
        out.artifacts["cadence_summary"] = {"root": "reports", "name": "cadence.json"}
        return out

    def verify(self, ctx, plan, output):
        report = read_json(plan.data["report"])
        issues = []
        if report["decoded_frames"] != plan.data["expected"]:
            issues.append("decoded frame count changed")
        if report["analysis"]["frame_count"] != plan.data["expected"]:
            issues.append("analysis did not cover every frame")
        return issues
