"""``stretch-preview``: side-by-side previews of each video-origin stretch, nearest vs interpolated.

For every converted stretch in the hybrid plan this renders one short review clip: on the left the default
treatment (nearest real 59.94p/29.97p frame on the 23.976 grid), on the right motion-compensated interpolation,
each labelled, with a second of picture either side. The clips are for judging only (8-bit 4:2:0 x264, never a
delivery source); the choice is recorded as ``video.hybrid_choices`` and applied by a fresh ``video-trim`` and
encode from the master. Episodes without video-origin stretches pass with nothing rendered.
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import ArtifactError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media import hybrid
from fstk.media.cadence_map import ivtc_vpy_script, parse_ranges
from fstk.media.encode import DEFAULT_CHROMA

from .base import ArtifactExists, Stage, StepOutput, StepPlan, ValueSet, register

HANDLE_FRAMES = 24          # one second of picture either side
MAX_PREVIEW_FRAMES = 24 * 20  # 20 s per stretch is enough to judge


def preview_wrapper(nearest: Path, interp: Path, first: int, last: int) -> str:
    """VapourSynth script stacking the two treatments of output frames first..last, labelled."""
    return "\n".join([
        "import vapoursynth as vs",
        "core = vs.core",
        "def _load(p):",
        "    ns = {}",
        "    exec(compile(open(p, encoding='utf-8').read(), p, 'exec'), ns)",
        "    vs.clear_outputs()",
        "    return ns['out']",
        f"a = core.std.Trim(_load({str(nearest)!r}), {first}, {last})",
        f"b = core.std.Trim(_load({str(interp)!r}), {first}, {last})",
        "a = core.text.Text(a, 'NEAREST REAL FRAMES (default)', alignment=7)",
        "b = core.text.Text(b, 'INTERPOLATED', alignment=7)",
        "out = core.std.StackHorizontal([a, b])",
        "out = core.resize.Bicubic(out, format=vs.YUV420P8, dither_type='error_diffusion')",
        "out.set_output()",
    ]) + "\n"


def output_window(seg: hybrid.Segment, head_trim: int, frames: int) -> tuple[int, int] | None:
    """Trimmed-output frame window [first, last] (inclusive) to preview for a stretch, or None if trimmed away."""
    a, b = seg.output_frames
    first = max(0, a - head_trim - HANDLE_FRAMES)
    last = min(frames - 1, b - 1 - head_trim + HANDLE_FRAMES, first + MAX_PREVIEW_FRAMES - 1)
    return (first, last) if last > first else None


@register
class StretchPreview(Stage):
    id = "stretch-preview"
    title = "Preview video-origin stretches (nearest vs interpolated)"
    description = ("Renders one side-by-side clip per video-origin stretch for the final review: nearest real frames "
                   "(default) against motion-compensated interpolation. Review-only clips; nothing delivered.")
    requires = (ArtifactExists("hybrid_plan"), ArtifactExists("stack_master"), ArtifactExists("di_map"),
                ValueSet("audio.pad_frames"), ValueSet("video.head_trim"), ValueSet("video.tail_trim"),
                ValueSet("video.frames"))
    writes = ()
    writes_artifacts = ("stretch_previews",)
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        doc = read_json(ctx.artifact_path("hybrid_plan", m))
        segs = [s for s in (hybrid.Segment.from_dict(x) for x in doc["segments"]) if s.active]
        run = utc_stamp()[:15].lower()
        folder = ctx.root("probe") / f"stretch-preview-{ctx.episode.slug}-{run}"
        report = ctx.root("reports") / f"stretch-preview-{run}.json"
        trims = [r for r in ctx.root("reports").glob("video-trim-*.json")]
        repair = read_json(sorted(trims)[-1])["repair"] if trims else ("qtgmc" if segs else "none")
        return StepPlan(self.id, params, inputs=[ctx.artifact_path("hybrid_plan", m)], outputs=[report],
                        summary=f"{len(segs)} stretch preview(s)",
                        data={"folder": str(folder), "report": str(report), "repair": repair,
                              "segments": [s.as_dict() for s in segs],
                              "master": str(ctx.artifact_path("stack_master", m)),
                              "ranges": parse_ranges(ctx.artifact_path("di_map", m).read_text(encoding="utf-8")),
                              "pad": int(m.require("audio.pad_frames")), "head": int(m.require("video.head_trim")),
                              "tail": int(m.require("video.tail_trim")), "frames": int(m.require("video.frames")),
                              "chroma": m.get("mode.chroma_output") or DEFAULT_CHROMA})

    def run(self, ctx, plan):
        d = plan.data
        segs = [hybrid.Segment.from_dict(x) for x in d["segments"]]
        folder = Path(d["folder"])
        previews = []
        if segs:
            folder.mkdir(parents=True, exist_ok=True)
            scripts = {}
            for t in hybrid.TREATMENTS:
                forced = [hybrid.Segment(s.kind, s.first_cycle, s.end_cycle, s.plate, t) for s in segs]
                text = ivtc_vpy_script(d["master"], d["pad"], d["head"], d["tail"], [tuple(r) for r in d["ranges"]],
                                       d["repair"], expected_frames=d["frames"], chroma=d["chroma"],
                                       video_cycles=hybrid.cycle_treatments(forced))
                scripts[t] = folder / f"ivtc-{t}.vpy"
                atomic_write_bytes(scripts[t], text.encode())
            vs = ctx.tool("vapoursynth")
            for i, s in enumerate(segs):
                ctx.check_cancel()
                win = output_window(s, d["head"], d["frames"])
                if win is None:
                    continue
                ctx.report(i / len(segs), f"stretch at {win[0] * 1001 / 24000:.1f} s")
                wrapper = folder / f"stretch-{s.first_cycle}.vpy"
                atomic_write_bytes(wrapper, preview_wrapper(scripts[hybrid.NEAREST], scripts[hybrid.INTERPOLATE],
                                                            *win).encode())
                clip = folder / f"stretch-{s.first_cycle}.mkv"
                res = vs.encode(wrapper, ["-c:v", "libx264", "-preset", "medium", "-crf", "14", "-r", "24000/1001",
                                          str(clip)], name=f"stretch-preview-{s.first_cycle}", cancel=ctx.cancel)
                if res.returncode != 0 or not clip.is_file():
                    raise ArtifactError(f"preview of the stretch at cycle {s.first_cycle} failed ({res.describe()})")
                previews.append({"first_cycle": s.first_cycle, "kind": s.kind, "seconds": round(s.seconds, 2),
                                 "start_s": round(win[0] * 1001 / 24000, 3), "end_s": round((win[1] + 1) * 1001 / 24000, 3),
                                 "clip": str(clip)})
        atomic_write_json(d["report"], {"schema": "fstk-stretch-preview-v1", "created_utc": utc_now_iso(),
                                        "episode": str(ctx.episode), "previews": previews,
                                        "note": "review clips only (8-bit x264); the delivery is encoded from the master"})
        out = StepOutput(report=Path(d["report"]))
        out.artifacts["stretch_previews"] = {"root": "reports", "name": Path(d["report"]).name}
        return out
