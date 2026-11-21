"""DVD reference stages."""
from __future__ import annotations

import json

from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.hashing import sha256_file
from fstk.core.timeutil import utc_now_iso

from .base import ArtifactExists, Stage, StepOutput, StepPlan, register


@register
class DvdProbe(Stage):
    """Inventory a DVD reference: streams, packet-order (seekability), decoded frame count, hash.

    Replaces the probe half of v38 ``dvd_prep.py``. The packet scan decides whether later
    stages may ever seek this file (non-monotonic PTS → sequential decode only). Writes a
    report; records no manifest values (the DVD's role is an operator choice).
    """

    id = "dvd-probe"
    title = "Probe DVD reference"
    description = "Streams, PTS monotonicity, decoded frame count and SHA-256 of the DVD rip."
    requires = (ArtifactExists("dvd"),)
    resource = "prep"

    def plan(self, ctx, params):
        dvd = ctx.artifact_path("dvd")
        report = ctx.root("reports") / "dvd-probe.json"
        return StepPlan(self.id, params, inputs=[dvd], outputs=[report],
                        summary=f"probe {dvd.name}", data={"dvd": str(dvd), "report": str(report)})

    def run(self, ctx, plan):
        from pathlib import Path

        ff = ctx.tool("ffmpeg")
        dvd = Path(plan.data["dvd"])
        ctx.report(0.05, "probing streams")
        info = ff.probe(dvd)
        ctx.check_cancel()
        ctx.report(0.2, "scanning video packet order")
        scan = ff.scan_packets(dvd, "v:0")
        ctx.check_cancel()
        ctx.report(0.4, "decoding to count frames")
        frames = ff.count_frames(dvd, "v:0")
        ctx.report(0.8, "hashing")
        digest = sha256_file(dvd)
        streams = []
        for s in info.streams:
            row = {"index": s.index, "type": s.codec_type, "codec": s.codec_name}
            for k in ("channels", "channel_layout", "sample_rate", "width", "height", "r_frame_rate",
                      "avg_frame_rate", "field_order", "pix_fmt", "duration"):
                if k in s.raw:
                    row[k] = s.raw[k]
            lang = (s.raw.get("tags") or {}).get("language")
            if lang:
                row["language"] = lang
            streams.append(row)
        report = {
            "schema": "fstk-dvd-probe-v1", "created_utc": utc_now_iso(), "episode": str(ctx.episode),
            "dvd": {"path": str(dvd), "bytes": dvd.stat().st_size, "sha256": digest},
            "container_duration_s": info.duration_s, "streams": streams,
            "video_packets": {"count": scan.packets, "monotonic": scan.monotonic,
                              "violations": scan.violations, "first_violation_index": scan.first_violation_index},
            "seek_policy": "seek-allowed" if scan.monotonic else "sequential-decode-only",
            "decoded_video_frames": frames,
        }
        atomic_write_json(plan.data["report"], report)
        out = StepOutput(report=Path(plan.data["report"]))
        out.data = {"frames": frames, "monotonic": scan.monotonic}
        return out

    def verify(self, ctx, plan, output):
        issues = []
        try:
            report = read_json(plan.data["report"])
        except Exception as exc:  # unreadable report is a failure, not a pass
            return [f"report unreadable: {exc}"]
        if report.get("decoded_video_frames", 0) <= 0:
            issues.append("decoded video frame count is not positive")
        if not any(s["type"] == "video" for s in report.get("streams", [])):
            issues.append("no video stream")
        if report["dvd"]["sha256"] != sha256_file(plan.data["dvd"]):
            issues.append("DVD changed while it was probed")
        return issues


__all__ = ["DvdProbe", "json"]
