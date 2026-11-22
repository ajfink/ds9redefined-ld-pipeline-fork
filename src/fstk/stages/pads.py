"""PAD audit and repair (v38 ``pad_audit.py`` + ``rife_padframes.py`` + ``padfix``; owner rules 2026-11-21).

``pad-audit`` finds which reference-map PAD frames actually rendered as a single black frame in
the delivered chain (the IVTC encode script, never an encoded video), proves each is black over
the full raster, and classifies each as a shot cut or not. ``pad-repair`` replaces them:

* not a cut → RIFE midpoint of the two neighbours;
* cut + DVD → the missing frame borrowed from the aligned DVD, registered to the LD raster and
  colour-matched to the neighbouring LD frame (never interpolated across a cut);
* cut without a DVD (or no unambiguous DVD match) → the previous frame is held one more frame.

The repaired chain is written as ``padfix_script`` (the base script plus a splice) and
``encode-final`` prefers it; the frame count is asserted unchanged. Nothing here needs a human:
the A/V check remains the only review.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import pad_repair as pr
from fstk.media import pads as pm
from fstk.tools import process
from fstk.tools.orc_project import reference_map_ranges, source_id_for_capture

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register

AUDIT_SCHEMA = "fstk-pad-audit-v1"
REPAIR_SCHEMA = "fstk-pad-repair-v1"
DVD_WINDOW = 15   # DVD grid frames either side of the aligned position


def base_script(ctx, m) -> Path:
    """The IVTC script the repaired chain is built on (never an already-padfixed one)."""
    return ctx.artifact_path("encode_script", m)


@register
class PadAudit(Stage):
    id = "pad-audit"
    title = "PAD audit (which placeholder frames rendered, and where the cuts are)"
    description = ("One pass of the IVTC chain: blackdetect and 48×36 descriptors; every reference-map PAD is "
                   "accounted for, proved black over the full raster, and classified as cut or non-cut.")
    requires = (ValueSet("video.frames"), ValueSet("video.head_trim"), ValueSet("audio.pad_frames"),
                ValueSet("stack.reference"), ArtifactExists("encode_script"), ArtifactExists("stack_project"))
    writes = ("pads.map_pads", "pads.rendered_isolated", "pads.trimmed_away", "pads.inside_break_black",
              "pads.never_rendered", "pads.cut_pads", "pads.cut_pad_frames", "pads.content_blacks_left_alone")
    writes_artifacts = ("pad_audit",)
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        script, project = base_script(ctx, m), ctx.artifact_path("stack_project", m)
        report = ctx.root("reports") / f"pad-audit-{utc_stamp()[:15].lower()}.json"
        return StepPlan(self.id, params, inputs=[script, project], outputs=[report],
                        summary="scan the IVTC chain for single-frame black PADs and classify cuts",
                        data={"script": str(script), "project": str(project), "report": str(report),
                              "frames": int(m.require("video.frames")), "head": int(m.require("video.head_trim")),
                              "pad": int(m.require("audio.pad_frames")), "reference": m.require("stack.reference"),
                              "accept_residual": bool(params.get("accept_residual", False))})

    def run(self, ctx, plan):
        d = plan.data
        vs = ctx.tool("vapoursynth")
        project = Path(d["project"])
        pads = reference_map_ranges(project, source_id=source_id_for_capture(project, d["reference"]))
        ctx.report(0.05, f"scanning {d['frames']} frames (blackdetect + descriptors)")
        scan = vs.scan(Path(d["script"]), cancel=ctx.cancel)
        if scan.frames != d["frames"]:
            raise ArtifactError(f"scan decoded {scan.frames} frames but video.frames={d['frames']}")
        projected = dict(zip(pads, pm.project_map_pads(pads, pad_trim=d["pad"], head_trim=d["head"]), strict=True))
        groups: list[list[int]] = []
        blips, spans = pm.blips_from_events(scan.events, groups=groups)
        res = pm.classify_pads(projected, frames=d["frames"], blips=blips, spans=spans, groups=groups)
        scores = pm.flank_scores(scan.thumbs)
        info = vs.info(Path(d["script"]))
        w, h = int(info["width"]), int(info["height"])
        rows, content, repairable = [], list(res.orphans), []
        for i, r in enumerate(res.rows):
            if r["status"] != "matched":
                continue
            f = r["blip"]
            ctx.report(0.2 + 0.7 * i / max(len(res.rows), 1), f"full-raster check of PAD {f}")
            frame = vs.frames(Path(d["script"]), f, f, width=w, height=h)[0]
            black, mx, mean = pm.near_black(frame)
            r.update(rgb_max=mx, rgb_mean=round(mean, 4), full_raster_black=black)
            if not black and not d["accept_residual"]:
                r["status"] = "not_black_full_raster"
                content.append(f)
                continue
            score = float(scores[f])
            run = next(r for r in pm.runs_of(res.candidates) if f in r)
            if len(run) > 1:   # adjacent PADs: score across the whole gap (the flanks of a member are PADs)
                a_, b_ = run[0] - 1, run[-1] + 1
                score = (float(1.0 - np.dot(pm._norm(scan.thumbs[a_]), pm._norm(scan.thumbs[b_])))
                         if a_ >= 0 and b_ < len(scan.thumbs) else float("nan"))
            r.update(flank_score=None if np.isnan(score) else round(score, 4), cut=pm.is_cut(score),
                     ambiguous=bool(not np.isnan(score) and pm.AMBIGUOUS_BAND[0] <= score <= pm.AMBIGUOUS_BAND[1]))
            repairable.append(f)
        rows = res.rows
        cuts = [r["blip"] for r in rows if r["status"] == "matched" and r.get("cut")]
        rep = {"schema": AUDIT_SCHEMA, "created_utc": utc_now_iso(), "episode": str(ctx.episode),
               "script": d["script"], "frames": d["frames"], "cut_threshold": pm.CUT_THRESHOLD,
               "counts": res.counts, "candidates": sorted(repairable), "cut_pad_frames": sorted(cuts),
               "content_blacks_left_alone": sorted(set(content)), "rows": rows,
               "blackdetect_events": len(scan.events), "scan_errors": scan.errors[:5]}
        atomic_write_json(d["report"], rep)
        out = StepOutput(report=Path(d["report"]))
        c = res.counts
        out.measure("pads.map_pads", c["map_pads"])
        out.measure("pads.rendered_isolated", c["rendered_isolated"])
        out.measure("pads.trimmed_away", c["trimmed_away"])
        out.measure("pads.inside_break_black", c["inside_break_black"])
        out.measure("pads.never_rendered", c["never_rendered"])
        out.measure("pads.cut_pads", len(cuts))
        out.measure("pads.cut_pad_frames", sorted(cuts))
        out.measure("pads.content_blacks_left_alone", sorted(set(content)))
        out.artifacts["pad_audit"] = {"root": "reports", "name": Path(d["report"]).name}
        return out

    def verify(self, ctx, plan, output):
        rep = read_json(plan.data["report"])
        issues = []
        if len(rep["candidates"]) != len(set(rep["candidates"])):
            issues.append("duplicate PAD candidates")
        if rep["scan_errors"]:
            issues.append(f"scan reported errors: {rep['scan_errors'][0]}")
        return issues


def _dvd_window_script(dvd: Path, first: int, last: int) -> str:
    """Progressive 23.976 window of the DVD between grid frames ``first``..``last``."""
    return "\n".join([
        "import vapoursynth as vs", "core = vs.core",
        f"src = core.bs.VideoSource({str(dvd)!r})",
        f"src = core.std.Trim(src, {int(first)}, {int(last)})",
        "src = core.std.SetFrameProps(src, _FieldBased=2)",
        "fm = core.tivtc.TFM(src, order=-1, PP=0, chroma=1, slow=2)",
        "out = core.tivtc.TDecimate(fm, mode=1, cycle=5, cycleR=1, chroma=1)",
        "out.set_output()", ""])


def ld_to_master_frame(out_frame: int, *, head_trim: int, pad: int) -> int:
    """Inverse of the v38 mapping ``(master − pad)*4//5 − head`` (nearest source frame of the cadence)."""
    return (out_frame + head_trim) * 5 // 4 + pad


