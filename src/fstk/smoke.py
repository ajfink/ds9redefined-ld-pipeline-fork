"""``fstk smoke`` — run the video half of the delivery chain on a short real excerpt.

The point is to find which seam breaks first on a real machine (real VapourSynth/TIVTC, real
x265) in minutes instead of discovering it on hour 14 of a full episode. It builds an
isolated sandbox (its own profile, workspace and manifest; nothing in the real data root is touched),
cuts the first ``seconds`` of an existing stack master with stream copy, and runs

    cadence-evidence → video-trim (no-DVD route) → encode-final

Every step's status, timing and issues go into a log (JSON + Markdown).
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
from fstk.tools import process


@dataclass
class SmokeSpec:
    episode: str
    master: Path                      # an existing stack master (FFV1 .mkv; a legacy ProRes .mov is accepted with a warning)
    project: Path                     # the Orc stack project
    reference: str                    # label of the reference capture in that project
    pad_frames: int                   # audio.pad_frames of the selected capture
    capture: str                      # selected audio capture label (only used in the video title)
    sandbox: Path
    seconds: float = 200.0
    chroma: str = "422"
    repair: str = "qtgmc"
    steps: list[str] = field(default_factory=lambda: ["cadence", "trim", "encode"])
    preset: str = "ultrafast"         # smoke checks seams, not the recipe; recorded as a recipe override
    profile: Path | None = None       # the machine profile whose tool paths are reused (default location if None)


def _log_line(log: list[dict], **kw) -> None:
    log.append({"utc": utc_now_iso(), **kw})
    print(" ".join(f"{k}={v}" for k, v in kw.items()), flush=True)


def _default_sandbox() -> Path:
    from fstk.config.profile import default_app_home

    return default_app_home() / "smoke"


def build_profile(spec: SmokeSpec):
    from fstk.config.profile import KNOWN_TOOLS, SCHEMA, Profile, current_os

    tools = {}
    try:                     # the machine's own profile first (Orc is usually not on PATH) …
        from fstk.config.profile import default_profile_path

        real = Profile.load(spec.profile) if getattr(spec, "profile", None) else Profile.load(default_profile_path())
        tools.update({k: v for k, v in real.tools.items() if v and Path(v).exists()})
    except Exception:        # no profile yet: PATH only
        pass
    for name, ts in KNOWN_TOOLS.items():   # … then PATH for anything still missing
        if name in tools:
            continue
        found = next((shutil.which(n) for n in ts["path_names"] if shutil.which(n)), None)
        if found:
            tools[name] = found
    root = spec.sandbox.absolute()
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "SMOKE", "os": current_os(),
                              "data_root": str(root / "data"), "media_root": str(root / "media"),
                              "tools": tools, "limits": {"min_free_gb": 0.5}})
    prof.save(root / "profile.json")
    return prof, tools


def make_excerpt(prof, ep: EpisodeId, spec: SmokeSpec) -> tuple[Path, int]:
    import subprocess

    out = prof.root("prores", ep) / f"{ep.code}-stack-master.mkv"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        subprocess.run([str(prof.tool("ffmpeg")), "-hide_banner", "-nostdin", "-v", "error", "-i", str(spec.master),
                        "-t", f"{spec.seconds:.3f}", "-map", "0:v:0", "-c", "copy", "-an", str(out)], check=True,
                       **process.hidden())
    ff = prof.tool("ffprobe")
    res = subprocess.run([str(ff), "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True, check=True, **process.hidden())
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

    stage_of = {"cadence": ("cadence-evidence", {}), "trim": ("video-trim", {"repair": spec.repair}), "encode": ("encode-final", {"planned_gib": 3.0,
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
                   "video.frames", "video.head_trim", "video.tail_trim", "video.repair_frames",
                   "video.tfm_period5_pct", "video.tfm_unmatched")},
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
                     sandbox=Path(args.sandbox) if args.sandbox else _default_sandbox(), seconds=args.seconds,
                     chroma=args.chroma,
                     steps=args.steps.split(",") if args.steps else ["cadence", "trim", "encode"],
                     preset=args.preset, repair=args.repair,
                     profile=Path(args.profile) if getattr(args, "profile", None) else None)
    try:
        summary = run_smoke(spec)
    except FstkError as exc:
        print(f"smoke could not start: {exc}")
        return 2
    md = to_markdown(summary)
    (spec.sandbox / "smoke-result.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if all(r["status"] in ("passed", "awaiting-review") for r in summary["results"].values()) else 1
