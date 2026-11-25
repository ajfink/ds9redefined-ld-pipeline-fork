"""``dvd-alignment``: DVD ↔ LD picture alignment, accepted automatically when every check passes.

Ports v38 ``extract_video_descriptors.py`` + ``measure_dvd_alignment.py``; the visual review
(``build_alignment_review_assets.py`` → ``dvd_alignment_review.py`` → ``record_dvd_alignment_review.py``)
becomes a fallback. Owner decision 2026-11-21: automate DVD picture alignment as far as possible.

* Descriptors: both videos are decoded **sequentially** to 48×36 grey (area scaling), with no
  seek, no ``setpts``/``fps`` filter and no explicit ``-fps_mode``. This is v38's validated
  extraction; those options changed E05's descriptor count. Both grids must be 30000/1001.
  Descriptors of an unchanged source (size, mtime, head+tail hash) are reused.
* Alignment: ``fstk.media.alignment.measure``. A forward Viterbi path (with an unmatched state
  for LD-only material) is modelled as acts. Each act is a continuous offset curve that follows
  the transfers' speed difference and wander. Every textured frame is checked against the
  curves, and a reverse (DVD→LD) path must land on them.
* Decision: ``AUTO-ACCEPTED`` commits at once. ``NEEDS-REVIEW`` holds the values for the
  ``dvd-alignment`` review, with the failed checks listed in the report.
"""
from __future__ import annotations

import csv
import json
from fractions import Fraction
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError, FstkError, GateError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.hashing import SCOPE_HEAD_TAIL, Fingerprint
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import alignment as al
from fstk.media import alignment_visual as vis

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueIs, register

GRID = Fraction(30000, 1001)
DESC_SCHEMA = "fstk-video-descriptors-v1"
REPORT_SCHEMA = "fstk-dvd-alignment-v1"
TUNABLE = tuple(al.DEFAULTS)


def _source_id(path: Path) -> dict:
    fp = Fingerprint.of(path, scope=SCOPE_HEAD_TAIL)
    return {"name": path.name, "size_bytes": fp.size_bytes, "mtime_ns": fp.mtime_ns,
            "sha256": fp.sha256, "scope": fp.scope}


def _reusable(ctx, key: str, source: Path) -> Path | None:
    """An earlier descriptor file whose metadata names this exact source: the one the manifest records, else the
    newest one in the probe folder (a run held for review and rejected records none, yet its descriptors are as
    good: S02E02 re-decoded its 2.6-hour master for 14 minutes on every redo)."""
    current = _source_id(source)

    def matches(path: Path) -> bool:
        meta = path.with_name(path.name + ".meta.json")
        if not (path.is_file() and meta.is_file()):
            return False
        try:
            recorded = read_json(meta).get("source") or {}
        except (OSError, ValueError):
            return False
        return all(recorded.get(k) == current[k] for k in ("size_bytes", "sha256", "scope"))

    candidates: list[Path] = []
    if ctx.manifest.artifact(key):
        candidates.append(ctx.artifact_path(key))
    role = key.removesuffix("_descriptors")
    candidates += sorted(ctx.root("probe").glob(f"{ctx.episode.code}-{role}-desc-*.npy"), reverse=True)
    return next((c for c in candidates if matches(c)), None)


