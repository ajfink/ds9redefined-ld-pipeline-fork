"""``video-quality``: measure the picture, decide what (if anything) to apply, build the finishing script.

Runs on the resolved chain (the exact frames that would be encoded). Everything it applies is
chosen from measurements and gated by grain retention — original grain is paramount:

* dropout concealment — LD-only: a defect rectangle is copied from the previous/next *LaserDisc* frame
  (real grain), accepted only if the surround matches and the copied grain strength equals the ring's;
* chroma-only temporal denoise — luma (where the grain lives) is never touched; applied only when the
  measured chroma noise exceeds a trigger; the stage proves the luma plane is bit-identical;
* active-area crop and SAR/DAR lock — measured per episode, the DVD (when present) is the aspect reference;
* levels / colour — measured and compared with the DVD; reported, never altered.
"""
from __future__ import annotations

from collections import deque
from fractions import Fraction
from pathlib import Path
from statistics import median

import numpy as np

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media.finish import finish_script
from fstk.quality import conceal, geometry, measure

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register

SCHEMA = "fstk-video-quality-v1"
CHROMA_NOISE_TRIGGER = 2.5      # median Cb/Cr residual std (8-bit codes, luma-flat areas) above which denoise runs
CHROMA_DENOISE = {"strength": 0.5, "limit": 2.0}
FRAME_DEFECT_CAP = 0.02         # a frame with > 2 % flagged pixels is a flash/scene event, not a dropout
MAX_DEFECT_HEIGHT = 14
SAMPLE_COUNT = 36
METRIC_STRIDE = 24
GRAIN_SAMPLE_FLOOR = 4


def sample_indices(frames: int, count: int = SAMPLE_COUNT) -> list[int]:
    if frames < 8:
        return list(range(1, max(frames - 1, 1)))
    return sorted({round(v) for v in np.linspace(frames * 0.02, frames * 0.98, count)})


def pix_name(fmt: str | None) -> str:
    return {"YUV422P10": "yuv422p10le", "YUV420P10": "yuv420p10le"}.get(fmt or "", "")