def dvd_borrow(ctx, work: Path, f: int, ld_prev, ld_next, gap: int, *, head: int, pad: int):
    """DVD frames standing in for delivered frames ``f``..``f+gap−1``, registered to the LD raster and
    colour-matched to an LD neighbour → (Borrowed | None, reason). Also used by operator DVD fixes."""
    m = ctx.manifest
    rec = m.require("video.dvd_alignment")
    master = ld_to_master_frame(f, head_trim=head, pad=pad)
    act = next((a for a in rec["acts"] if a["start_frame"] <= master <= a["end_frame"]), None)
    if act is None:
        return None, "no accepted act covers this frame"
    off = float(np.interp(master, act["knots"], act["values"]))
    centre = round(master + off)
    first = max(0, centre - DVD_WINDOW)
    work.mkdir(parents=True, exist_ok=True)
    win = work / f"dvd-window-{f}.vpy"
    atomic_write_bytes(win, _dvd_window_script(ctx.artifact_path("dvd", m), first, centre + DVD_WINDOW + gap).encode())
    vs = ctx.tool("vapoursynth")
    info = vs.info(win)
    frames = vs.frames(win, 0, info["frames"] - 1, width=int(info["width"]), height=int(info["height"]))
    got = pr.borrow_dvd_frame(ld_prev, ld_next, frames, gap)
    return got, ("" if got else f"flank frames not found {gap + 1} apart in the aligned DVD window, or the "
                                "registered DVD frames do not reproduce the LD neighbour")


