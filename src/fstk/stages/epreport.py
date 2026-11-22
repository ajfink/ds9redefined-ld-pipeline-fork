"""``episode-report``: the per-episode quality report, saved next to the deliverable.

Collects the video-quality, audio-quality and QC results, ranks the worst moments, and renders a contact sheet
of them from the delivered file. It measures nothing and changes nothing; it exists so the final review (and the
owner, later) can see the whole quality picture in one place. Missing inputs are reported, never invented.
"""
from __future__ import annotations

import json
from pathlib import Path

from fstk.core.errors import GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.quality import contact, geometry, report

from .base import Stage, StepOutput, StepPlan, ValueSet, register
from .qc import _deliverable_key


def _load(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fixes(path: Path | None) -> list[dict]:
    if path is None or not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) >= 2 and cols[0].isdigit():
            out.append({"frame": int(cols[0])})
    return out


@register
class EpisodeReport(Stage):
    id = "episode-report"
    title = "Per-episode quality report and contact sheet"
    description = ("Gathers the quality and QC results into one report next to the deliverable, ranks the worst "
                   "moments and renders a contact sheet of them. Read-only with respect to the media.")
    requires = (ValueSet("video.frames"), ValueSet("video.duration_s"))
    writes = ("quality.verdict",)
    writes_artifacts = ("episode_quality_report", "episode_contact_sheet")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        deliverable = ctx.artifact_path(_deliverable_key(m), m)
        if not deliverable.is_file():
            raise GateError(f"deliverable missing: {deliverable}")
        run = utc_stamp()[:15].lower()
        reports = ctx.root("reports")
        qcs = sorted(reports.glob("qc-*.json"))
        outs = {"json": reports / f"episode-quality-{run}.json", "md": reports / f"episode-quality-{run}.md",
                "png": reports / f"episode-contact-{run}.png"}
        beside = deliverable.with_name(deliverable.stem + ".quality.md")
        return StepPlan(self.id, params, inputs=[deliverable], outputs=[outs["json"], outs["md"], beside],
                        summary=f"quality report for {deliverable.name}",
                        data={"deliverable": str(deliverable), "beside": str(beside), "qc": str(qcs[-1]) if qcs else None,
                              **{k: str(v) for k, v in outs.items()}})

    def run(self, ctx, plan):
        d, m = plan.data, ctx.manifest

        def art(key):
            return ctx.artifact_path(key, m) if m.artifact(key) else None
        video = _load(art("quality_video_report"))
        audio = _load(art("audio_quality_report"))
        qc = _load(Path(d["qc"])) if d["qc"] else None
        frames, duration = int(m.require("video.frames")), float(m.require("video.duration_s"))
        try:
            stream = ctx.tool("ffmpeg").probe(Path(d["deliverable"])).only("video").raw
            tags = geometry.check_colour_tags(stream, expect_dar=m.get("quality.dar"), expect_sar=m.get("quality.sar"))
        except Exception as exc:                      # a probe failure is reported, never hidden or fatal here
            tags = [{"check": "ffprobe", "value": str(exc), "expected": "readable", "status": "FAIL"}]
        if m.artifact("stack_master"):
            from .stack import LOSSLESS_MASTER_CODECS
            try:
                codec = ctx.tool("ffmpeg").probe(ctx.artifact_path("stack_master", m)).only("video").raw.get("codec_name")
            except Exception as exc:                  # reported, not fatal: the master may have been archived
                codec = f"unreadable ({exc})"
            tags.append({"area": "Source chain", "check": "stack master is lossless (single lossy generation)",
                         "value": codec, "expected": "/".join(LOSSLESS_MASTER_CODECS),
                         "status": "PASS" if codec in LOSSLESS_MASTER_CODECS else "FLAG"})
        rep = report.build(str(ctx.episode), m.title or "", video=video, audio=audio, qc=qc,
                           fixes=_fixes(art("quality_fixes")), duration_s=duration, frames=frames, tags=tags)
        rep["delivered_tags"] = tags
        ctx.report(0.3, "contact sheet of the worst moments")
        n = contact.contact_sheet(ctx.tool("ffmpeg"), Path(d["deliverable"]), rep["moments"], Path(d["png"]))
        if n:
            rep["contact_sheet"] = d["png"]
        rep.update(created_utc=utc_now_iso(), deliverable=d["deliverable"], contact_frames=n)
        atomic_write_json(d["json"], rep)
        text = report.render_markdown(rep).encode("utf-8")
        atomic_write_bytes(Path(d["md"]), text)
        atomic_write_bytes(Path(d["beside"]), text)
        out = StepOutput(report=Path(d["json"]))
        out.data["verdict"] = rep["verdict"]
        out.measure("quality.verdict", rep["verdict"])
        out.artifacts["episode_quality_report"] = {"root": "reports", "name": Path(d["json"]).name}
        if n:
            out.artifacts["episode_contact_sheet"] = {"root": "reports", "name": Path(d["png"]).name}
        return out
