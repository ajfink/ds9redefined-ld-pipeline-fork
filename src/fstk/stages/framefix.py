"""Operator frame fixes: ``frame-fixes`` (applies them to the chain) and ``fix-preview`` (shows one before choosing).

The fixes live in the manifest (``video.manual_fixes``), chosen in the final review from the contact sheet. The
chain the final encode reads is always resolved the same way (:func:`resolve_chain`): the IVTC encode script,
then the PAD splice, then the operator fixes, each guarded against being built on an older link, and the
finishing script (crop / concealment / chroma) on top. Everything is applied to the lossless master's chain and
encoded once; no encoded file is ever patched.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import manual_fix as mf
from fstk.media import pad_repair as pr
from fstk.quality.measure import grain_retention
from fstk.tools import process

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register
from .pads import dvd_borrow

#: interpolated frames whose grain falls below this share of their neighbours' are flagged in the report
GRAIN_FLAG = 0.7
#: neighbours either side of a fix less alike than this are a shot change: interpolation is refused
MIN_NEIGHBOUR_NCC = 0.6


def resolve_chain(ctx, m) -> Path:
    """The script the finishing step builds on: encode script → PAD splice → operator fixes (each verified)."""
    base = ctx.artifact_path("encode_script", m)
    text = base.read_text(encoding="utf-8")
    if m.artifact("padfix_script"):
        p = ctx.artifact_path("padfix_script", m)
        if not pr.padfix_matches_base(p.read_text(encoding="utf-8"), text):
            raise GateError("padfix_script was built from an older encode script; re-run pad-audit and pad-repair")
        base, text = p, p.read_text(encoding="utf-8")
    if m.artifact("manualfix_script"):
        p = ctx.artifact_path("manualfix_script", m)
        if not mf.manualfix_matches_base(p.read_text(encoding="utf-8"), text):
            raise GateError("manualfix_script was built on an older chain; re-run frame-fixes")
        base = p
    return base


def _pre_fix_chain(ctx, m) -> Path:
    """The chain operator fixes are applied to (encode script → PAD splice)."""
    base = ctx.artifact_path("encode_script", m)
    if m.artifact("padfix_script"):
        p = ctx.artifact_path("padfix_script", m)
        if not pr.padfix_matches_base(p.read_text(encoding="utf-8"), base.read_text(encoding="utf-8")):
            raise GateError("padfix_script was built from an older encode script; re-run pad-audit and pad-repair")
        return p
    return base


def _fixes(m) -> list[mf.Fix]:
    return [mf.Fix.from_dict(x) for x in (m.get("video.manual_fixes") or [])]


def _dvd_ok(m) -> bool:
    return bool(m.get("mode.dvd_reference_available") is True and m.get("video.dvd_alignment") and m.artifact("dvd"))


def prepare(ctx, base: Path, fixes: list[mf.Fix], work: Path, *, w: int, h: int, head: int, pad: int,
            dvd: bool) -> tuple[str | None, dict[int, int], list[dict]]:
    """DVD payload (registered, colour-matched frames) and per-fix details. Raises GateError on a fix that
    cannot be built as asked (the operator chose it, so it is never silently swapped for something else)."""
    vs = ctx.tool("vapoursynth")
    images, slots, details = [], {}, []
    for f in fixes:
        ctx.check_cancel()
        det = {**f.as_dict(), "status": "planned"}
        if f.action == mf.LEAVE:
            det["status"] = "left as captured"
            details.append(det)
            continue
        tri = vs.frames(base, f.first - 1, f.last + 1, width=w, height=h)
        prev, nxt = tri[0], tri[-1]
        ncc = float(pr.desc48(prev) @ pr.desc48(nxt))
        det["neighbour_ncc"] = round(ncc, 4)
        if f.action == mf.INTERPOLATE and ncc < MIN_NEIGHBOUR_NCC:
            raise GateError(f"fix {f.first}..{f.last}: the frames either side are a shot change (similarity {ncc:.2f}); "
                            "interpolation across a cut is refused, use the DVD or a region patch")
        if f.action == mf.DVD:
            if not dvd:
                raise GateError(f"fix {f.first}..{f.last}: no accepted DVD alignment for this episode")
            got, why = dvd_borrow(ctx, work, f.first, prev, nxt, f.length, head=head, pad=pad)
            if got is None:
                raise GateError(f"fix {f.first}..{f.last}: DVD frames not found: {why}")
            for k, img in enumerate(got.images):
                slots[f.first + k] = len(images)
                images.append(img)
            det.update(dvd_gains=got.gains, dvd_offsets=got.offsets, colour_reference=got.reference,
                       post_ncc=round(got.post_ncc, 4))
        details.append(det)
    payload = None
    if images:
        ffm = ctx.tool("ffmpeg")
        seq = work / "seq"
        seq.mkdir(parents=True, exist_ok=True)
        for k, img in enumerate(images):
            pr.write_png(seq / f"{k:05d}.png", img)
        payload = str(work / "fix-payload.mkv")
        res = process.run([ffm.ffmpeg, "-y", "-hide_banner", "-nostdin", "-v", "error", "-framerate", "24000/1001",
                           "-i", str(seq / "%05d.png"), "-c:v", "ffv1", "-pix_fmt", "rgb48le", payload],
                          cancel=ctx.cancel)
        if res.returncode != 0 or not Path(payload).is_file():
            raise ArtifactError(f"fix payload clip failed: {res.describe()}")
        shutil.rmtree(seq, ignore_errors=True)
    return payload, slots, details


def measure_grain(ctx, before: Path, after: Path, fixes: list[mf.Fix], details: list[dict], *, w: int, h: int) -> None:
    """Grain of each rebuilt frame against the real neighbours' grain (whole-frame fixes only)."""
    vs = ctx.tool("vapoursynth")
    for f, det in zip(fixes, details, strict=True):
        if f.action not in (mf.INTERPOLATE, mf.DVD) or f.rect is not None:
            continue
        ref = vs.frames(before, f.first - 1, f.first - 1, width=w, height=h, pix_fmt="gray")[0]
        new = vs.frames(after, f.first, f.last, width=w, height=h, pix_fmt="gray")
        ratios = [r["ratio"] for r in (grain_retention(ref, x) for x in new) if r]
        if ratios:
            det["grain_ratio_vs_neighbour"] = round(float(np.median(ratios)), 3)
            det["grain_flag"] = bool(np.median(ratios) < GRAIN_FLAG)