@register
class DvdAlignment(Stage):
    id = "dvd-alignment"
    title = "DVD picture alignment"
    description = ("Sequential 48×36 descriptors of the stack master and DVD; continuity-constrained "
                   "alignment with an unmatched state; dense and reverse checks; auto-accepted when "
                   "every check passes, otherwise held for review.")
    requires = (ValueIs("mode.dvd_reference_available", True), ArtifactExists("dvd"), ArtifactExists("stack_master"))
    writes = ("video.dvd_alignment", "video.dvd_origin_master_frame")
    writes_artifacts = ("master_descriptors", "dvd_descriptors", "align_npy", "alignment_report",
                        "alignment_summary", "alignment_sheet")
    resource = "compute"
    review = "dvd-alignment"
    review_when = "only when an automatic check fails"

    def plan(self, ctx, params):
        m = ctx.manifest
        unknown = sorted(set(params) - set(TUNABLE) - {"redecode"})
        if unknown:
            raise GateError(f"unknown dvd-alignment parameter(s) {unknown}; tunable: {list(TUNABLE)}")
        master, dvd = ctx.artifact_path("stack_master", m), ctx.artifact_path("dvd", m)
        for p in (master, dvd):
            if not p.is_file():
                raise GateError(f"alignment input missing: {p}")
        run = f"{utc_stamp()}"
        probe = ctx.root("probe")
        code = ctx.episode.code
        reuse = {}
        if not params.get("redecode"):
            reuse = {role: _reusable(ctx, f"{role}_descriptors", src)
                     for role, src in (("master", master), ("dvd", dvd))}
        desc = {}
        outputs = []
        for role in ("master", "dvd"):
            if reuse.get(role):
                desc[role] = {"path": str(reuse[role]), "reused": True}
            else:
                path = probe / f"{code}-{role}-desc-{run}.npy"
                desc[role] = {"path": str(path), "reused": False}
                outputs += [path, path.with_name(path.name + ".meta.json")]
        names = {"npy": probe / f"{code}-dvd-alignment-{run}.npy",
                 "tsv": probe / f"{code}-dvd-alignment-{run}.tsv",
                 "json": ctx.root("reports") / f"dvd-alignment-{run}.json",
                 "png": ctx.root("reports") / f"dvd-alignment-{run}-sheet.png"}
        outputs += list(names.values())
        inputs = [master, dvd] + [Path(d["path"]) for d in desc.values() if d["reused"]]
        tuned = {k: params[k] for k in TUNABLE if k in params}
        return StepPlan(self.id, params, inputs=inputs, outputs=outputs,
                        input_scope={str(master): SCOPE_HEAD_TAIL, str(dvd): SCOPE_HEAD_TAIL},
                        summary=("descriptors: " + ", ".join(f"{r} {'reused' if d['reused'] else 'decode'}"
                                                             for r, d in desc.items())),
                        data={"master": str(master), "dvd": str(dvd), "desc": desc, "params": tuned,
                              **{k: str(v) for k, v in names.items()}})

    # ------------------------------------------------------------------ run
    def _descriptors(self, ctx, role: str, source: Path, spec: dict, base: float, span: float) -> np.ndarray:
        path = Path(spec["path"])
        if spec["reused"]:
            arr = np.load(path, mmap_mode="r")
            if arr.ndim != 2 or arr.shape[1] != al.FEATURES:
                raise ArtifactError(f"reused {role} descriptors have shape {arr.shape}")
            return arr
        ff = ctx.tool("ffmpeg")
        stream = ff.probe(source).only("video")
        rate, avg = stream.raw.get("r_frame_rate"), stream.raw.get("avg_frame_rate")
        if stream.frame_rate != GRID:
            raise GateError(f"{role} video grid is {rate}; direct alignment needs 30000/1001")
        ctx.report(base, f"decoding {role} descriptors (sequential; no seek)")
        arr = ff.gray_thumbnails(source, al.W, al.H, cancel=ctx.cancel)
        if len(arr) < 1000:
            raise ArtifactError(f"{role} descriptor output is unexpectedly short: {len(arr)} frames")
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, arr)
        atomic_write_json(path.with_name(path.name + ".meta.json"), {
            "schema": DESC_SCHEMA, "episode": str(ctx.episode), "role": role, "source": _source_id(source),
            "ffprobe_r_frame_rate": rate, "ffprobe_avg_frame_rate": avg, "output_grid_fps": "30000/1001",
            "rate_mismatch": bool(rate and avg and rate != avg), "descriptor_size": [al.W, al.H],
            "frames": len(arr), "coverage_s": len(arr) / float(GRID), "stream_nb_frames": stream.raw.get("nb_frames"),
            "decode": "ffmpeg -i <src> -map 0:v:0 -vf scale=48:36:flags=area,format=gray -f rawvideo (sequential)",
            # FFmpeg builds differ in where they place soft-telecine (RFF) duplicates on a 29.97 grid
            # (S01E20: 4.4 vs the Windows build put the duplicate one frame apart in ~13% of rows).
            "ffmpeg": ff.version(),
            "created_utc": utc_now_iso()})
        ctx.report(base + span, f"{role}: {len(arr)} descriptors")
        return arr

    def run(self, ctx, plan):
        d = plan.data
        master = self._descriptors(ctx, "master", Path(d["master"]), d["desc"]["master"], 0.0, 0.25)
        ctx.check_cancel()
        dvd = self._descriptors(ctx, "dvd", Path(d["dvd"]), d["desc"]["dvd"], 0.25, 0.1)
        ctx.check_cancel()
        res = al.measure(master, dvd, params=d["params"],
                         progress=lambda f, msg: ctx.report(0.35 + 0.6 * f, msg))
        path = al.path_array(res)
        if len(path) < 2 or not res.acts:
            raise GateError("no DVD picture matched the stack master (is this the right DVD title/episode?); "
                            f"{int(res.matched.sum())}/{len(res.idxs)} samples matched")
        np.save(d["npy"], path)
        with open(d["tsv"], "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(("master_frame", "master_time_s", "dvd_minus_master_frames", "subframe_offset", "model_offset",
                        "ncc", "local_margin", "flat", "state"))
            for i, o, sb, m, n, g, fl in zip(res.idxs, res.offsets, res.sub, res.matched, res.ncc, res.margin,
                                             res.flat, strict=True):
                act = res.act_for(int(i))
                w.writerow((int(i), f"{i / al.FPS:.6f}", int(o) if m else "", "" if np.isnan(sb) else f"{sb:.3f}",
                            "" if act is None else f"{float(act.offset(float(i))):.3f}",
                            "" if np.isnan(n) else f"{n:.6f}", "" if np.isnan(g) else f"{g:.6f}", int(fl),
                            "matched" if m else "unmatched"))
        fit = self._picture_check(ctx, d, res, len(master), len(dvd))
        summary = res.summary()
        names = {role: Path(spec["path"]).name for role, spec in d["desc"].items()}
        report = {"schema": REPORT_SCHEMA, "episode": str(ctx.episode), "created_utc": utc_now_iso(),
                  "master": Path(d["master"]).name, "dvd": Path(d["dvd"]).name, "descriptors": names,
                  "descriptor_frames": {"master": len(master), "dvd": len(dvd)},
                  "alignment_path": Path(d["npy"]).name, "diagnostics": Path(d["tsv"]).name, **summary,
                  "picture_check": {k: v for k, v in fit.items() if k != "view"},
                  "picture_sheet": Path(d["png"]).name,
                  "review_summary": self._review_summary(res, fit),
                  "review_view": {**fit["view"], "reasons": list(res.reasons)}}
        atomic_write_json(d["json"], report)
        out = StepOutput(report=Path(d["json"]))
        record = {k: summary[k] for k in ("decision", "reasons", "acts", "breaks", "common_slope_ppm", "consistency",
                                          "reverse_agreement", "origin_master_frame", "matched_samples", "samples",
                                          "params")}
        record["report"] = Path(d["json"]).name
        note = (f"{res.decision}: {len(res.acts)} act(s), common slope {summary['common_slope_ppm']} ppm"
                + (f"; {len(res.reasons)} check(s) failed" if res.reasons else ""))
        out.measure("video.dvd_alignment", json.loads(json.dumps(record)), note=note)
        if res.origin_master_frame is not None:
            out.measure("video.dvd_origin_master_frame", res.origin_master_frame,
                        note="first textured stack-master sample matched to DVD picture")
        for role, spec in d["desc"].items():
            out.artifacts[f"{role}_descriptors"] = {"root": "probe", "name": Path(spec["path"]).name}
        out.artifacts["align_npy"] = {"root": "probe", "name": Path(d["npy"]).name}
        out.artifacts["alignment_report"] = {"root": "probe", "name": Path(d["tsv"]).name}
        out.artifacts["alignment_summary"] = {"root": "reports", "name": Path(d["json"]).name}
        out.artifacts["alignment_sheet"] = {"root": "reports", "name": Path(d["png"]).name}
        out.needs_review = res.decision != "AUTO-ACCEPTED"
        out.data["decision"] = res.decision
        return out

    def _picture_check(self, ctx, d: dict, res, master_frames: int, dvd_frames: int) -> dict:
        """Spot-check the alignment on real pictures and write the picture sheet (never fails the stage: the
        sheet is evidence for a person, so a problem here is reported in the sheet and the report instead)."""
        png = Path(d["png"])
        try:
            spots = vis.choose_spots(res, dvd_frames)
            if not spots:
                raise ArtifactError("no textured, matched picture to spot-check")
            ctx.report(0.95, f"spot-checking {len(spots)} aligned pictures")
            ff = ctx.tool("ffmpeg")
            want_m, want_d = vis.wanted_indices(spots)
            got_m = ff.frames_by_index(d["master"], want_m, *vis.SHOT, cancel=ctx.cancel, frame_count=master_frames)
            ctx.check_cancel()
            got_d = ff.frames_by_index(d["dvd"], want_d, *vis.SHOT, cancel=ctx.cancel, frame_count=dvd_frames)
            for sp in spots:
                if sp.master_frame in got_m:
                    vis.judge(sp, got_m[sp.master_frame], got_d)
                else:
                    sp.verdict = vis.UNSURE
            result = vis.assess(spots)
            vis.render_sheet(spots, got_m, got_d, png, title=f"DVD alignment spot checks — {ctx.episode}",
                             summary=result["text"])
            fit = {**result, "points": [sp.to_dict() for sp in spots]}
            fit["view"] = vis.review_view(res, spots, fit, sheet=png.name, duration_s=master_frames / al.FPS,
                                          rendered=True)
            return fit
        except (FstkError, OSError, ValueError, KeyError) as exc:
            if ctx.cancel.is_set():
                raise
            why = f"picture spot checks could not be made: {exc}"
            vis.render_note(png, why)
            fit = {"verdict": "NOT CHECKED", "text": why, "checked": 0, "agree": 0, "points": [],
                   "recommendation": "use the numeric reasons below"}
            fit["view"] = vis.review_view(res, [], fit, sheet=png.name, duration_s=master_frames / al.FPS,
                                          rendered=False)
            return fit

    @staticmethod
    def _review_summary(res, fit: dict) -> str:
        lines = [f"Automatic picture check: {fit['verdict']}. {fit['text']}"]
        if res.reasons:
            lines.append("Why the numbers held it for review: " + "; ".join(res.reasons) + ".")
        else:
            lines.append("All of the numeric checks passed too.")
        lines.append("Look at the spots: each shows a LaserDisc frame and the DVD frame the alignment pairs with it "
                     "(Blink flips between them). Approve if they show the same moment; reject if they are clearly "
                     "out of step.")
        return "\n".join(lines)

    def verify(self, ctx, plan, output):
        d, issues = plan.data, []
        try:
            path = np.load(d["npy"])
            if path.ndim != 2 or path.shape[1] != 2 or len(path) < 2:
                issues.append(f"alignment path has shape {path.shape}")
            elif (np.diff(path[:, 0]) <= 0).any():
                issues.append("alignment path master frames are not increasing")
        except (OSError, ValueError) as exc:
            issues.append(f"alignment path unreadable: {exc}")
        rep = read_json(d["json"])
        if rep.get("decision") != output.data.get("decision"):
            issues.append("report decision differs from the stage result")
        return issues
