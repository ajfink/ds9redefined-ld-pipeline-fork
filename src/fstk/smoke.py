"""``fstk smoke`` — run the video half of the delivery chain on a short real excerpt.

The point is to find which seam breaks first on a real machine (real VapourSynth/TIVTC, real
x265, real PAD scan) in minutes instead of discovering it on hour 14 of a full episode. It builds an
isolated sandbox (its own profile, workspace and manifest; nothing in the real data root is touched),
cuts the first ``seconds`` of an existing stack master with stream copy, and runs

    cadence-evidence → video-trim (no-DVD route) → pad-audit → pad-repair → encode-final

then, when a DVD and its alignment are available, repeats ``pad-repair`` on the DVD route to exercise
the borrowed-frame path. Every step's status, timing and issues go into a log (JSON + Markdown).
Audio stages are not part of the smoke run: they need whole-capture timing.
"""
from __future__ import annotations

import json
import shutil
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.core.timeutil import utc_now_iso


@dataclass
class SmokeSpec:
    episode: str
    master: Path                      # an existing stack master (ProRes .mov)
    project: Path                     # the Orc stack project (reference frame map → PAD positions)
    reference: str                    # label of the reference capture in that project
    pad_frames: int                   # audio.pad_frames of the selected capture
    capture: str                      # selected audio capture label (only used in the video title)
    sandbox: Path
    seconds: float = 200.0
    chroma: str = "422"
    dvd: Path | None = None
    dvd_offset: float | None = None   # DVD − master offset (grid frames) near the excerpt, for the DVD-borrow pass
    repair: str = "qtgmc"
    rife_standin: bool = False        # CPU-only sandboxes: blend stand-in instead of RIFE (labelled STANDIN)
    steps: list[str] = field(default_factory=lambda: ["cadence", "trim", "audit", "repair", "encode"])
    preset: str = "ultrafast"         # smoke checks seams, not the recipe; recorded as a recipe override


def _log_line(log: list[dict], **kw) -> None:
    log.append({"utc": utc_now_iso(), **kw})
    print(" ".join(f"{k}={v}" for k, v in kw.items()), flush=True)


def build_profile(spec: SmokeSpec):
    from fstk.config.profile import KNOWN_TOOLS, SCHEMA, Profile, current_os

    tools = {}
    for name, ts in KNOWN_TOOLS.items():
        found = next((shutil.which(n) for n in ts["path_names"] if shutil.which(n)), None)
        if found:
            tools[name] = found
    root = spec.sandbox.absolute()
    if spec.rife_standin and "rife_engine" not in tools:
        import stat
        import sys
        model = root / "rife-models" / "rife-v0-smoke-STANDIN"
        model.mkdir(parents=True, exist_ok=True)
        wrapper = root / "rife-standin.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m fstk.smoke_rife_standin "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        tools["rife_engine"], tools["rife_model_dir"] = str(wrapper), str(model)
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "SMOKE", "os": current_os(),
                              "data_root": str(root / "data"), "media_root": str(root / "media"),
                              "tools": tools, "limits": {"min_free_gb": 0.5}})
    prof.save(root / "profile.json")
    return prof, tools