@register
class FrameFixes(Stage):
    id = "frame-fixes"
    title = "Operator frame fixes (interpolate / region patch / DVD)"
    description = ("Applies the frame fixes chosen in the final review to the chain the final encode reads "
                   "(after the PAD splice, before finishing). With none chosen it records an unchanged chain.")
    requires = (ArtifactExists("encode_script"), ValueSet("video.frames"), ValueSet("video.head_trim"),
                ValueSet("audio.pad_frames"))
    writes = ()
    writes_artifacts = ("manualfix_script", "manualfix_report")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        base = _pre_fix_chain(ctx, m)
        fixes = _fixes(m)
        problems = mf.validate(fixes, frames=int(m.require("video.frames")))
        if problems:
            raise GateError("video.manual_fixes: " + "; ".join(problems))
        if any(f.action == mf.DVD for f in fixes) and not _dvd_ok(m):
            raise GateError("DVD fixes need the DVD route (an accepted video.dvd_alignment and the dvd artifact)")
        run = utc_stamp()[:15].lower()
        slug = ctx.episode.slug
        files = {"script": ctx.root("scripts") / f"ivtc-manualfix-{slug}-{run}.vpy",
                 "report": ctx.root("reports") / f"frame-fixes-{run}.json",
                 "work": ctx.root("probe") / f"frame-fixes-{slug}-{run}"}
        return StepPlan(self.id, params, inputs=[base], outputs=[files["script"], files["report"]],
                        summary=f"{len(fixes)} operator fix(es)",
                        data={**{k: str(v) for k, v in files.items()}, "base": str(base),
                              "fixes": [f.as_dict() for f in fixes], "frames": int(m.require("video.frames")),
                              "head": int(m.require("video.head_trim")), "pad": int(m.require("audio.pad_frames")),
                              "dvd": _dvd_ok(m)})

    def run(self, ctx, plan):
        d = plan.data
        fixes = [mf.Fix.from_dict(x) for x in d["fixes"]]
        base = Path(d["base"])
        details: list[dict] = []
        payload, slots = None, {}
        vs = ctx.tool("vapoursynth")
        if any(f.action != mf.LEAVE for f in fixes):
            info = vs.info(base)
            w, h = int(info["width"]), int(info["height"])
            problems = mf.validate(fixes, frames=d["frames"], width=w, height=h)
            if problems:
                raise GateError("; ".join(problems))
            payload, slots, details = prepare(ctx, base, fixes, Path(d["work"]), w=w, h=h, head=d["head"],
                                              pad=d["pad"], dvd=d["dvd"])
        else:
            details = [{**f.as_dict(), "status": "left as captured"} for f in fixes]
        text = mf.manualfix_script(base.read_text(encoding="utf-8"), fixes=fixes, expected_frames=d["frames"],
                                   payload=payload, slots=slots)
        atomic_write_bytes(d["script"], text.encode())
        if any(f.action != mf.LEAVE for f in fixes):
            info2 = vs.info(Path(d["script"]))
            if info2["frames"] != d["frames"]:
                raise ArtifactError(f"fixed chain reports {info2['frames']} frames; expected {d['frames']}")
            ctx.report(0.9, "grain of rebuilt frames")
            measure_grain(ctx, base, Path(d["script"]), fixes, details, w=w, h=h)
            for det in details:
                if det["status"] == "planned":
                    det["status"] = "applied"
        atomic_write_json(d["report"], {"schema": "fstk-frame-fixes-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "base": d["base"], "script": d["script"],
                                        "payload": payload, "fixes": details})
        out = StepOutput(report=Path(d["report"]))
        out.notes += [f"fix {x['first']}..{x['last']}: grain {x['grain_ratio_vs_neighbour']} of the neighbour's"
                      for x in details if x.get("grain_flag")]
        out.artifacts["manualfix_script"] = {"root": "scripts", "name": Path(d["script"]).name}
        out.artifacts["manualfix_report"] = {"root": "reports", "name": Path(d["report"]).name}
        return out


