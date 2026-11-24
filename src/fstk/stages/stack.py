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
    independent FFprobe decode count. No human review (owner decision 2026-11-21; v38 had a
    ``review_gate`` here): the only human check is the delivery A/V check.
"""
from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path

from fstk.core.errors import ArtifactError, GateError, ToolError
from fstk.core.fsio import atomic_write_bytes, atomic_write_json, move_aside, read_json
from fstk.core.hashing import SCOPE_HEAD_TAIL
from fstk.core.timeutil import utc_now_iso
from fstk.tools.orc import project_source_labels, source_alignment_decision, validate_render_evidence
from fstk.tools.orc_project import (
    build_stack_project,
    bundled_template,
    find_capture_file,
    lossless_render_project,
    strip_timecode_pads,
)

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

LOG = logging.getLogger("fstk.stack")
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


LOSSLESS_MASTER_CODECS = ("ffv1",)


def _text_sha(text: str, paths: dict[str, Path] | None = None) -> str:
    """Key of a refresh: the project text plus the size and time of every capture it reads."""
    import hashlib

    parts = [text]
    for label, p in sorted((paths or {}).items()):
        try:
            st = Path(p).stat()
            parts.append(f"{label}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{label}:missing")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _load_pass(path: Path, order: list[str], key: str) -> dict | None:
    """A saved refresh result, only if it was made from exactly this project text and source order."""
    try:
        data = read_json(path) if path.is_file() else None
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            LOG.warning("damaged refresh cache %s moved aside (%s); the pass runs again", path, exc)
            move_aside(path)
        return None
    if (isinstance(data, dict) and data.get("order") == order and data.get("project_sha256") == key
            and isinstance(data.get("decision"), dict) and "orc_exit" in data):
        return data
    return None


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
        master = ctx.root("prores") / f"{ctx.episode.code}-stack-master.mkv"
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
            where = sorted(labels).index(first) + 1 if first in labels else 0
            ctx.report((n - 1) / MAX_REFERENCE_PASSES,
                       f"[capture {where} of {len(labels)}] refresh pass {n}: reference candidate {first}")
            text = build_stack_project(template, order, paths, Path(plan.data["master"]),
                                       f"{ctx.episode.code} weighted stack (pass {n})")
            project = scratch / f"pass{n}.orcprj"
            atomic_write_bytes(project, text.encode("utf-8"))
            saved = _load_pass(scratch / f"pass{n}.result.json", order, _text_sha(text, paths))
            if saved is not None:                       # this refresh finished before an earlier run stopped
                ctx.report(n / MAX_REFERENCE_PASSES, f"refresh pass {n}: reusing the saved result")
                decision, exit_code = saved["decision"], saved["orc_exit"]
            else:
                refreshed = orc.refresh(project, log_name=f"orc-refresh-pass{n}", cancel=ctx.cancel)
                decision = source_alignment_decision(refreshed.report, order)
                exit_code = refreshed.process.returncode
                atomic_write_json(scratch / f"pass{n}.result.json",
                                  {"order": order, "project_sha256": _text_sha(text, paths), "orc_exit": exit_code,
                                   "decision": decision})
            passes.append({"pass": n, "order": order, "orc_exit": exit_code, "decision": decision})
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
        # Orc pads every number a CLV disc's time code skips; those are not missing pictures (ADR 0004)
        stripped = strip_timecode_pads(final_project_text)
        frames = sum(coverage) - stripped.count
        recorded_frames = plan.data.get("recorded_frames")
        if (recorded_frames is not None and recorded_frames not in (frames, sum(coverage))
                and not plan.params.get("reselect")):
            raise ArtifactError(f"recorded stack.frames={recorded_frames} conflicts with Orc's {frames}; "
                                "rerun with reselect=true to re-measure")
        atomic_write_bytes(plan.data["project"], stripped.text.encode("utf-8"))
        report = {"schema": "fstk-stack-reference-v1", "created_utc": utc_now_iso(),
                  "episode": str(ctx.episode), "passes": passes, "selected": decision["suggested_label"],
                  "coverage_1_2_3plus": coverage, "project": plan.data["project"],
                  "timecode_pads_removed": {"count": stripped.count, "per_capture": stripped.per_capture,
                                            "other_pads_kept": stripped.kept, "frames_orc": sum(coverage),
                                            "frames": frames},
                  "selector": "Orc suggestedReference (no custom score)"}
        atomic_write_json(plan.data["report"], report)
        out = StepOutput(report=Path(plan.data["report"]))
        out.measure("stack.reference", decision["suggested_label"])
        out.measure("stack.alignment_map", decision["alignment_map"])
        out.measure("stack.frames", frames)
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
    description = "Orc --process with artifact evidence validation; no human review (owner decision 2026-11-21)."
    requires = (ValueSet("stack.reference"), ValueSet("stack.alignment_map"), ValueSet("stack.frames"),
                ArtifactExists("stack_project"))
    writes = ("stack.frames",)
    writes_artifacts = ("stack_master",)
    resource = "compute"

    def plan(self, ctx, params):
        try:
            planned = float(params["planned_gib"])
            basis = str(params["estimate_basis"])
        except (KeyError, TypeError, ValueError):
            raise GateError("stack-render needs params planned_gib and estimate_basis: a defensible "
                            "estimate for this episode (never another episode's file size)") from None
        m = ctx.manifest
        project = ctx.artifact_path("stack_project", m)
        # The master is always rendered lossless to <code>-stack-master.mkv. An older master recorded for this
        # episode (a v38 ProRes .mov) is left untouched; a finished lossless master is reused, not re-rendered.
        master = ctx.root("prores") / f"{ctx.episode.code}-stack-master.mkv"
        reuse = master.exists() and not params.get("replace_existing_master")
        if master.exists() and params.get("replace_existing_master"):
            raise GateError(f"{master} exists; move it aside before re-rendering (fstk never overwrites a master)")
        render_project = ctx.root("stack") / f"{project.stem}-lossless.orcprj"
        report = ctx.root("reports") / "stack-render.json"
        # A project made before ADR 0004 still carries Orc's CLV time-code PADs: they are taken out here, and a
        # master already rendered with them loses them by a lossless copy. What the master holds is recorded by
        # the project it was rendered from (``…-lossless.orcprj``), so that is where its PADs are read; the
        # stack project itself may already have been stripped (CP53 stripped it before checking the master).
        pads = strip_timecode_pads(project.read_text(encoding="utf-8"))
        project_has_pads = pads.count > 0
        if reuse and render_project.is_file():
            rendered = strip_timecode_pads(render_project.read_text(encoding="utf-8"))
            if rendered.count:
                if rendered.text != lossless_render_project(pads.text, master):
                    raise GateError(f"{master.name} was rendered from {render_project.name}, which is not the "
                                    f"current {project.name} (apart from its time-code PADs); move the master "
                                    "aside to re-render it")
                pads = rendered
        recorded = int(m.require("stack.frames"))
        # stack.frames counts the PADs when the project still has them (made before ADR 0004). With the project
        # already stripped it may count them or not (CP53 re-measured an unmigrated master), so the pad-free count
        # then comes from the master's own packets in run().
        frames = recorded - pads.count if project_has_pads else (None if pads.count else recorded)
        space = None
        if not reuse:
            space = SpaceNeed("prores", planned, basis)
        elif pads.count:
            space = SpaceNeed("prores", master.stat().st_size / 2**30,
                              "the existing master is copied once without its time-code PAD frames")
        return StepPlan(self.id, params, inputs=[project], outputs=[master, report], space=space,
                        summary=(f"reuse the lossless master {master.name}"
                                 + (f" without its {pads.count} time-code PAD frames" if pads.count else "")
                                 if reuse else f"render {project.name} → {master.name} (FFV1)"),
                        data={"project": str(project), "render_project": str(render_project), "master": str(master),
                              "report": str(report), "frames": frames, "recorded_frames": recorded,
                              "pad_positions": list(pads.master_positions),
                              "groups": m.get("stack.pressing_groups"), "reuse": reuse})

    def run(self, ctx, plan):
        orc, ff = ctx.tool("orc"), ctx.tool("ffmpeg")
        d = plan.data
        project, master = Path(d["project"]), Path(d["master"])
        labels = project_source_labels(project)
        original = project.read_text(encoding="utf-8")
        stripped = strip_timecode_pads(original)
        if stripped.count:                       # the project itself goes without them from now on
            kept = project.with_name(project.stem + "-with-timecode-pads.orcprj")
            if not kept.exists():
                atomic_write_bytes(kept, original.encode("utf-8"))
            atomic_write_bytes(project, stripped.text.encode("utf-8"))
        if d["reuse"]:
            ctx.report(0.05, f"checking the existing master {master.name}")
            codec = ff.probe(master).only("video").raw.get("codec_name")
            issues = []
            migration = None
            if codec not in LOSSLESS_MASTER_CODECS:
                issues.append(f"existing {master.name} is {codec!r}, not lossless; move it aside to re-render")
            index = ff.packet_index(master, cancel=ctx.cancel)
            actual = len(index["pts"])
            positions = d["pad_positions"]
            expected = d["frames"]
            if not issues and positions:
                sizes = index["size"]
                typical = sorted(sizes)[len(sizes) // 2] if sizes else 0
                blank = [p < actual and sizes[p] <= typical / 5 for p in positions]
                kept = master.with_name(master.stem + "-with-timecode-pads.mkv")
                if all(blank) and actual in {int(d["recorded_frames"]), int(d["recorded_frames"]) + len(positions)}:
                    migration = self._drop_timecode_pads(ctx, ff, master, index, positions)
                    actual = migration["frames"]
                elif not any(blank) and kept.is_file():
                    # an earlier run already took them out (its render project was not updated before CP56)
                    migration = {"count": len(positions), "frames": actual, "original_kept_as": kept.name,
                                 "method": "already removed by an earlier run; render project brought up to date"}
                else:
                    issues.append(f"{master.name}: {sum(blank)} of the {len(positions)} time-code PAD positions its "
                                  f"render project lists are blank frames; expected all (not yet removed) or none "
                                  f"(removed earlier, {kept.name} kept). Not touched")
                if migration:
                    expected = migration["frames"]
                    self._record_render_project(Path(d["render_project"]), stripped.text, master)
            if expected is None:
                expected = actual
            if actual != int(expected):
                issues.append(f"existing {master.name} has {actual} frames; it should have {expected}")
            d["frames"] = int(expected)
            evidence = {"ok": not issues, "issues": issues, "reused": True, "master_codec": codec,
                        "decoded_frames": actual, "frames_counted_from": "packets (intra-only FFV1)"}
            if migration:
                evidence["timecode_pads_removed"] = migration
            res_desc, exit_code = "reused existing master", None
        else:
            render = Path(d["render_project"])
            atomic_write_bytes(render, lossless_render_project(stripped.text, master).encode())
            master.parent.mkdir(parents=True, exist_ok=True)
            ctx.report(None, f"Orc render of {len(labels)} captures (lossless FFV1)")
            from fstk.tools.progress_lines import LineProgress

            res, text = orc.process_project(render, cancel=ctx.cancel,
                                            on_line=LineProgress(ctx.report, total_frames=int(d["frames"]), span=0.88,
                                                                 label="Orc render"))
            ctx.check_cancel()
            if not master.is_file() or master.stat().st_size == 0:
                raise ArtifactError(f"Orc produced no stack master ({res.describe()}); if this Orc build rejects "
                                    "ffmpeg_format mkv-ffv1 the log above says so")
            ctx.report(0.9, "decode-counting master frames")
            actual = ff.count_frames(master)
            evidence = validate_render_evidence(text, labels, int(d["frames"]), actual, d["groups"])
            codec = ff.probe(master).only("video").raw.get("codec_name")
            evidence["master_codec"] = codec
            if codec not in LOSSLESS_MASTER_CODECS:
                evidence["ok"] = False
                evidence.setdefault("issues", []).append(
                    f"stack master is {codec!r}, not lossless ({', '.join(LOSSLESS_MASTER_CODECS)}): every later picture "
                    "step reads it, so a lossy master would put a second lossy generation before the final encode")
            res_desc, exit_code = res.describe(), res.returncode
        atomic_write_json(d["report"], {
            "schema": "fstk-stack-render-v1", "created_utc": utc_now_iso(), "episode": str(ctx.episode),
            "orc_exit_code": exit_code, "orc": res_desc, "project": str(project), "master": str(master),
            "expected_frames": d["frames"], "evidence": evidence,
            "status": "PASS" if evidence["ok"] else "REVIEW_REQUIRED",
            "note": "artifact checks only; picture quality is judged in the delivery A/V check"})
        out = StepOutput(report=Path(d["report"]))
        out.data["evidence"] = evidence
        if stripped.count:
            out.rewrote_inputs.append(project)   # its own upgrade, not an outside change
        if evidence["ok"]:
            out.artifacts["stack_master"] = {"root": "prores",
                                             "name": master.relative_to(ctx.root("prores")).as_posix()}
            out.measure("stack.frames", int(d["frames"]))
        return out

    @staticmethod
    def _record_render_project(render: Path, stack_text: str, master: Path) -> None:
        """The render project records what the master holds: after its PADs are gone it is rewritten without
        them (the old one kept as ``…-with-timecode-pads.orcprj``)."""
        if render.is_file():
            keep = render.with_name(render.stem + "-with-timecode-pads.orcprj")
            if not keep.exists():
                atomic_write_bytes(keep, render.read_bytes())
        atomic_write_bytes(render, lossless_render_project(stack_text, master).encode())

    def _drop_timecode_pads(self, ctx, ff, master: Path, index: dict, positions: list[int]) -> dict:
        """Take Orc's CLV time-code PAD frames out of a master rendered with them, without re-encoding.

        Proven first, or nothing is touched: every packet is a keyframe (each FFV1 frame stands alone) with rising
        timestamps, and every predicted position is a tiny packet that decodes to an all-zero picture. The copy
        replaces the master; the original stays beside it as ``…-with-timecode-pads.mkv``.
        """
        import numpy as np

        sizes = np.asarray(index["size"], dtype=np.int64)
        pts = np.asarray(index["pts"], dtype=np.int64)
        if not all(index["key"]):
            raise ArtifactError(f"{master.name} has non-key frames; its PAD frames cannot be dropped by copying "
                                "(move it aside and re-render)")
        if np.any(np.diff(pts) <= 0):
            raise ArtifactError(f"{master.name} has timestamps that do not rise; not touched")
        typical = float(np.median(sizes))
        tiny = [p for p in positions if p >= len(sizes) or sizes[p] > typical / 5]   # screen only; the decode below is the proof
        if tiny:
            raise ArtifactError(f"{master.name}: frame(s) {tiny[:5]} where Orc put a time-code PAD are not blank "
                                "packets; not touched")
        ctx.report(0.15, f"proving the {len(positions)} time-code PAD frames are blank")
        tb = Fraction(index["time_base"])
        for p in positions:
            ctx.check_cancel()
            try:
                frame = ff.frame_at(master, float(pts[p] * tb), 16, 12, cancel=ctx.cancel)
            except ToolError as e:
                raise ToolError(f"{master.name}: could not read frame {p} to check it is a time-code PAD; the "
                                f"drive gave a read error. Nothing was changed; run again (if it repeats, check "
                                f"the drive). {e}") from e
            if int(frame.max()) > 0:
                raise ArtifactError(f"{master.name}: frame {p} where Orc put a time-code PAD is not all-zero; "
                                    "not touched")
        tmp = master.with_name(master.stem + ".without-timecode-pads.partial.mkv")
        kept = master.with_name(master.stem + "-with-timecode-pads.mkv")
        if kept.exists():
            raise ArtifactError(f"{kept.name} already exists; move it away and run again")
        from fstk.tools.progress_lines import LineProgress

        ctx.report(0.2, f"copying {master.name} without its {len(positions)} time-code PAD frames (no re-encode)")
        ff.drop_frames(master, tmp, positions, cancel=ctx.cancel,
                       on_line=LineProgress(ctx.report, total_frames=len(sizes), span=0.75, base=0.2,
                                            label="lossless copy"))
        ctx.report(0.95, "checking the copy")
        copy = ff.packet_index(tmp, cancel=ctx.cancel)
        want = len(sizes) - len(positions)
        if len(copy["pts"]) != want or not all(copy["key"]) or np.any(np.diff(copy["pts"]) <= 0):
            tmp.rename(tmp.with_name(tmp.stem + ".rejected.mkv"))
            raise ArtifactError(f"the copy has {len(copy['pts'])} frames (expected {want}) or irregular "
                                "timestamps; the original master is unchanged")
        keep = np.ones(len(sizes), dtype=bool)
        keep[list(positions)] = False
        if not np.array_equal(np.asarray(copy["size"]), sizes[keep]):
            tmp.rename(tmp.with_name(tmp.stem + ".rejected.mkv"))
            raise ArtifactError("the copy's frames differ from the original's; the original master is unchanged")
        master.rename(kept)
        tmp.rename(master)
        return {"count": len(positions), "positions": list(positions), "frames": want,
                "original_kept_as": kept.name, "method": "stream copy (noise=drop, setts); packets byte-identical"}

    def still_current(self, step_state: dict) -> str | None:
        """A master that still holds Orc's CLV time-code PAD frames is not current (ADR 0004): CP53 could pass
        one while its project had already been stripped. Its render project records what it holds."""
        for fp in step_state.get("inputs", []):
            path = Path(fp.get("path", ""))
            if path.suffix != ".orcprj" or path.stem.endswith("-lossless"):
                continue
            render = path.with_name(path.stem + "-lossless.orcprj")
            try:
                if render.is_file() and strip_timecode_pads(render.read_text(encoding="utf-8")).count:
                    return f"the stack master still has Orc's time-code PAD frames ({render.name}; ADR 0004)"
            except Exception as e:  # unreadable or inconsistent: run the step, which reports it properly
                return f"cannot tell whether the stack master still has time-code PADs: {e}"
        return None

    def verify(self, ctx, plan, output):
        return list(output.data["evidence"]["issues"])
