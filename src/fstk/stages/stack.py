"""Weighted-stack stages (Orc).

``stack-reference``
    Ported behaviour of v38 ``select_stack_reference.py`` + ``record_orc_reference_measurements``:
    build a scratch project in a deterministic provisional order, run Orc's *refresh* (never
    ``--process``), map Orc's ``suggestedReference`` to a capture label, and repeat with that
    capture first until the recommendation is stable (≤ 3 passes). Orc's own recommendation is
    the selector; no custom score. Records ``stack.reference``, ``stack.alignment_map``,
    ``stack.frames`` (= 1/2/3+-source coverage counts of the final pass) and the fit statistics.
    A previously recorded reference that Orc disagrees with stops the stage unless ``reselect``.

``stack-render``
    Ported behaviour of v38 ``render_stack.py``: render the refreshed project, then validate
    the *artifact*: one ``Loaded`` line per capture, StackerStage multi-input evidence, zero
    unrecoverable samples, Orc's written-frame count == measured ``stack.frames`` == an
    independent FFprobe decode count. Requires a human review of the render report before
    downstream stages run (v38 ``review_gate``).
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, read_json
from fstk.core.hashing import SCOPE_HEAD_TAIL
from fstk.core.timeutil import utc_now_iso
from fstk.tools.orc import project_source_labels, source_alignment_decision, validate_render_evidence
from fstk.tools.orc_project import build_stack_project, bundled_template, find_capture_file

from .base import (
    ArtifactExists,
    CapturesAtLeast,
    Custom,
    SpaceNeed,
    Stage,
    StepOutput,
    StepPlan,
    ValueSet,
    register,
)

MAX_REFERENCE_PASSES = 3


def _downloads(ctx, manifest) -> Path:
    override = manifest.doc["sources"].get("downloads_override")
    return Path(override) if override else ctx.profile.downloads_dir(ctx.episode)


def _capture_paths(ctx, manifest) -> dict[str, Path]:
    folder = _downloads(ctx, manifest)
    if not folder.is_dir():
        raise GateError(f"capture folder missing: {folder}")
    return {label: find_capture_file(folder, label) for label in manifest.capture_labels}


def _downloads_present(ctx, manifest):
    try:
        paths = _capture_paths(ctx, manifest)
    except Exception as exc:
        return False, str(exc)
    return True, f"{len(paths)} TBC+JSON pairs"


@register
class StackReference(Stage):
    id = "stack-reference"
    title = "Select stack reference (Orc refresh)"
    description = "Orc refresh loop; records reference, alignment map, stack frames and fit statistics."
    requires = (CapturesAtLeast(2), Custom("captures-present", _downloads_present))
    writes = ("stack.reference", "stack.alignment_map", "stack.frames", "stack.fit_trusted",
              "stack.fell_back_to_mean", "stack.stack_sigma", "stack.best_single_sigma")
    writes_artifacts = ("stack_project", "stack_master")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        paths = _capture_paths(ctx, m)
        stack_dir = ctx.root("stack")
        project = stack_dir / f"stack-full-{ctx.episode.slug}.orcprj"
        master = ctx.root("prores") / f"{ctx.episode.code}-stack-master.mov"
        report = ctx.root("reports") / "stack-reference.json"
        inputs = [p for label in m.capture_labels for p in (paths[label], Path(str(paths[label]) + ".json"))]
        return StepPlan(self.id, params, inputs=inputs, outputs=[project, report],
                        input_scope={str(p): SCOPE_HEAD_TAIL for p in inputs},
                        summary=f"Orc refresh loop over {len(paths)} captures",
                        data={"paths": {k: str(v) for k, v in paths.items()}, "project": str(project),
                              "master": str(master), "report": str(report), "scratch": str(stack_dir / "scratch"),
                              "recorded_reference": m.get("stack.reference"),
                              "recorded_frames": m.get("stack.frames"),
                              "labels": m.capture_labels})

    def run(self, ctx, plan):
        orc = ctx.tool("orc")
        labels: list[str] = list(plan.data["labels"])
        paths = {k: Path(v) for k, v in plan.data["paths"].items()}
        recorded = plan.data["recorded_reference"]
        first = recorded if recorded in labels else sorted(labels)[0]
        template = bundled_template()
        scratch = Path(plan.data["scratch"])
        passes = []
        decision = None
        final_project_text = None
        for n in range(1, MAX_REFERENCE_PASSES + 1):
            ctx.check_cancel()
            order = [first, *[c for c in labels if c != first]]
            ctx.report((n - 1) / MAX_REFERENCE_PASSES, f"refresh pass {n}: reference candidate {first}")
            text = build_stack_project(template, order, paths, Path(plan.data["master"]),
                                       f"{ctx.episode.code} weighted stack (pass {n})")
            project = scratch / f"pass{n}.orcprj"
            atomic_write_bytes(project, text.encode("utf-8"))
            refreshed = orc.refresh(project, log_name=f"orc-refresh-pass{n}", cancel=ctx.cancel)
            decision = source_alignment_decision(refreshed.report, order)
            passes.append({"pass": n, "order": order, "orc_exit": refreshed.process.returncode,
                           "decision": decision})
            if decision["suggested_label"] == first:
                final_project_text = project.read_text(encoding="utf-8")
                break
            first = decision["suggested_label"]
        if final_project_text is None or decision is None:
            raise ArtifactError(f"Orc's recommendation did not stabilise within {MAX_REFERENCE_PASSES} "
                                f"refreshes: {[p['decision']['suggested_label'] for p in passes]}")
        if recorded and recorded != decision["suggested_label"] and not plan.params.get("reselect"):
            raise ArtifactError(f"recorded reference {recorded!r} disagrees with Orc's stable "
                                f"recommendation {decision['suggested_label']!r}; rerun with reselect=true")
        coverage = [decision.get("frames_with_1_source"), decision.get("frames_with_2_sources"),
                    decision.get("frames_with_3plus_sources")]
        if not all(isinstance(c, int) and c >= 0 for c in coverage) or sum(coverage) <= 0:
            raise ArtifactError(f"Orc coverage counts are missing or invalid: {coverage}")
        if not decision.get("alignment_map"):
            raise ArtifactError("Orc did not report a measured alignment map")
        recorded_frames = plan.data.get("recorded_frames")
        if recorded_frames is not None and recorded_frames != sum(coverage) and not plan.params.get("reselect"):
            raise ArtifactError(f"recorded stack.frames={recorded_frames} conflicts with Orc's {sum(coverage)}; "
                                "rerun with reselect=true to re-measure")
        atomic_write_bytes(plan.data["project"], final_project_text.encode("utf-8"))
        report = {"schema": "fstk-stack-reference-v1", "created_utc": utc_now_iso(),
                  "episode": str(ctx.episode), "passes": passes, "selected": decision["suggested_label"],
                  "coverage_1_2_3plus": coverage, "project": plan.data["project"],
                  "selector": "Orc suggestedReference (no custom score)"}
        atomic_write_json(plan.data["report"], report)
        out = StepOutput(report=Path(plan.data["report"]))
        out.measure("stack.reference", decision["suggested_label"])
        out.measure("stack.alignment_map", decision["alignment_map"])
        out.measure("stack.frames", sum(coverage))
        for key, src in (("stack.fit_trusted", "fit_trusted"), ("stack.fell_back_to_mean", "fell_back_to_mean")):
            if isinstance(decision.get(src), bool):
                out.measure(key, decision[src])
        for key, src in (("stack.stack_sigma", "stack_sigma"), ("stack.best_single_sigma", "best_single_sigma")):
            if isinstance(decision.get(src), (int, float)) and not isinstance(decision.get(src), bool):
                out.measure(key, float(decision[src]))
        stack_root, prores_root = ctx.root("stack"), ctx.root("prores")
        out.artifacts["stack_project"] = {"root": "stack",
                                          "name": Path(plan.data["project"]).relative_to(stack_root).as_posix()}
        out.artifacts["stack_master"] = {"root": "prores",
                                         "name": Path(plan.data["master"]).relative_to(prores_root).as_posix()}
        return out

    def verify(self, ctx, plan, output):
        issues = []
        project = Path(plan.data["project"])
        labels = project_source_labels(project)
        selected = read_json(plan.data["report"])["selected"]
        if not labels or labels[0] != selected:
            issues.append(f"saved project order {labels} does not start with the selected reference {selected}")
        if sorted(labels) != sorted(plan.data["labels"]):
            issues.append("saved project does not contain exactly the inventoried captures")
        return issues


def _stack_frames_recorded(ctx, manifest):
    return manifest.is_set("stack.frames"), "stack.frames measured by stack-reference"


@register
class StackRender(Stage):
    id = "stack-render"
    title = "Render weighted stack (Orc)"
    description = "Orc --process with artifact evidence validation; human quality review required."
    requires = (ValueSet("stack.reference"), ValueSet("stack.alignment_map"), ValueSet("stack.frames"),
                ArtifactExists("stack_project"))
    writes = ()
    resource = "compute"
    review = "stack-render"

    def plan(self, ctx, params):
        try:
            planned = float(params["planned_gib"])
            basis = str(params["estimate_basis"])
        except (KeyError, TypeError, ValueError):
            raise GateError("stack-render needs params planned_gib and estimate_basis: a defensible "
                            "estimate for this episode (never another episode's file size)") from None
        m = ctx.manifest
        project = ctx.artifact_path("stack_project", m)
        master = ctx.artifact_path("stack_master", m)
        if master.exists() and not params.get("replace_existing_master"):
            raise GateError(f"stack master already exists: {master} (set replace_existing_master to re-render)")
        report = ctx.root("reports") / "stack-render.json"
        return StepPlan(self.id, params, inputs=[project], outputs=[master, report],
                        space=SpaceNeed("prores", planned, basis),
                        summary=f"render {project.name} → {master.name}",
                        data={"project": str(project), "master": str(master), "report": str(report),
                              "frames": m.require("stack.frames"),
                              "groups": m.get("stack.pressing_groups")})

    def run(self, ctx, plan):
        orc, ff = ctx.tool("orc"), ctx.tool("ffmpeg")
        project, master = Path(plan.data["project"]), Path(plan.data["master"])
        labels = project_source_labels(project)
        master.parent.mkdir(parents=True, exist_ok=True)
        ctx.report(0.0, f"Orc render of {len(labels)} captures")
        res, text = orc.process_project(project, cancel=ctx.cancel)
        ctx.check_cancel()
        if not master.is_file() or master.stat().st_size == 0:
            raise ArtifactError(f"Orc produced no stack master ({res.describe()})")
        ctx.report(0.9, "decode-counting master frames")
        actual = ff.count_frames(master)
        evidence = validate_render_evidence(text, labels, int(plan.data["frames"]), actual, plan.data["groups"])
        atomic_write_json(plan.data["report"], {
            "schema": "fstk-stack-render-v1", "created_utc": utc_now_iso(), "episode": str(ctx.episode),
            "orc_exit_code": res.returncode, "project": str(project), "master": str(master),
            "expected_frames": plan.data["frames"], "evidence": evidence,
            "status": "PASS" if evidence["ok"] else "REVIEW_REQUIRED",
            "note": "artifact checks only; human quality review is still required"})
        out = StepOutput(report=Path(plan.data["report"]))
        out.data["evidence"] = evidence
        return out

    def verify(self, ctx, plan, output):
        return list(output.data["evidence"]["issues"])