def _strip(images: list, labels: list[str], out: Path, *, thumb_w: int = 360) -> None:
    """A labelled row of frames (uint16 or uint8 RGB) as one PNG."""
    from PIL import Image, ImageDraw

    tiles = []
    for img, lab in zip(images, labels, strict=True):
        a = np.asarray(img)
        if a.dtype == np.uint16:
            a = (a >> 8).astype(np.uint8)
        im = Image.fromarray(a)
        im = im.resize((thumb_w, max(1, round(im.height * thumb_w / im.width))), Image.LANCZOS)
        tiles.append((im, lab))
    th = max(t.height for t, _ in tiles)
    sheet = Image.new("RGB", (thumb_w * len(tiles), th + 24), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for i, (im, lab) in enumerate(tiles):
        sheet.paste(im, (i * thumb_w, 0))
        draw.text((i * thumb_w + 4, th + 4), lab[:48], fill=(230, 230, 230))
    sheet.save(out, "PNG")


def _clip_script(chain: Path, first: int, last: int) -> str:
    return "\n".join([
        "import vapoursynth as vs", "core = vs.core", "ns = {}",
        f"exec(compile(open({str(chain)!r}, encoding='utf-8').read(), {str(chain)!r}, 'exec'), ns)",
        "vs.clear_outputs()",
        f"out = core.std.Trim(ns['out'], {first}, {last})",
        "out = core.resize.Bicubic(out, format=vs.YUV420P8, dither_type='error_diffusion')",
        "out.set_output()"]) + "\n"


@register
class FixPreview(Stage):
    id = "fix-preview"
    title = "Preview one operator frame fix"
    description = ("Shows one proposed fix before it is chosen: the frames around it as captured and as fixed "
                   "(for the DVD, the conformed DVD frames beside the LD ones) and, for fixes up to 2 s, short clips "
                   "of the shot as captured and with the fix in place. Review-only output.")
    requires = (ArtifactExists("encode_script"), ValueSet("video.frames"), ValueSet("video.head_trim"),
                ValueSet("audio.pad_frames"))
    writes = ()
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        try:
            fix = mf.Fix.from_dict(params["fix"])
        except (KeyError, TypeError, ValueError):
            raise GateError("fix-preview needs params.fix {first, last, action, [rect], [src], note}") from None
        problems = mf.validate([fix], frames=int(m.require("video.frames")))
        if problems:
            raise GateError("; ".join(problems))
        base = _pre_fix_chain(ctx, m)
        run = utc_stamp()[:15].lower()
        work = ctx.root("probe") / f"fix-preview-{ctx.episode.slug}-{run}"
        report = ctx.root("reports") / f"fix-preview-{run}.json"
        return StepPlan(self.id, params, inputs=[base], outputs=[report], summary=f"preview {fix.action} {fix.first}..{fix.last}",
                        data={"fix": fix.as_dict(), "base": str(base), "work": str(work), "report": str(report),
                              "frames": int(m.require("video.frames")), "head": int(m.require("video.head_trim")),
                              "pad": int(m.require("audio.pad_frames")), "dvd": _dvd_ok(m)})

    def run(self, ctx, plan):
        d = plan.data
        fix = mf.Fix.from_dict(d["fix"])
        base, work = Path(d["base"]), Path(d["work"])
        work.mkdir(parents=True, exist_ok=True)
        vs = ctx.tool("vapoursynth")
        info = vs.info(base)
        w, h = int(info["width"]), int(info["height"])
        payload, slots, details = prepare(ctx, base, [fix], work, w=w, h=h, head=d["head"], pad=d["pad"], dvd=d["dvd"])
        fixed = work / "fixed.vpy"
        atomic_write_bytes(fixed, mf.manualfix_script(base.read_text(encoding="utf-8"), fixes=[fix],
                                                      expected_frames=d["frames"], payload=payload, slots=slots).encode())
        lo, hi = max(0, fix.first - 2), min(d["frames"] - 1, fix.last + 2)
        if hi - lo + 1 > 9:                         # wide range: both ends and the middle
            picks = sorted({lo, fix.first, (fix.first + fix.last) // 2, fix.last, hi})
        else:
            picks = list(range(lo, hi + 1))
        before = [vs.frames(base, n, n, width=w, height=h)[0] for n in picks]
        after = [vs.frames(fixed, n, n, width=w, height=h)[0] for n in picks]
        tag = {True: "fixed", False: ""}
        strips = {"captured": str(work / "strip-captured.png"), "fixed": str(work / "strip-fixed.png")}
        _strip(before, [f"{n} captured" for n in picks], Path(strips["captured"]))
        _strip(after, [f"{n} {tag[fix.first <= n <= fix.last]}".strip() for n in picks], Path(strips["fixed"]))
        measure_grain(ctx, base, fixed, [fix], details, w=w, h=h)
        clips = {}
        if fix.length <= mf.PREVIEW_MAX_FRAMES and fix.action != mf.LEAVE:
            a, b = max(0, fix.first - 48), min(d["frames"] - 1, fix.last + 48)
            for name, chain in (("captured", base), ("fixed", fixed)):
                s = work / f"clip-{name}.vpy"
                atomic_write_bytes(s, _clip_script(chain, a, b).encode())
                clip = work / f"clip-{name}.mkv"
                res = vs.encode(s, ["-c:v", "libx264", "-preset", "medium", "-crf", "12", "-r", "24000/1001", str(clip)],
                                name=f"fix-preview-{name}", cancel=ctx.cancel)
                if res.returncode != 0 or not clip.is_file():
                    raise ArtifactError(f"preview clip ({name}) failed: {res.describe()}")
                clips[name] = str(clip)
            clips["start_frame"] = a
        atomic_write_json(d["report"], {"schema": "fstk-fix-preview-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "fix": fix.as_dict(), "detail": details[0],
                                        "strips": strips, "clips": clips, "frames_shown": picks})
        out = StepOutput(report=Path(d["report"]))
        return out