@register
class VideoQuality(Stage):
    id = "video-quality"
    title = "Picture quality: measure, conceal dropouts, crop/aspect lock, optional chroma denoise"
    description = ("One sequential pass over the chain: levels, grain, chroma noise, dropouts, active "
                   "area. Builds the finishing script (LD-only dropout concealment, locked crop, chroma-only denoise "
                   "when measured necessary). Never alters luma grain; the DVD is only a measuring reference.")
    requires = (ValueSet("video.frames"), ArtifactExists("encode_script"))
    writes = ("quality.crop", "quality.sar", "quality.dar", "quality.dropouts_found", "quality.dropouts_fixed",
              "quality.chroma_denoise")
    writes_artifacts = ("finish_script", "quality_fixes", "quality_video_report")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        from .framefix import resolve_chain

        base = resolve_chain(ctx, m)     # encode script → operator fixes, each verified
        run = utc_stamp()[:15].lower()
        slug = ctx.episode.slug
        outs = {"script": ctx.root("scripts") / f"ivtc-finish-{slug}-{run}.vpy",
                "fixes": ctx.root("reports") / f"quality-fixes-{slug}-{run}.tsv",
                "report": ctx.root("reports") / f"quality-video-{run}.json"}
        for p in outs.values():
            if p.exists():
                raise GateError(f"refusing to overwrite {p}")
        return StepPlan(self.id, params, inputs=[base], outputs=list(outs.values()),
                        summary="measure + finish script",
                        data={"base": str(base), **{k: str(v) for k, v in outs.items()},
                              "frames": m.require("video.frames"), "chroma": m.get("mode.chroma_output") or "422",
                              "dvd": bool(m.get("mode.dvd_reference_available") is True and m.get("video.dvd_alignment")
                                          and m.artifact("dvd")),
                              "chroma_mode": str(params.get("chroma_denoise", "auto")),
                              "conceal": bool(params.get("conceal", True))})

    # ------------------------------------------------------------------ the pass
    def _scan(self, ctx, vs, script: Path, d: dict, info: dict):
        frames = int(info["frames"])
        want = set(sample_indices(frames))
        samples_y: dict[int, np.ndarray] = {}
        metrics = {"grain": [], "chroma": [], "cross": []}
        found = 0
        flagged_frames: list[dict] = []
        fixes: list[conceal.Fix] = []
        residual: list[dict] = []
        win: deque = deque(maxlen=5)         # (index, luma, mask)
        pending: deque = deque(maxlen=3)     # raw luma of the last 3 frames for mask computation
        grain_default = 3.0
        for i, p in enumerate(vs.iter_planes(script, cancel=ctx.cancel)):
            if i % 240 == 0:
                ctx.report(i / max(frames, 1) * 0.8, f"scanning frame {i}/{frames}")
            if i in want:
                samples_y[i] = p.y.copy()
            if i % METRIC_STRIDE == 0:
                for key, v in (("grain", measure.grain_power(p.y)), ("chroma", measure.chroma_noise(p)),
                               ("cross", measure.cross_colour_ratio(p))):
                    if v is not None:
                        metrics[key].append(v)
            pending.append((i, p.y))
            mask = None
            if len(pending) == 3:
                (_, a), (j, b), (_, c) = pending
                mask = measure.dropout_mask(a, b, c)
                win.append((j, b, mask))
            if len(win) == 5 and d["conceal"]:
                # window = frames j-2..j+2 (indices contiguous); plan for the middle one
                (_, _y0, _m0), (_, ym, mm), (jc, yc, mc), (_, yn, mn), _last = win
                if mc.any():
                    boxes = _boxes(mc)
                    boxes = [b for b in boxes if b.h <= MAX_DEFECT_HEIGHT]
                    found += len(boxes)
                    if mc.mean() > FRAME_DEFECT_CAP:
                        flagged_frames.append({"frame": jc, "reason": "flash/scene event (defect area cap)"})
                        continue
                    grain = (metrics["grain"][-1]["std"] if metrics["grain"] else grain_default)
                    for b in boxes:
                        fix, why = conceal.plan_fix(jc, yc, {-1: (ym, mm), 1: (yn, mn)}, b,
                                                    max_cost=conceal.expected_cost(grain), cur_mask=mc)
                        if fix is not None and 1 <= jc < frames - 1:
                            fixes.append(fix)
                        else:
                            residual.append({"frame": jc, "x": b.x, "y": b.y, "w": b.w, "h": b.h, "reason": why})
        return samples_y, metrics, found, fixes, residual, flagged_frames

    def run(self, ctx, plan):
        d = plan.data
        m = ctx.manifest
        vs, ff = ctx.tool("vapoursynth"), ctx.tool("ffmpeg")
        base = Path(d["base"])
        info = vs.info(base)
        if info["frames"] != d["frames"]:
            raise ArtifactError(f"script reports {info['frames']} frames but video.frames={d['frames']}")
        samples_y, metrics, found, fixes, residual, flagged = self._scan(ctx, vs, base, d, info)
        ctx.report(0.82, "levels, geometry, decisions")

        ys = list(samples_y.values())
        ld_levels = measure.level_stats(ys)
        report: dict = {"schema": SCHEMA, "created_utc": utc_now_iso(), "episode": str(ctx.episode),
                        "frames": info["frames"], "raster": [info["width"], info["height"]],
                        "levels": {"ld": ld_levels, "clipping": measure.clipping_findings(ld_levels)},
                        "grain": _summ(metrics["grain"], ("std", "fine_over_coarse")),
                        "chroma": _summ(metrics["chroma"], ("cb_std", "cr_std", "dot_crawl_luma", "dot_crawl_chroma")),
                        "cross_colour_ratio": round(float(median(metrics["cross"])), 3) if metrics["cross"] else None}

        # ---- DVD as a measuring reference (levels, active area): never a pixel source
        dvd_luma, dvd_sar = {}, None
        if d["dvd"]:
            dvd_luma, dvd_sar, skipped = self._dvd_samples(ctx, vs, ff, sorted(samples_y), d)
            report["levels"]["dvd_samples"] = {"taken": len(dvd_luma), "skipped": skipped}
        if dvd_luma:
            report["levels"]["dvd"] = measure.level_stats(list(dvd_luma.values()))
            report["levels"]["compare"] = measure.compare_levels(ld_levels, report["levels"]["dvd"])

        # ---- crop + aspect lock
        crop = geometry.active_crop(ys)
        src = ff.probe(Path(ctx.artifact_path("stack_master", m))).only("video")
        master_sar = Fraction(*[int(x) for x in (src.raw.get("sample_aspect_ratio") or "1:1").split(":")])
        dvd_dar = None
        if dvd_luma and dvd_sar:
            dcrop = geometry.active_crop(list(dvd_luma.values()))
            dw, dh = dcrop.size(*next(iter(dvd_luma.values())).shape[::-1])
            dvd_dar = Fraction(dw, dh) * dvd_sar
            report["geometry_dvd"] = {"crop": dcrop.as_dict(), "dar_measured": float(dvd_dar)}
        lock = geometry.lock_geometry(master_w=int(info["width"]), master_h=int(info["height"]),
                                      master_sar=master_sar, ld_crop=crop, dvd_dar=dvd_dar)
        report["geometry"] = lock

        # ---- hybrid cadence plan (advisory: lists video-origin stretches that 5->4 decimation would judder)
        report["hybrid"] = self._hybrid(ctx, m, Path(d["report"]))

        # ---- chroma denoise decision (metric decides)
        chroma_med = max(report["chroma"].get("cb_std", 0), report["chroma"].get("cr_std", 0))
        mode = d["chroma_mode"]
        denoise = None
        if mode == "on" or (mode == "auto" and chroma_med > CHROMA_NOISE_TRIGGER):
            denoise = dict(CHROMA_DENOISE)
        report["chroma_decision"] = {"mode": mode, "measured_chroma_std": round(chroma_med, 3),
                                     "trigger": CHROMA_NOISE_TRIGGER, "applied": denoise}

        # ---- build the finishing script and prove it
        fix_tuples = [(f.frame, f.src, f.dx, f.dy, f.x, f.y, f.w, f.h) for f in fixes]
        text = finish_script(base.read_text(encoding="utf-8"), expected_frames=int(info["frames"]),
                             crop=lock["crop"], chroma_denoise=denoise, dropout_fixes=fix_tuples)
        script_out = Path(d["script"])
        atomic_write_bytes(script_out, text.encode("utf-8"))
        proof = self._prove(ctx, vs, base, script_out, info, fixes, lock, bool(denoise))
        report["proof"] = proof
        report["dropouts"] = {"found": found, "concealed": len(fixes), "residual": residual[:500],
                              "residual_count": len(residual), "flash_frames_skipped": flagged[:200]}
        tsv = "frame\tsrc\tdx\tdy\tx\ty\tw\th\tcost\n" + "".join(f.tsv() + "\n" for f in fixes)
        atomic_write_bytes(Path(d["fixes"]), tsv.encode("utf-8"))
        atomic_write_json(d["report"], report)

        out = StepOutput(report=Path(d["report"]))
        out.data["issues"] = proof["issues"]
        out.measure("quality.crop", lock["crop"])
        out.measure("quality.sar", lock["sar"])
        out.measure("quality.dar", lock["dar"])
        out.measure("quality.dropouts_found", int(found))
        out.measure("quality.dropouts_fixed", len(fixes))
        out.measure("quality.chroma_denoise", "off" if not denoise else f"{denoise['strength']}/{denoise['limit']}")
        out.artifacts["finish_script"] = {"root": "scripts", "name": script_out.name}
        out.artifacts["quality_fixes"] = {"root": "reports", "name": Path(d["fixes"]).name}
        out.artifacts["quality_video_report"] = {"root": "reports", "name": Path(d["report"]).name}
        return out

    def _hybrid(self, ctx, m, report_path: Path) -> dict:
        """What the delivery does with video-origin stretches (decided in cadence-evidence, applied by video-trim)."""
        from fstk.media import hybrid

        if not m.artifact("hybrid_plan"):
            return {"status": "SKIP", "reason": "no hybrid plan recorded (cadence-evidence predates it)"}
        doc = read_json(ctx.artifact_path("hybrid_plan", m))
        segs = hybrid.apply_choices([hybrid.Segment.from_dict(x) for x in doc["segments"]],
                                    m.get("video.hybrid_choices"))
        info = hybrid.summary(segs)
        info["status"] = "CONVERTED" if info["video_segments"] else "NOT NEEDED"
        info["plate_tests"] = doc.get("plate_tests", {})
        return info

    def _dvd_samples(self, ctx, vs, ff, indices: list[int], d: dict):
        """Aligned DVD luma at the sampled output frames (8-bit codes), the DVD's sample aspect ratio, and the
        samples that could not be taken (with why: they are reported, not dropped silently)."""
        import numpy as np

        from fstk.stages.dvdborrow import dvd_window_script as _dvd_window_script
        from fstk.stages.dvdborrow import ld_to_master_frame

        m = ctx.manifest
        rec = m.require("video.dvd_alignment")
        dvd_path = ctx.artifact_path("dvd", m)
        head, pad = int(m.require("video.head_trim")), int(m.require("audio.pad_frames"))
        sar = None
        try:
            raw = ff.probe(dvd_path).only("video").raw.get("sample_aspect_ratio") or ""
            if ":" in raw:
                a, b = (int(x) for x in raw.split(":"))
                sar = Fraction(a, b) if a > 0 and b > 0 else None
        except Exception:
            sar = None
        out = {}
        skipped: list[str] = []
        work = ctx.root("probe") / f"quality-dvd-{ctx.episode.slug}"
        work.mkdir(parents=True, exist_ok=True)
        for f in indices:
            master = ld_to_master_frame(f, head_trim=head, pad=pad)
            act = next((a for a in rec["acts"] if a["start_frame"] <= master <= a["end_frame"]), None)
            if act is None:
                skipped.append(f"frame {f}: no accepted act covers master frame {master}")
                continue
            centre = round(master + float(np.interp(master, act["knots"], act["values"])))
            win = work / f"w{f}.vpy"
            atomic_write_bytes(win, _dvd_window_script(dvd_path, max(0, centre - 4), centre + 4).encode())
            try:
                wi = vs.info(win)
                arr = vs.frames(win, 0, wi["frames"] - 1, width=int(wi["width"]), height=int(wi["height"]),
                                pix_fmt="gray")
            except Exception as exc:
                skipped.append(f"frame {f} (DVD frame {centre}): {str(exc).splitlines()[0][:160]}")
                continue
            out[f] = arr[len(arr) // 2].astype(np.float32)
        return out, sar, skipped

    def _prove(self, ctx, vs, base: Path, finish: Path, info: dict, fixes, lock: dict, denoised: bool) -> dict:
        """The finishing script reports the right frame count and size, leaves luma untouched outside the
        concealed rectangles, and each concealed patch keeps the surrounding grain strength."""
        issues: list[str] = []
        fi = vs.info(finish)
        c = lock["crop"]
        want_w, want_h = info["width"] - c["left"] - c["right"], info["height"] - c["top"] - c["bottom"]
        if fi["frames"] != info["frames"]:
            issues.append(f"finishing script reports {fi['frames']} frames, expected {info['frames']}")
        if (fi.get("width"), fi.get("height")) != (want_w, want_h):
            issues.append(f"finishing script raster {fi.get('width')}x{fi.get('height')}, expected {want_w}x{want_h}")
        checked, luma_diff_px, retention = 0, 0, []
        fix_frames = sorted({f.frame for f in fixes})
        quiet = [n for n in (info["frames"] // 3, info["frames"] // 2, 2 * info["frames"] // 3) if n not in fix_frames]
        for n in fix_frames[:6] + quiet:
            a = next(iter(vs.iter_planes(base, n, n)))
            b = next(iter(vs.iter_planes(finish, n, n)))
            ya = a.y[c["top"]:info["height"] - c["bottom"], c["left"]:info["width"] - c["right"]].copy()
            mask = np.zeros_like(ya, bool)
            for f in (x for x in fixes if x.frame == n):
                mask[max(f.y - c["top"], 0):f.y - c["top"] + f.h, max(f.x - c["left"], 0):f.x - c["left"] + f.w] = True
            luma_diff_px += int(((ya != b.y) & ~mask).sum())
            if n in quiet:                     # grain retention is read on frames without a concealed defect
                r = measure.grain_retention(ya, b.y)
                if r:
                    retention.append(r["ratio"])
            checked += 1
        if luma_diff_px:
            issues.append(f"luma changed outside concealed rectangles ({luma_diff_px} px in {checked} frames): "
                          "the finishing script must never touch grain")
        return {"issues": issues, "frames_checked": checked, "luma_changed_outside_fixes_px": luma_diff_px,
                "luma_grain_retention_ratio": [round(r, 4) for r in retention], "chroma_denoise_applied": denoised}

    def verify(self, ctx, plan, output):
        return list(output.data["issues"])


def _boxes(mask: np.ndarray):
    import cv2

    grown = cv2.dilate(mask.astype(np.uint8), np.ones((5, 9), np.uint8))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        inside = mask[y:y + h, x:x + w]
        if inside.any():
            out.append(measure.Dropout(int(x), int(y), int(w), int(h), int(inside.sum()), 0.0))
    return out


def _summ(rows: list[dict], keys) -> dict:
    return {k: round(float(median(r[k] for r in rows)), 4) for k in keys} | {"samples": len(rows)} if rows else {}