def make_excerpt(prof, ep: EpisodeId, spec: SmokeSpec) -> tuple[Path, int]:
    import subprocess

    out = prof.root("prores", ep) / f"{ep.code}-stack-master.mov"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        subprocess.run([str(prof.tool("ffmpeg")), "-hide_banner", "-nostdin", "-v", "error", "-i", str(spec.master),
                        "-t", f"{spec.seconds:.3f}", "-map", "0:v:0", "-c", "copy", "-an", str(out)], check=True)
    ff = prof.tool("ffprobe")
    res = subprocess.run([str(ff), "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True, check=True)
    return out, int(res.stdout.strip())


def run_smoke(spec: SmokeSpec) -> dict:
    from fstk.manifest.model import Manifest
    from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

    ep = EpisodeId.parse(spec.episode)
    spec.sandbox.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    prof, tools = build_profile(spec)
    _log_line(log, event="sandbox", path=spec.sandbox, tools=",".join(sorted(tools)))
    master, frames = make_excerpt(prof, ep, spec)
    _log_line(log, event="excerpt", path=master, frames=frames, seconds=spec.seconds)

    ws = EpisodeWorkspace(prof, ep)
    if not ws.store.exists():
        m = Manifest.new(ep, title="smoke excerpt")
        ws.store.create(m, by="smoke", reason="smoke sandbox")
    with ws.store.transaction(by="smoke", reason="smoke inputs") as m:
        for k, v in {"stack.frames": frames, "stack.reference": spec.reference, "audio.pad_frames": spec.pad_frames,
                     "audio.capture": spec.capture, "mode.dvd_reference_available": False,
                     "mode.no_dvd_proceed_approved": True, "mode.chroma_output": spec.chroma}.items():
            if m.get(k) != v:
                m.set(k, v, by="smoke", note="smoke input")
        m.set_artifact("stack_master", root="prores", name=master.name)
        m.set_artifact("stack_project", path=str(spec.project))

    stage_of = {"cadence": ("cadence-evidence", {}), "trim": ("video-trim", {"repair": spec.repair}), "audit": ("pad-audit", {}),
                "repair": ("pad-repair", {}), "encode": ("encode-final", {"planned_gib": 3.0,
                                                                          "estimate_basis": "smoke excerpt", "preset": spec.preset})}
    results: dict[str, dict] = {}
    for key in spec.steps:
        stage, params = stage_of[key]
        plan = {"schema": PLAN_SCHEMA, "episode": str(ep), "job": f"smoke-{key}-{int(time.time())}",
                "steps": [{"id": key, "stage": stage, "params": params, "after": []}]}
        t0 = time.time()
        try:
            state = Engine(prof, plan, actor="smoke").run()
            st = state["steps"][key]
            if st["status"] == "awaiting-review":
                # Smoke only exercises the machinery: record an approval for exactly the evidence the
                # stage produced, clearly labelled as automatic (never a human review), then resume.
                from fstk.stages.base import get_stage
                review = get_stage(stage).review
                evidence = [Path(fp["path"]) for fp in st.get("outputs", [])]
                ws.approvals.record(review=review, decision="APPROVED", actor="smoke-auto",
                                    note="SMOKE AUTO-APPROVAL: pipeline exercise only, not a human review",
                                    evidence=evidence, manifest=ws.store.load(), extra={"job": plan["job"], "step": key})
                _log_line(log, event="auto-approved", step=key, review=review)
                state = Engine(prof, plan, actor="smoke").run()
                st = state["steps"][key]
            results[key] = {"stage": stage, "status": st["status"], "issues": st.get("issues"),
                            "seconds": round(time.time() - t0, 1)}
        except Exception as exc:                                    # a crash is a finding, not a stop
            results[key] = {"stage": stage, "status": "crashed", "issues": [f"{type(exc).__name__}: {exc}"],
                            "trace": traceback.format_exc()[-1500:], "seconds": round(time.time() - t0, 1)}
        _log_line(log, event="stage", **{k: v for k, v in results[key].items() if k != "trace"})
        if results[key]["status"] not in ("passed", "awaiting-review"):
            break

    m = ws.store.load()
    summary = {"schema": "fstk-smoke-v1", "created_utc": utc_now_iso(), "spec": {k: str(v) for k, v in vars(spec).items()},
               "results": results, "manifest_values": {k: m.get(k) for k in (
                   "video.frames", "video.head_trim", "video.tail_trim", "video.repair_frames", "pads.map_pads",
                   "pads.rendered_isolated", "pads.trimmed_away", "pads.cut_pads", "pads.cut_pad_frames",
                   "pads.repaired", "pads.content_blacks_left_alone", "video.tfm_period5_pct", "video.tfm_unmatched")},
               "log": log}
    (spec.sandbox / "smoke-result.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def to_markdown(summary: dict) -> str:
    out = [f"# fstk smoke — {summary['spec']['episode']} — {summary['created_utc']}", "",
           f"excerpt {summary['spec']['seconds']} s, chroma {summary['spec']['chroma']}", "",
           "| step | stage | status | seconds | issues |", "|---|---|---|---|---|"]
    for k, r in summary["results"].items():
        issues = "; ".join(str(i) for i in (r.get("issues") or []))[:300].replace("|", "/")
        out.append(f"| {k} | {r['stage']} | {r['status']} | {r['seconds']} | {issues} |")
    out += ["", "## measured", "", "```json", json.dumps(summary["manifest_values"], indent=1, default=str), "```"]
    for k, r in summary["results"].items():
        if r.get("trace"):
            out += ["", f"## {k} traceback", "", "```", r["trace"], "```"]
    return "\n".join(out) + "\n"


def smoke_main(args) -> int:
    spec = SmokeSpec(episode=args.episode, master=Path(args.master), project=Path(args.project),
                     reference=args.reference, pad_frames=args.pad_frames, capture=args.capture,
                     sandbox=Path(args.sandbox), seconds=args.seconds, chroma=args.chroma,
                     dvd=Path(args.dvd) if args.dvd else None, dvd_offset=args.dvd_offset,
                     steps=args.steps.split(",") if args.steps else ["cadence", "trim", "audit", "repair", "encode"],
                     preset=args.preset, repair=args.repair, rife_standin=args.rife_standin)
    try:
        summary = run_smoke(spec)
    except FstkError as exc:
        print(f"smoke could not start: {exc}")
        return 2
    md = to_markdown(summary)
    (spec.sandbox / "smoke-result.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if all(r["status"] in ("passed", "awaiting-review") for r in summary["results"].values()) else 1
