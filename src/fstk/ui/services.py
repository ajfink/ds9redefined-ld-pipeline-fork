"""UI-agnostic service API used by the desktop app (and usable by any other front end).

The UI never touches manifests, approvals, or tools directly; it calls these functions,
which go through the same store/engine/approval code paths as the CLI. Everything here is
safe to call from a worker thread except where noted.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fstk.config.profile import Profile
from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.manifest.registry import FIELDS, field_def
from fstk.stages.base import get_stage
from fstk.workflow.engine import Engine, EpisodeWorkspace, load_plan


@dataclass(frozen=True)
class ValueRow:
    key: str
    kind: str
    value: str
    unit: str
    origin: str
    by: str
    set: bool


@dataclass(frozen=True)
class PendingReview:
    job: str
    step: str
    stage: str
    review: str
    evidence: tuple[str, ...]
    detail: str


class Services:
    def __init__(self, profile: Profile):
        self.profile = profile
        self._catalog = None

    # ---------------------------------------------------------------- catalog / intake
    def catalog(self):
        from fstk.catalog.index import CatalogIndex

        if self._catalog is None:
            from fstk.catalog.build import resolve_path

            self._catalog = CatalogIndex.load(resolve_path(self.profile))
        return self._catalog

    def catalog_status(self) -> dict:
        from fstk.catalog.build import status

        return status(self.profile)

    def refresh_catalog(self) -> str:
        """Fetch a new dated snapshot (network; call from a worker thread) and reload the index."""
        from fstk.catalog.build import refresh

        path, _ = refresh(self.profile.data_root)
        self._catalog = None
        return str(path)

    # ---------------------------------------------------------------- shared workspace (ADR 0002)
    def shared_tree(self):
        """This device's SharedTree, or None when the profile has no shared_root or it is unreachable
        (``shared_problem`` then says why)."""
        from fstk.workspace.shared import SharedError, SharedTree

        self.shared_problem = ""
        if not self.profile.shared_root:
            self.shared_problem = "shared workspace off (no shared_root in this machine profile)"
            return None
        try:
            return SharedTree(self.profile)
        except (SharedError, FstkError, OSError) as exc:
            self.shared_problem = str(exc)
            return None

    def all_presence(self) -> dict[str, list]:
        tree = self.shared_tree()
        return tree.all_presence() if tree else {}

    def presence(self, episode: str | EpisodeId) -> list:
        tree = self.shared_tree()
        return tree.presence(EpisodeId.parse(str(episode))) if tree else []

    def handed_to_me(self, episode: str | EpisodeId):
        """The peer Presence that handed this episode to this device, if any."""
        tree = self.shared_tree()
        if not tree:
            return None
        for p in tree.presence(EpisodeId.parse(str(episode))):
            if not p.mine and p.state == "handed-off" and (p.detail.get("handoff") or {}).get("to") == tree.device:
                return p
        return None

    def adopt(self, episode: str | EpisodeId, *, source: str, actor: str, fork: bool = False) -> dict:
        tree = self.shared_tree()
        if tree is None:
            raise FstkError(self.shared_problem)
        return tree.adopt(EpisodeId.parse(str(episode)), source=source, actor=actor, fork=fork)

    def handoff(self, episode: str | EpisodeId, *, to: str, actor: str, note: str = "") -> dict:
        tree = self.shared_tree()
        if tree is None:
            raise FstkError(self.shared_problem)
        return tree.handoff(EpisodeId.parse(str(episode)), to=to, actor=actor, note=note)

    def frozen_reason(self, episode: str | EpisodeId) -> str | None:
        from fstk.workspace.shared import frozen_reason

        return frozen_reason(self.profile, EpisodeId.parse(str(episode)))

    def peers(self) -> list[str]:
        tree = self.shared_tree()
        return tree.peers() if tree else []

    def local_status(self, entry):
        from fstk.catalog.index import local_status

        return local_status(entry, self.profile)

    def create_from_catalog(self, entry, *, actor: str) -> EpisodeId:
        from fstk.workspace.episodes import create_from_catalog

        return create_from_catalog(self.profile, entry, actor=actor,
                                   catalog_summary=self.catalog().summary()).load().episode

    def create_from_local(self, folder: str | Path, *, actor: str) -> EpisodeId:
        from fstk.workspace.episodes import create_from_local

        return create_from_local(self.profile, folder, actor=actor).load().episode

    def write_intake_plan(self, episode: str | EpisodeId, *, include_stack_reference: bool = True) -> Path:
        from fstk.core.fsio import atomic_write_json
        from fstk.workflow.plans import expects_efm, intake_plan

        ws = self.workspace(episode)
        m = ws.store.load()
        plan = intake_plan(m.episode, download=bool(m.doc["sources"].get("archive_item")),
                           include_stack_reference=include_stack_reference, include_efm=expects_efm(m))
        path = ws.state_dir / "plans" / f"{plan['job']}.plan.json"
        atomic_write_json(path, plan)
        return path

    def write_delivery_plan(self, episode: str | EpisodeId) -> Path:
        """Everything after intake (``workflow.plans.delivery_plan``)."""
        from fstk.core.fsio import atomic_write_json
        from fstk.workflow.plans import delivery_plan

        ws = self.workspace(episode)
        plan = delivery_plan(ws.store.load())
        path = ws.state_dir / "plans" / f"{plan['job']}.plan.json"
        atomic_write_json(path, plan)
        return path

    @classmethod
    def load(cls, path: str | Path | None = None) -> Services:
        return cls(Profile.load(path))

    def workspace(self, episode: str | EpisodeId) -> EpisodeWorkspace:
        ep = episode if isinstance(episode, EpisodeId) else EpisodeId.parse(episode)
        return EpisodeWorkspace(self.profile, ep)

    # ---------------------------------------------------------------- browse
    def episodes(self) -> list[EpisodeId]:
        root = self.profile.data_root / "episodes"
        out = []
        if root.is_dir():
            for series_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                for code_dir in sorted(p for p in series_dir.iterdir() if p.is_dir()):
                    if (code_dir / "manifest" / "manifest.json").is_file():
                        try:
                            out.append(EpisodeId.parse(f"{series_dir.name}:{code_dir.name}"))
                        except FstkError:
                            continue
        return out

    def manifest_rows(self, episode: str | EpisodeId, *, include_unset: bool = True) -> list[ValueRow]:
        m = self.workspace(episode).store.load()
        rows = []
        for fd in FIELDS:
            entry = m.entry(fd.key)
            if entry is None and not include_unset:
                continue
            value = entry["value"] if entry else None
            text = ("—" if entry is None else
                    value if isinstance(value, str) else json.dumps(value)[:120])
            rows.append(ValueRow(fd.key, fd.kind, text, fd.unit, (entry or {}).get("origin", ""),
                                 (entry or {}).get("by", ""), entry is not None))
        return rows

    def title(self, episode: str | EpisodeId) -> str:
        return self.workspace(episode).store.load().title

    def jobs(self, episode: str | EpisodeId) -> list[str]:
        d = self.workspace(episode).state_dir / "plans"
        return sorted(p.name for p in d.iterdir() if (p / "state.json").is_file()) if d.is_dir() else []

    def job_state(self, episode: str | EpisodeId, job: str) -> dict[str, Any]:
        path = self.workspace(episode).job_dir(job) / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- reviews
    def pending_reviews(self, episode: str | EpisodeId) -> list[PendingReview]:
        out = []
        for job in self.jobs(episode):
            state = self.job_state(episode, job)
            for sid, st in state["steps"].items():
                if st.get("status") != "awaiting-review":
                    continue
                plan_step = st.get("stage")
                stage_id = plan_step or self._stage_of(episode, job, sid)
                stage = get_stage(stage_id)
                out.append(PendingReview(job, sid, stage_id, stage.review or "",
                                         tuple(fp["path"] for fp in st.get("outputs", [])),
                                         st.get("review_detail", "")))
        return out

    def _stage_of(self, episode, job: str, sid: str) -> str:
        plan_path = self.workspace(episode).job_dir(job) / "plan.json"
        if plan_path.is_file():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            for s in plan["steps"]:
                if s["id"] == sid:
                    return s["stage"]
        raise FstkError(f"cannot find stage for step {sid!r} of job {job!r}")

    def decide(self, episode: str | EpisodeId, review: PendingReview, *, decision: str, note: str,
               actor: str) -> Path:
        """Record APPROVED/REJECTED for exactly the evidence the reviewer was shown."""
        ws = self.workspace(episode)
        evidence = [Path(p) for p in review.evidence]
        if not evidence:
            raise FstkError("this step produced no evidence files to review")
        return ws.approvals.record(review=review.review, decision=decision, note=note, actor=actor,
                                   evidence=evidence, manifest=ws.store.load(),
                                   extra={"job": review.job, "step": review.step})

    # ---------------------------------------------------------------- EFM capture review
    def _ctx(self, episode: str | EpisodeId, actor: str = "ui"):
        from fstk.stages.base import StageContext

        ws = self.workspace(episode)
        return StageContext(self.profile, ws.episode, ws.store, actor)

    def efm_comparison(self, episode: str | EpisodeId) -> dict[str, Any]:
        """Everything the EFM capture review shows: metrics, problems, the automatic pick, pads."""
        from fstk.stages.efm import _comparison, efm_captures
        from fstk.tools.orc import project_source_labels
        from fstk.tools.orc_efm import PARETO_METRICS, least_bad, parse_alignment_map, unique_pareto

        ctx = self._ctx(episode)
        m = ctx.manifest
        quality, problems, provs = _comparison(ctx, m)
        pads: dict[str, int] = {}
        pad_problem = ""
        try:
            order = project_source_labels(ctx.artifact_path("stack_project", m))
            pads = parse_alignment_map(m.require("stack.alignment_map"), order)
        except FstkError as exc:
            pad_problem = str(exc)
        auto = (unique_pareto(quality) or least_bad(quality)) if not problems else None
        best = {}
        for k in PARETO_METRICS:
            vals = [q.get(k) for q in quality.values() if isinstance(q.get(k), (int, float))]
            best[k] = min(vals) if vals else None
        return {"labels": [c["label"] for c in efm_captures(m)],
                "rows": [{"capture": label, **q} for label, q in quality.items()],
                "problems": problems, "auto": auto.capture if auto else None,
                "auto_note": auto.note if auto else "", "best": best, "pads": pads, "pad_problem": pad_problem,
                "current": m.get("audio.capture"), "current_selection": m.get("audio.capture_selection"),
                "wavs": {label: str(self.profile.root("audio", ctx.episode) / p["wav"]["relative_path"])
                         for label, p in provs.items()}}

    def write_efm_select_plan(self, episode: str | EpisodeId, *, capture: str | None, note: str) -> Path:
        """Plan with one ``efm-select`` step carrying the operator's choice (or none → automatic)."""
        from fstk.core.fsio import atomic_write_json
        from fstk.core.timeutil import utc_stamp
        from fstk.workflow.engine import PLAN_SCHEMA, validate_plan

        ws = self.workspace(episode)
        params: dict[str, Any] = {}
        if capture:
            if len(note.strip()) < 8:
                raise FstkError("explain the choice (at least 8 characters): what you heard or measured")
            params = {"capture": capture, "note": note.strip()}
        plan = {"schema": PLAN_SCHEMA, "episode": ws.episode.canonical,
                "job": f"efm-select-{utc_stamp()[:15].lower()}",
                "steps": [{"id": "efm-select", "stage": "efm-select", "params": params, "after": []}]}
        validate_plan(plan)
        path = ws.state_dir / "plans" / f"{plan['job']}.plan.json"
        atomic_write_json(path, plan)
        return path

    def write_av_rerun_plan(self, episode: str | EpisodeId, *, verdicts: list[dict[str, Any]],
                            qc_report: str | Path, track: str = "ld") -> Path:
        """Plan for an A/V-check rerun: av-correction → rebuild the LD audio (``dvd-ld-audio`` for DVD
        episodes, ``dvd-free-audio`` otherwise) → remux → QC.

        ``verdicts``: ``[{"t_s": chunk centre, "verdict": "ok"|"early"|"late"}]`` from the delivery
        review. The rebuilt deliverable is a new version; nothing is overwritten.
        """
        from fstk.core.fsio import atomic_write_json
        from fstk.core.timeutil import utc_stamp
        from fstk.workflow.engine import PLAN_SCHEMA, validate_plan

        ws = self.workspace(episode)
        if not any(v.get("verdict") in ("early", "late") for v in verdicts):
            raise FstkError("mark at least one chunk 'audio too early' or 'audio too late' to rerun")
        m = ws.store.load()
        dvd = m.get("mode.dvd_reference_available")
        if dvd is None:
            raise FstkError("record the DVD reference first: `fstk episode dvd <episode> --find` (or --file PATH, or --none for LD-only)")
        audio_stage = "dvd-ld-audio" if dvd else "dvd-free-audio"
        job = f"av-rerun-{utc_stamp()[:15].lower()}"
        steps = [{"id": "av-correction", "stage": "av-correction", "after": [],
                  "params": {"track": track, "verdicts": verdicts, "qc_report": Path(qc_report).name}},
                 {"id": audio_stage, "stage": audio_stage, "params": {}, "after": ["av-correction"]},
                 {"id": "remux", "stage": "remux", "params": {}, "after": [audio_stage]},
                 {"id": "qc-report", "stage": "qc-report", "params": {}, "after": ["remux"]}]
        plan = {"schema": PLAN_SCHEMA, "episode": ws.episode.canonical, "job": job, "steps": steps}
        validate_plan(plan)
        path = ws.state_dir / "plans" / f"{job}.plan.json"
        atomic_write_json(path, plan)
        return path

    # ---------------------------------------------------------------- delivery QC review
    def qc_reports(self, episode: str | EpisodeId) -> list[Path]:
        d = self.profile.root("reports", self.workspace(episode).episode)
        return sorted(d.glob("qc-*.json"), reverse=True) if d.is_dir() else []

    def episode_quality(self, episode: str | EpisodeId) -> tuple[str, Path | None]:
        """Latest per-episode quality report as markdown, plus its contact-sheet image when one exists."""
        d = self.profile.root("reports", self.workspace(episode).episode)
        found = sorted(d.glob("episode-quality-*.json")) if d.is_dir() else []
        if not found:
            return "", None
        rep = json.loads(found[-1].read_text(encoding="utf-8"))
        from fstk.quality.report import render_markdown

        png = Path(rep["contact_sheet"]) if rep.get("contact_sheet") else None
        return render_markdown(rep), (png if png and png.is_file() else None)

    # ---------------------------------------------------------------- video-origin stretches
    def video_stretches(self, episode: str | EpisodeId) -> list[dict[str, Any]]:
        """Converted video-origin stretches: timing, current choice, what the latest encode script used, preview."""
        from fstk.media import hybrid

        ws = self.workspace(episode)
        m = ws.store.load()
        if not m.artifact("hybrid_plan"):
            return []
        root = self.profile.root("probe", ws.episode)
        doc = json.loads((root / m.artifact("hybrid_plan")["name"]).read_text(encoding="utf-8"))
        segs = hybrid.apply_choices([hybrid.Segment.from_dict(x) for x in doc["segments"]],
                                    m.get("video.hybrid_choices"))
        reports = self.profile.root("reports", ws.episode)
        used: dict[str, str] = {}
        trims = sorted(reports.glob("video-trim-*.json")) if reports.is_dir() else []
        if trims:
            vs_ = json.loads(trims[-1].read_text(encoding="utf-8")).get("video_stretches") or {}
            used = {str(x["first_cycle"]): x["treatment"] for x in vs_.get("segments", []) if x.get("active")}
        previews: dict[str, dict] = {}
        found = sorted(reports.glob("stretch-preview-*.json")) if reports.is_dir() else []
        if found:
            for p in json.loads(found[-1].read_text(encoding="utf-8")).get("previews", []):
                previews[str(p["first_cycle"])] = p
        head = int(m.get("video.head_trim") or 0)
        out = []
        for sg in segs:
            if not sg.active:
                continue
            key = str(sg.first_cycle)
            pv = previews.get(key, {})
            out.append({"first_cycle": sg.first_cycle, "kind": sg.kind, "seconds": round(sg.seconds, 2),
                        "start_s": round(max(0, sg.output_frames[0] - head) * 1001 / 24000, 2),
                        "treatment": sg.treatment, "encoded": used.get(key), "preview": pv.get("clip")})
        return out

    def write_stretch_plan(self, episode: str | EpisodeId, *, choices: dict[str, str], actor: str,
                           note: str) -> Path:
        """Record the per-stretch treatments and plan a fresh encode from the master with them.

        video-trim (new encode script) → pad-audit → pad-repair → frame-fixes → video-quality → encode-final → remux →
        qc-report → episode-report. Audio, trims and frame numbering are unaffected by the choice.
        """
        from fstk.core.fsio import atomic_write_json
        from fstk.core.timeutil import utc_stamp
        from fstk.media import hybrid
        from fstk.workflow.engine import PLAN_SCHEMA, validate_plan

        bad = {v for v in choices.values()} - set(hybrid.TREATMENTS)
        if bad:
            raise FstkError(f"unknown treatment(s): {sorted(bad)}")
        if len(note.strip()) < 8:
            raise FstkError("say what you saw in the previews (at least 8 characters)")
        ws = self.workspace(episode)
        with ws.store.transaction(by=actor, reason="video-origin stretch treatments chosen in the final review") as m:
            m.set("video.hybrid_choices", dict(sorted(choices.items())), by=actor, note=note.strip())
        job = f"stretches-{utc_stamp()[:15].lower()}"
        chain = [("video-trim", "video-trim"), ("pad-audit", "pad-audit"), ("pad-repair", "pad-repair"),
                 ("frame-fixes", "frame-fixes"), ("video-quality", "video-quality"), ("encode", "encode-final"), ("remux", "remux"),
                 ("qc", "qc-report"), ("episode-report", "episode-report")]
        steps, prev = [], None
        for sid, stage in chain:
            params: dict[str, Any] = {}
            if stage == "encode-final":
                frames = int(ws.store.load().get("video.frames") or 0)
                params = {"planned_gib": round(frames * 110_000 / 2**30, 2),
                          "estimate_basis": f"{frames} output frames × 110000 B (v38 deliveries ≈ 72 KB/frame)"}
            steps.append({"id": sid, "stage": stage, "after": [prev] if prev else [], "params": params})
            prev = sid
        plan = {"schema": PLAN_SCHEMA, "episode": ws.episode.canonical, "job": job, "steps": steps}
        validate_plan(plan)
        path = ws.state_dir / "plans" / f"{job}.plan.json"
        atomic_write_json(path, plan)
        return path

    # ---------------------------------------------------------------- operator frame fixes
    def fix_context(self, episode: str | EpisodeId) -> dict[str, Any]:
        """What the frame-fix tab needs: worst moments (as frames), current fixes, whether the DVD route exists."""
        ws = self.workspace(episode)
        m = ws.store.load()
        d = self.profile.root("reports", ws.episode)
        moments = []
        found = sorted(d.glob("episode-quality-*.json")) if d.is_dir() else []
        if found:
            for mo in json.loads(found[-1].read_text(encoding="utf-8")).get("moments", []):
                moments.append({**mo, "frame": round(float(mo["time_s"]) * 24000 / 1001)})
        return {"moments": moments, "fixes": list(m.get("video.manual_fixes") or []),
                "frames": m.get("video.frames"),
                "dvd": bool(m.get("mode.dvd_reference_available") is True and m.get("video.dvd_alignment")
                            and m.artifact("dvd"))}

    def write_fix_preview_plan(self, episode: str | EpisodeId, fix: dict[str, Any]) -> Path:
        from fstk.core.fsio import atomic_write_json
        from fstk.core.timeutil import utc_stamp
        from fstk.media import manual_fix as mf
        from fstk.workflow.engine import PLAN_SCHEMA, validate_plan

        ws = self.workspace(episode)
        frames = int(ws.store.load().get("video.frames") or 0)
        problems = mf.validate([mf.Fix.from_dict(fix)], frames=frames)
        if problems:
            raise FstkError("; ".join(problems))
        job = f"fix-preview-{utc_stamp()[:15].lower()}"
        plan = {"schema": PLAN_SCHEMA, "episode": ws.episode.canonical, "job": job,
                "steps": [{"id": "fix-preview", "stage": "fix-preview", "after": [], "params": {"fix": fix}}]}
        validate_plan(plan)
        path = ws.state_dir / "plans" / f"{job}.plan.json"
        atomic_write_json(path, plan)
        return path

    def latest_fix_preview(self, episode: str | EpisodeId) -> dict[str, Any] | None:
        d = self.profile.root("reports", self.workspace(episode).episode)
        found = sorted(d.glob("fix-preview-*.json")) if d.is_dir() else []
        return json.loads(found[-1].read_text(encoding="utf-8")) if found else None

    def write_fixes_plan(self, episode: str | EpisodeId, *, fixes: list[dict[str, Any]], actor: str) -> Path:
        """Record the operator's frame fixes and plan a fresh encode from the master with them:
        frame-fixes → video-quality → encode-final → remux → qc-report → episode-report."""
        from fstk.core.fsio import atomic_write_json
        from fstk.core.timeutil import utc_stamp
        from fstk.media import manual_fix as mf
        from fstk.workflow.engine import PLAN_SCHEMA, validate_plan

        ws = self.workspace(episode)
        m = ws.store.load()
        parsed = [mf.Fix.from_dict(f) for f in fixes]
        problems = mf.validate(parsed, frames=int(m.get("video.frames") or 0))
        if problems:
            raise FstkError("; ".join(problems))
        with ws.store.transaction(by=actor, reason="frame fixes chosen in the final review") as w:
            w.set("video.manual_fixes", [f.as_dict() for f in sorted(parsed, key=lambda f: f.first)], by=actor,
                  note=f"{len(parsed)} fix(es) from the final review")
        frames = int(m.get("video.frames") or 0)
        job = f"frame-fixes-{utc_stamp()[:15].lower()}"
        chain = [("frame-fixes", "frame-fixes"), ("video-quality", "video-quality"), ("encode", "encode-final"),
                 ("remux", "remux"), ("qc", "qc-report"), ("episode-report", "episode-report")]
        steps, prev = [], None
        for sid, stage in chain:
            params: dict[str, Any] = {}
            if stage == "encode-final":
                params = {"planned_gib": round(frames * 110_000 / 2**30, 2),
                          "estimate_basis": f"{frames} output frames × 110000 B (v38 deliveries ≈ 72 KB/frame)"}
            steps.append({"id": sid, "stage": stage, "after": [prev] if prev else [], "params": params})
            prev = sid
        plan = {"schema": PLAN_SCHEMA, "episode": ws.episode.canonical, "job": job, "steps": steps}
        validate_plan(plan)
        path = ws.state_dir / "plans" / f"{job}.plan.json"
        atomic_write_json(path, plan)
        return path

    def qc_result(self, path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def delivery_review_for(self, episode: str | EpisodeId, report: str | Path) -> PendingReview | None:
        """The pending ``delivery`` review whose evidence includes this QC report, if any."""
        want = str(Path(report))
        for p in self.pending_reviews(episode):
            if p.review == "delivery" and want in p.evidence:
                return p
        return None

    # ---------------------------------------------------------------- run
    def run_plan(self, plan_path: str | Path, *, actor: str,
                 events: Callable[[dict], None] | None = None) -> tuple[Engine, dict[str, Any]]:
        """Run a plan synchronously (call from a worker thread). Returns the engine and state."""
        engine = Engine(self.profile, load_plan(plan_path), actor=actor, events=events)
        return engine, engine.run()


def describe_field(key: str) -> str:
    fd = field_def(key)
    return f"{fd.description} ({fd.kind}{', ' + fd.unit if fd.unit else ''})"
