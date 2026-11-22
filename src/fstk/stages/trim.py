"""``video-trim``: head/tail trims, ``video.delta_s``, output frame count and the encode script.

Both routes. With a DVD, the head is the alignment's programme origin. Without one, it is the fade-up
after the LD front matter that ``media.trim.no_dvd_programme_start`` finds from the first two
minutes of the master (prior from 12 v38 episodes). Steps:

1. Write the IVTC/PAD-repair script untrimmed, in probe mode (``cadence_map.ivtc_vpy_script``):
   every source frame is tagged with its index before TFM/TDecimate. Evaluating it
   (``vspipe --info``) records the exact master frame of the head and tail output frames.
2. Decide the trims (``fstk.media.trim.decide``) from the accepted DVD alignment (programme
   origin, LD-only tail) and the master descriptors (black around the fade-up).
3. Write the final encode script with those trims and the expected frame count, and confirm with
   ``vspipe --info`` that it delivers exactly that many 24000/1001 frames.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import alignment as al
from fstk.media.cadence_map import ivtc_vpy_script, parse_ranges
from fstk.media.trim import decide, no_dvd_programme_start, tail_ld_only_start

from .base import ArtifactExists, Custom, Stage, StepOutput, StepPlan, ValueSet, register

HEAD_PROBE_MARGIN = 600       # output frames beyond the expected head trim point
TAIL_PROBE = 3000             # output frames probed at the tail


def _route(ctx, manifest):
    dvd = manifest.get("mode.dvd_reference_available")
    if dvd is True:
        rec = manifest.get("video.dvd_alignment") or {}
        ok = (bool(rec.get("acts")) and rec.get("origin_master_frame") is not None
              and bool(manifest.artifact("master_descriptors")) and bool(manifest.artifact("alignment_report")))
        return ok, "DVD route: accepted alignment" if ok else "DVD route needs an accepted video.dvd_alignment"
    if dvd is False:
        ok = manifest.get("mode.no_dvd_proceed_approved") is True
        return ok, "no-DVD route approved" if ok else "no-DVD route needs mode.no_dvd_proceed_approved"
    return False, "record mode.dvd_reference_available first"


@register
class VideoTrim(Stage):
    id = "video-trim"
    title = "Trims, DELTA and the encode script"
    description = ("Exact IVTC frame map (tagged probe); head at the programme's fade-up (DVD origin, or the "
                   "v38-prior fade-up without a DVD); tail only if the LD has picture the DVD lacks; writes "
                   "the verified encode script.")
    requires = (Custom("route", _route), ValueSet("audio.pad_frames"), ValueSet("stack.frames"),
                ValueSet("video.repair_frames"), ArtifactExists("stack_master"), ArtifactExists("di_map"))
    writes = ("video.head_trim", "video.tail_trim", "video.frames", "video.duration_s", "video.delta_s",
              "video.fade_up_output")
    writes_artifacts = ("encode_script",)
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        repair = params.get("repair")
        ranges = parse_ranges(ctx.artifact_path("di_map", m).read_text(encoding="utf-8"))
        if repair is None:
            repair = "qtgmc" if ranges else "none"
        if repair not in ("qtgmc", "pp2", "none"):
            raise GateError("param repair must be qtgmc, pp2 or none")
        run = utc_stamp()[:15].lower()
        slug = ctx.episode.slug
        files = {"probe_script": ctx.root("scripts") / f"ivtc-probe-{slug}-{run}.vpy",
                 "probe_json": ctx.root("probe") / f"ivtc-probe-{slug}-{run}.json",
                 "script": ctx.root("scripts") / f"ivtc-encode-{slug}-{run}.vpy",
                 "report": ctx.root("reports") / f"video-trim-{run}.json"}
        dvd = m.get("mode.dvd_reference_available") is True
        keys = ("stack_master", "di_map") + (("master_descriptors", "alignment_report") if dvd else ())
        inputs = [ctx.artifact_path(k, m) for k in keys]
        return StepPlan(self.id, params, inputs=inputs, outputs=[files["script"], files["probe_json"], files["report"]],
                        input_scope={str(inputs[0]): "head-tail"},
                        summary=f"probe the IVTC frame map, trim, write the encode script (repair {repair})",
                        data={**{k: str(v) for k, v in files.items()}, "repair": repair, "ranges": ranges, "dvd": dvd,
                              "master": str(inputs[0]), "pad": int(m.require("audio.pad_frames")),
                              "stack_frames": int(m.require("stack.frames"))})

    def _origin(self, ctx, d, m) -> tuple[int, np.ndarray, int | None, list[str]]:
        """(programme start master frame, flat mask, LD-only tail start, notes) for the episode's route."""
        if d["dvd"]:
            rec = m.require("video.dvd_alignment")
            desc = np.load(ctx.artifact_path("master_descriptors", m), mmap_mode="r")
            flat = al.flat_rows(np.asarray(desc), al.DEFAULTS["flat_std"])
            with open(ctx.artifact_path("alignment_report", m), newline="", encoding="utf-8") as f:
                samples = list(csv.DictReader(f, delimiter="\t"))
            step = int((rec.get("params") or {}).get("step", 15))
            tail_cut = tail_ld_only_start(samples, int(rec["acts"][-1]["end_frame"]), step)
            return int(rec["origin_master_frame"]), flat, tail_cut, []
        ctx.report(0.02, "reading the first two minutes of the master (no DVD: fade-up rule)")
        head = ctx.tool("ffmpeg").gray_thumbnails(Path(d["master"]), al.W, al.H, cancel=ctx.cancel,
                                                  max_frames=int(120 * al.FPS))
        flat = al.flat_rows(head, al.DEFAULTS["flat_std"])
        try:
            start, notes = no_dvd_programme_start(flat)
        except ValueError as exc:
            raise GateError(f"no-DVD programme start: {exc}") from None
        return start, flat, None, notes

    def run(self, ctx, plan):
        d, m = plan.data, ctx.manifest
        vs = ctx.tool("vapoursynth")
        origin, flat, tail_cut, route_notes = self._origin(ctx, d, m)
        expected_head = max(0, (origin - d["pad"]) * 4 // 5)
        script = ivtc_vpy_script(d["master"], d["pad"], 0, 0, [tuple(r) for r in d["ranges"]], d["repair"],
                                 probe_json=d["probe_json"], probe_head=expected_head + HEAD_PROBE_MARGIN,
                                 probe_tail=TAIL_PROBE)
        Path(d["probe_script"]).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(d["probe_script"], script.encode())
        ctx.report(0.05, "probing the IVTC frame map (head and tail)")
        info = vs.info(Path(d["probe_script"]))
        if not Path(d["probe_json"]).is_file():
            raise ArtifactError("the IVTC probe did not write its frame map (FstkN property not readable?)")
        probe = read_json(d["probe_json"])
        if probe.get("frames") != info["frames"]:
            raise ArtifactError(f"probe frame count {probe.get('frames')} != vspipe {info['frames']}")
        try:
            dec = decide(probe, flat=flat, origin=origin, tail_cut=tail_cut)
        except ValueError as exc:
            raise GateError(f"trim decision: {exc}") from None
        dec.notes = route_notes + dec.notes
        final = ivtc_vpy_script(d["master"], d["pad"], dec.head_trim, dec.tail_trim, [tuple(r) for r in d["ranges"]],
                                d["repair"], expected_frames=dec.frames)
        atomic_write_bytes(d["script"], final.encode())
        ctx.report(0.6, "confirming the trimmed script")
        info2 = vs.info(Path(d["script"]))
        if info2["frames"] != dec.frames or (info2["fps_num"], info2["fps_den"]) != (24000, 1001):
            raise ArtifactError(f"trimmed script gives {info2['frames']} frames at {info2['fps_num']}/{info2['fps_den']}; "
                                f"expected {dec.frames} at 24000/1001")
        fade_out = None
        for k, mf in sorted((int(a), int(b)) for a, b in probe["map"].items()):
            if mf >= dec.fade_up_master_frame:
                fade_out = k - dec.head_trim
                break
        report = {"schema": "fstk-video-trim-v1", "episode": str(ctx.episode), "created_utc": utc_now_iso(),
                  "decision": dec.summary(), "origin_master_frame": origin, "route": "dvd" if d["dvd"] else "no-dvd", "tail_ld_only_from": tail_cut,
                  "repair": d["repair"], "repair_ranges": len(d["ranges"]), "probe": d["probe_json"],
                  "script": d["script"], "vspipe_info": info2,
                  "v38_priors": "delta_s 49.5–52.4 s and tail_trim 0 across 12 completed v38 episodes"}
        atomic_write_json(d["report"], report)
        out = StepOutput(report=Path(d["report"]))
        why = f"head at master {dec.head_master_frame} (fade-up {dec.fade_up_master_frame}); " + "; ".join(dec.notes)
        out.measure("video.head_trim", dec.head_trim, note=why)
        out.measure("video.tail_trim", dec.tail_trim, note="; ".join(dec.notes) or "LD and DVD end together")
        out.measure("video.frames", dec.frames, note="vspipe --info on the trimmed encode script")
        out.measure("video.duration_s", dec.frames * 1001 / 24000)
        out.measure("video.delta_s", dec.delta_s, note=f"exact: master frame {dec.head_master_frame} via the IVTC probe")
        if fade_out is not None:
            out.measure("video.fade_up_output", int(fade_out))
        out.artifacts["encode_script"] = {"root": "scripts", "name": Path(d["script"]).name}
        return out

    def verify(self, ctx, plan, output):
        rep = read_json(plan.data["report"])
        return [] if rep["vspipe_info"]["frames"] == rep["decision"]["frames"] else ["frame count changed"]
