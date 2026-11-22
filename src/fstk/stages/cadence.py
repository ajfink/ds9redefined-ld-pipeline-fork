"""Cadence measurement stage (VapourSynth TFM on every platform).

``cadence-evidence`` replaces v38's Windows chain (AviSynth TFM log + ``build_map.ps1``) and the
macOS chain (``generate_cadence_evidence`` + ``build_map_vpy``) with one path:

1. write a TFM-log script over the stack master trimmed by ``audio.pad_frames``;
2. evaluate every frame (vspipe → FFmpeg null); the decoded count must equal
   ``stack.frames − audio.pad_frames`` and both processes must exit cleanly;
3. parse the TFM log, requiring exactly that many contiguous rows (fail closed);
4. analyse cadence (``fstk.media.cadence``) and build the repair map
   (``fstk.media.cadence_map``, v38 build_map.ps1 rules);
5. write the report and di-map as evidence.
6. **accept automatically** when the measurements look like every episode v38 completed
   (``AUTO_ACCEPT``, calibrated on 12 v38 episodes: period-5 89.9–93.1%, unmatched 0, repair
   2.2–4.6%, inserts 2–8). Owner decision 2026-11-21: the only routine human review is the A/V
   check, so ``cadence-review`` is held only when a measurement falls outside those ranges.

Re-validating this chain on Windows VapourSynth against v38's AviSynth output is still owed
(REFACTOR-PLAN Phase 4 exit criterion).
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import ArtifactError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, decode_text, read_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import hybrid
from fstk.media.cadence import analyze_tfm_rows, load_motion_trace, parse_tfm_log
from fstk.media.cadence_map import di_map_text, repair_mask, repair_ranges
from fstk.tools.vapoursynth import field_diff_script, read_field_diffs, tfm_log_script

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register

#: Automatic acceptance ranges (with margin) from 12 completed v38 episodes (S01E08–S02E02).
AUTO_ACCEPT = {"min_period5_pct": 85.0, "max_unmatched": 0, "max_repair_pct": 6.0, "max_inserts": 12}


def cadence_issues(analysis: dict) -> list[str]:
    issues = []
    if float(analysis["period5_agreement_pct"]) < AUTO_ACCEPT["min_period5_pct"]:
        issues.append(f"period-5 cadence {analysis['period5_agreement_pct']:.2f}% < {AUTO_ACCEPT['min_period5_pct']}%")
    if int(analysis["unmatched_count"]) > AUTO_ACCEPT["max_unmatched"]:
        issues.append(f"{analysis['unmatched_count']} unmatched field(s)")
    if float(analysis["repair_frame_pct"]) > AUTO_ACCEPT["max_repair_pct"]:
        issues.append(f"repair {analysis['repair_frame_pct']:.2f}% > {AUTO_ACCEPT['max_repair_pct']}%")
    if int(analysis["insert_count"]) > AUTO_ACCEPT["max_inserts"]:
        issues.append(f"{analysis['insert_count']} inserts > {AUTO_ACCEPT['max_inserts']}")
    return issues


@register
class CadenceEvidence(Stage):
    id = "cadence-evidence"
    title = "Measure cadence (TFM) and build the repair map"
    description = ("Full-side VapourSynth TFM log, cadence analysis, and repair ranges; accepted automatically "
                   "inside the ranges of every completed v38 episode, otherwise held for review.")
    requires = (ValueSet("stack.frames"), ValueSet("audio.pad_frames"), ArtifactExists("stack_master"))
    writes = ("video.tfm_period5_pct", "video.tfm_unmatched", "video.tfm_combed_count",
              "video.tfm_combed_spans", "video.inserts", "video.repair_frames", "video.repair_pct",
              "video.hybrid_seconds")
    writes_artifacts = ("tfm_log", "di_map", "cadence_summary", "hybrid_plan")
    resource = "compute"
    review = "cadence-review"
    review_when = "only when a cadence measurement is outside the v38-calibrated ranges"

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
                 "hybrid": probe / f"hybrid-plan-{slug}.json", "fd_script": ctx.root("scripts") / f"field-diff-{slug}.vpy",
                 "fd_log": probe / f"field-diff-{slug}.txt",
                 "report": ctx.root("reports") / "cadence.json"}
        motion = params.get("motion_trace")
        inputs = [master] + ([Path(motion)] if motion else [])
        return StepPlan(self.id, params, inputs=inputs,
                        outputs=[files["log"], files["di_map"], files["hybrid"], files["report"]],
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
        plan_doc = self._hybrid_plan(ctx, d, rows)
        atomic_write_json(d["hybrid"], plan_doc)
        atomic_write_json(d["report"], {"schema": "fstk-cadence-v1", "hybrid": plan_doc["summary"], "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "source_pad": d["pad"],
                                        "decoded_frames": decode.frames, "repair_ranges": ranges,
                                        "analysis": analysis, "auto_accept": AUTO_ACCEPT,
                                        "auto_accept_issues": cadence_issues(analysis)})
        out = StepOutput(report=Path(d["report"]))
        issues = cadence_issues(analysis)
        out.needs_review = bool(issues)
        out.notes += issues
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
        out.artifacts["hybrid_plan"] = {"root": "probe", "name": Path(d["hybrid"]).relative_to(probe).as_posix()}
        out.measure("video.hybrid_seconds", float(plan_doc["summary"]["video_seconds"]),
                    note=f"{plan_doc['summary']['video_segments']} video-origin stretch(es) converted at 23.976p; "
                         f"{plan_doc['summary']['plate_segments']} film plate(s) under a video overlay kept as film")
        return out

    def _hybrid_plan(self, ctx, d, rows) -> dict:
        """Video-origin stretches and, for each, whether its main picture is still a telecined film plate."""
        segments = hybrid.plan_segments(hybrid.classify_cycles(rows))
        cand = [s for s in segments if s.kind != hybrid.FILM]
        verdicts: dict[int, dict] = {}
        if cand:
            ranges = [s.source_frames for s in cand]
            fd_log = Path(d["fd_log"])
            if fd_log.exists():
                fd_log.rename(fd_log.with_name(f"{fd_log.stem}.superseded-{utc_stamp()}{fd_log.suffix}"))
            atomic_write_bytes(d["fd_script"], field_diff_script(Path(d["master"]), d["pad"], ranges, fd_log).encode())
            ctx.report(0.9, f"film-plate test on {len(cand)} video-origin stretch(es)")
            dec = ctx.tool("vapoursynth").full_decode(Path(d["fd_script"]), name="field-diff", cancel=ctx.cancel)
            if not dec.clean or not fd_log.is_file():
                raise ArtifactError(f"field-difference pass failed: vspipe={dec.vspipe_rc} ffmpeg={dec.ffmpeg_rc} "
                                    f"errors={dec.errors[:5]}")
            try:
                diffs = read_field_diffs(fd_log.read_text(encoding="ascii"), ranges)
            except ValueError as exc:
                raise ArtifactError(str(exc)) from None
            for s, fd in zip(cand, diffs, strict=True):
                verdicts[s.first_cycle] = hybrid.film_plate_verdict(fd)
        segments = [hybrid.Segment(s.kind, s.first_cycle, s.end_cycle,
                                   bool(verdicts.get(s.first_cycle, {}).get("plate", False))) for s in segments]
        return {"schema": "fstk-hybrid-plan-v1", "created_utc": utc_now_iso(), "frame_space": "post-pad source frames",
                "segments": [s.as_dict() for s in segments],
                "plate_tests": {str(k): v for k, v in verdicts.items()},
                "summary": hybrid.summary(segments)}

    def verify(self, ctx, plan, output):
        report = read_json(plan.data["report"])
        issues = []
        if report["decoded_frames"] != plan.data["expected"]:
            issues.append("decoded frame count changed")
        if report["analysis"]["frame_count"] != plan.data["expected"]:
            issues.append("analysis did not cover every frame")
        return issues