@register
class PadRepair(Stage):
    id = "pad-repair"
    title = "PAD repair (RIFE, DVD-borrowed frame on cuts, hold without a DVD)"
    description = ("Replaces every audited PAD: RIFE midpoint when not on a cut; on a cut the correct frame is "
                   "borrowed from the aligned DVD (registered, colour-matched to the neighbour) or, without a "
                   "DVD, the previous frame is held. Writes the padfix script encode-final uses.")
    requires = (ValueSet("video.frames"), ValueSet("video.head_trim"), ValueSet("audio.pad_frames"),
                ArtifactExists("encode_script"), ArtifactExists("pad_audit"))
    writes = ("pads.repaired",)
    writes_artifacts = ("pad_repair_frames", "pad_repair_report", "pad_replacements", "padfix_script", "pad_payload")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        audit = read_json(ctx.artifact_path("pad_audit", m))
        dvd = m.get("mode.dvd_reference_available") is True
        if dvd and not (m.get("video.dvd_alignment") and m.artifact("dvd")):
            raise GateError("DVD route: an accepted video.dvd_alignment and the dvd artifact are needed to borrow frames")
        run = utc_stamp()[:15].lower()
        slug = ctx.episode.slug
        work = ctx.root("probe") / f"pad-repair-{slug}-{run}"
        files = {"work": work, "script": ctx.root("scripts") / f"ivtc-padfix-{slug}-{run}.vpy",
                 "payload": work / f"payload-{slug}.mkv", "tsv": ctx.root("reports") / f"pad-replacements-{slug}-{run}.tsv",
                 "report": ctx.root("reports") / f"pad-repair-{slug}-{run}.json",
                 "frames_txt": ctx.root("reports") / f"{ctx.episode.code}-pad-repair-frames-{run}.txt"}
        script = base_script(ctx, m)
        rows = {r["blip"]: r for r in audit["rows"] if r["status"] == "matched"}
        cand = [int(f) for f in audit["candidates"]]
        methods = {f: pr.choose_method(cut=bool(rows[f].get("cut", True)), dvd=dvd) for f in cand}
        return StepPlan(self.id, params, inputs=[script], outputs=[files["script"], files["tsv"], files["report"]],
                        summary=f"{len(cand)} PAD(s): " + ", ".join(f"{k}×{sum(v == k for v in methods.values())}"
                                                                      for k in (pr.RIFE, pr.DVD_CUT, pr.HOLD)),
                        data={**{k: str(v) for k, v in files.items()}, "base": str(script), "dvd": dvd,
                              "cuts": {str(f): bool(rows[f].get("cut", True)) for f in cand},
                              "methods": {str(k): v for k, v in methods.items()}, "candidates": cand,
                              "frames": int(m.require("video.frames")), "head": int(m.require("video.head_trim")),
                              "pad": int(m.require("audio.pad_frames")), "label": ctx.episode.code})

    @staticmethod
    def _cut(d, run) -> bool:
        """Audit verdict for a single PAD; a run is re-scored at repair time from its real neighbours."""
        return all(d["cuts"].get(str(f), True) for f in run)

    def _borrow(self, ctx, d, f, ld_prev, ld_next, gap=1):
        """DVD window around the aligned position of output frame ``f`` → Borrowed | None (+reason)."""
        return dvd_borrow(ctx, Path(d["work"]), f, ld_prev, ld_next, gap, head=d["head"], pad=d["pad"])

    def _rife_run(self, ctx, rife, work, run, ld_prev, ld_next, w, h):
        """RIFE at t = k/(L+1) between the real neighbours of a run of L PADs → L uint16 images."""
        import cv2

        a, b = work / f"prev-{run[0]}.png", work / f"next-{run[0]}.png"
        pr.write_png(a, np.rint(pr.f32(ld_prev)).astype(np.uint8))   # the engine takes 8-bit
        pr.write_png(b, np.rint(pr.f32(ld_next)).astype(np.uint8))
        out = []
        for k, f in enumerate(run, start=1):
            o = work / f"rife-{f}.png"
            rife.interpolate(a, b, o, step=k / (len(run) + 1), cancel=ctx.cancel)
            bgr = cv2.imread(str(o), cv2.IMREAD_COLOR)
            if bgr is None or bgr.shape[:2] != (h, w):
                raise ArtifactError(f"RIFE output for PAD {f} is missing or the wrong size")
            # rife-ncnn-vulkan reads and writes 8-bit PNG (stb_image): that is the engine's precision
            out.append(pr.to_u16(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32)))
        return out

    def run(self, ctx, plan):
        d = plan.data
        vs, ffm = ctx.tool("vapoursynth"), ctx.tool("ffmpeg")
        base = Path(d["base"])
        info = vs.info(base)
        w, h = int(info["width"]), int(info["height"])
        work = Path(d["work"])
        work.mkdir(parents=True, exist_ok=True)
        dvd = d["dvd"]
        runs = pm.runs_of(d["candidates"])
        rife = ctx.tool("rife") if any(not r_cut for r_cut in (self._cut(d, r) for r in runs)) else None
        rows, repairs, images = [], [], []
        for n, run in enumerate(runs):
            ctx.check_cancel()
            ctx.report(0.05 + 0.8 * n / max(len(runs), 1), f"repairing PAD(s) {run[0]}..{run[-1]}")
            L = len(run)
            tri = vs.frames(base, run[0] - 1, run[-1] + 1, width=w, height=h)
            ld_prev, ld_next = tri[0], tri[-1]
            cut = self._cut(d, run)
            if L > 1:   # neighbours of a run are the first real frames either side: re-score across the whole gap
                cut = pm.is_cut(1.0 - float(pr.desc48(ld_prev) @ pr.desc48(ld_next)))
            tag = f"run of {L} adjacent PADs; " if L > 1 else ""
            method, imgs, extra, note = pr.choose_method(cut=cut, dvd=dvd), None, {}, ""
            if method == pr.DVD_CUT:
                got, why = self._borrow(ctx, d, run[0], ld_prev, ld_next, gap=L)
                if got is None:
                    method, note = pr.HOLD, f"hold-prev-scene-cut: {why}"
                else:
                    imgs = got.images
                    extra = {"gains": got.gains, "offsets": got.offsets, "colour_reference": got.reference,
                             "post_ncc": round(got.post_ncc, 4), "dvd_flanks": [got.match.p, got.match.q]}
                    note = (f"{tag}borrowed from DVD, colour-matched to the {got.reference} LD frame "
                            f"(post-NCC {got.post_ncc:.3f})")
            if method == pr.RIFE:
                imgs = self._rife_run(ctx, rife, work, run, ld_prev, ld_next, w, h)
                note = f"{tag}{rife.method} interpolation between the real neighbours"
            elif method == pr.HOLD and not note:
                note = (f"{tag}hold-prev-scene-cut: no DVD reference; the previous frame is held "
                        f"{'for each missing frame' if L > 1 else 'one more frame'}")
            label = {pr.RIFE: rife.method if rife else "", pr.DVD_CUT: "dvd-cut-frame", pr.HOLD: pr.HOLD}[method]
            for k, f in enumerate(run):
                slot = None
                if imgs is not None:
                    slot = len(images)
                    images.append(imgs[k])
                    png = work / f"pad-{f:06d}.png"
                    pr.write_png(png, imgs[k])
                else:
                    png = work / f"pad-{f:06d}-held.png"
                    pr.write_png(png, ld_prev)   # the held frame, at the precision it was read
                rows.append({"frame": f, "method": label, "png": str(png), "note": note, "slot": slot,
                             "run": [run[0], run[-1]], **extra})
                repairs.append({"frame": f, "method": method, "slot": slot})
        payload = None
        if images:
            ctx.report(0.88, "building the lossless payload clip")
            seq = work / "seq"
            seq.mkdir(exist_ok=True)
            for k, img in enumerate(images):
                pr.write_png(seq / f"{k:05d}.png", img)
            res = process.run([ffm.ffmpeg, "-y", "-hide_banner", "-nostdin", "-v", "error", "-framerate", "24000/1001",
                               "-i", str(seq / "%05d.png"), "-c:v", "ffv1", "-pix_fmt", "rgb48le", d["payload"]],
                              cancel=ctx.cancel)
            if res.returncode != 0 or not Path(d["payload"]).is_file():
                raise ArtifactError(f"payload clip failed: {res.describe()}")
            payload = d["payload"]
            shutil.rmtree(seq, ignore_errors=True)
        base_text = base.read_text(encoding="utf-8")
        text = pr.padfix_script(base_text, repairs=repairs, payload=payload, expected_frames=d["frames"])
        atomic_write_bytes(d["script"], text.encode())
        info2 = vs.info(Path(d["script"]))
        if info2["frames"] != d["frames"]:
            raise ArtifactError(f"padfix script reports {info2['frames']} frames; expected {d['frames']}")
        Path(d["tsv"]).write_text(pr.replacements_tsv(d["label"], rows), encoding="utf-8")
        # v38's list format, read by the QC identity gate (every listed frame must have a replacement)
        Path(d["frames_txt"]).write_text("".join(f"{r['frame']}\n" for r in rows), encoding="utf-8")
        atomic_write_json(d["report"], {"schema": REPAIR_SCHEMA, "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "base_script": d["base"], "padfix": d["script"],
                                        "payload": payload, "rows": rows,
                                        "methods": {k: sum(r["method"] == k for r in rows)
                                                    for k in {r["method"] for r in rows}}})
        out = StepOutput(report=Path(d["report"]))
        out.measure("pads.repaired", len(rows))
        out.artifacts.update({"pad_repair_frames": {"root": "reports", "name": Path(d["frames_txt"]).name},
                              "pad_repair_report": {"root": "reports", "name": Path(d["report"]).name},
                              "pad_replacements": {"root": "reports", "name": Path(d["tsv"]).name},
                              "padfix_script": {"root": "scripts", "name": Path(d["script"]).name}})
        if payload:
            out.artifacts["pad_payload"] = {"path": payload}
        return out

    def verify(self, ctx, plan, output):
        rep = read_json(plan.data["report"])
        frames = [r["frame"] for r in rep["rows"]]
        return [] if frames == sorted(set(frames)) == sorted(plan.data["candidates"]) else ["repair rows do not match the audit"]
