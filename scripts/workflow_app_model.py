#!/usr/bin/env python3
"""Safe, testable intake-plan generation for the local workflow GUI."""
from __future__ import annotations

import copy
import csv
import getpass
import hashlib
import json
import math
import os
import platform
import re
import socket
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from typing import Any

PLAN_SCHEMA = "ld-pipeline-workflow-plan-v1"
EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)
EPISODE_SEARCH_RE = re.compile(r"S\d{2}E\d{2}", re.IGNORECASE)
ARCHIVE_PICKER_SERIES = ("DS9", "VOY", "TNG")


class WorkflowAppError(ValueError):
    """Invalid GUI input or an unsupported intake template."""


def load_archive_picker_catalog(path: str | Path) -> dict[str, Any]:
    """Load and validate the bundled metadata-only Section 31 navigation snapshot."""
    from archive_catalog import CATALOG_SCHEMA, load_catalog

    catalog_path = Path(path)
    try:
        catalog = load_catalog(catalog_path)
    except ValueError as exc:
        raise WorkflowAppError(str(exc)) from exc
    if catalog.get("schema") != CATALOG_SCHEMA:
        raise WorkflowAppError(f"unsupported Archive catalog schema in {catalog_path}")
    return catalog


def build_archive_picker_tree(catalog: dict[str, Any]) -> dict[str, dict[int, list[dict[str, Any]]]]:
    """Group catalog episodes as series -> season -> episode, preserving known limitations."""
    from archive_catalog import CATALOG_SCHEMA

    if catalog.get("schema") != CATALOG_SCHEMA:
        raise WorkflowAppError("unsupported Archive catalog schema")
    grouped: dict[str, dict[int, list[dict[str, Any]]]] = {
        series: {} for series in ARCHIVE_PICKER_SERIES
    }
    items = [item for section in catalog.get("children", [])
             for item in section.get("items", [])]
    for item in items:
        series = item.get("series")
        if series not in grouped:
            continue
        entries: list[dict[str, Any]] = []
        if item.get("category") == "episode-copy":
            entries.append({"series": series, "season": item.get("season"),
                            "episode": item.get("episode"), "identifier": item.get("identifier"),
                            "title": _archive_catalog_episode_title(item, series),
                            "kind": "episode-copy"})
        elif item.get("category") == "multi-episode-extra":
            for episode_group in item.get("episode_groups", []):
                candidates = episode_group.get("title_candidates", [])
                entries.append({"series": series, "season": episode_group.get("season"),
                                "episode": episode_group.get("episode"),
                                "identifier": item.get("identifier"),
                                "title": " / ".join(str(title) for title in candidates),
                                "kind": "combined-item"})
        for entry in entries:
            season, episode = entry["season"], entry["episode"]
            if (not isinstance(season, int) or not isinstance(episode, int)
                    or season < 1 or episode < 1):
                continue
            grouped[series].setdefault(season, []).append(entry)
    for seasons in grouped.values():
        for entries in seasons.values():
            entries.sort(key=lambda entry: (entry["episode"], entry["identifier"] or ""))
    return grouped


def _archive_catalog_episode_title(item: dict[str, Any], series: str) -> str:
    """Extract a display-only title from a capture filename when available."""
    episode_code = f"S{int(item['season']):02d}E{int(item['episode']):02d}"
    prefix = re.compile(rf"^{re.escape(series)}-{episode_code}-(.+)$", re.IGNORECASE)
    capture_suffix = re.compile(r"-(?:L|M|P)\d+$", re.IGNORECASE)
    for row in item.get("files", []):
        name = Path(str(row.get("name", ""))).name
        if not name.casefold().endswith(".tbc"):
            continue
        match = prefix.match(name[:-4])
        if match:
            title = capture_suffix.sub("", match.group(1)).strip(" -_")
            if title:
                return title
    return ""


def resolve_archive_picker_selection(catalog: dict[str, Any], series: str,
                                     season: int, episode: int) -> dict[str, Any]:
    """Resolve the catalog route and permit supported standalone DS9/VOY identities."""
    from archive_catalog import resolve_episode

    series = series.upper()
    if series not in ARCHIVE_PICKER_SERIES:
        raise WorkflowAppError(f"unsupported Archive picker series: {series}")
    result = resolve_episode(catalog, series, season, episode)
    warnings: list[str] = []
    if result.get("status") == "matched":
        warnings = list((result.get("catalog_item") or {}).get("workflow_warnings", []))
    elif result.get("status") == "combined-item-only":
        warnings = [result.get("reason", "This episode is listed only inside a combined Archive item.")]
    elif result.get("status") == "no-standalone-item":
        warnings = ["No standalone Archive item is available for this episode; combined-item matches are lookup-only."]
    route = result.get("workflow_route") or {}
    selectable = (result.get("status") == "matched" and
                  series in ("DS9", "VOY") and route.get("supported") is True)
    if series == "DS9" and any("Emissary" in warning for warning in warnings):
        selectable = False
    result["picker_warnings"] = warnings
    result["picker_selectable"] = selectable
    if not selectable:
        if series == "DS9" and any("Emissary" in warning for warning in warnings):
            result["picker_reason"] = "Emissary requires its dedicated two-part workflow and cannot use generic episode intake."
        elif result.get("status") == "combined-item-only":
            result["picker_reason"] = "This episode exists only in a combined item; the picker will not route it as a single-episode download."
        elif series == "TNG":
            result["picker_reason"] = "TNG is not enabled for single-episode processing; combined-item matches remain lookup-only."
        else:
            result["picker_reason"] = "No safe standalone episode item is available in this catalog snapshot."
    return result


def inspect_stack_console(profile_value: str | Path, manifest_value: str | Path,
                          episode: str, instance_root_value: str | Path | None = None,
                          series: str = "DS9") -> dict[str, Any]:
    """Determine the next safe stack-pilot phase from the current manifest/artifacts."""
    code = str(episode).strip().upper()
    if not EPISODE_RE.fullmatch(code):
        raise WorkflowAppError("stage console needs a resolved SxxExx episode")
    series = str(series or "DS9").strip().upper()
    if series not in ("DS9", "VOY"):
        raise WorkflowAppError("stage console supports DS9 or VOY series")
    identity = f"{series}:{code}"
    env_keys = ("LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY",
                "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR", "LDP_INSTANCE_MODE",
                "LDP_INSTANCE_LOGS", "LDP_CUT_PAD_IMAGES")
    prior_env = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ["LDP_PROFILE"] = str(Path(profile_value).expanduser().resolve(strict=False))
        os.environ["LDP_SERIES"] = series
        os.environ["LDP_EPISODE"] = code
        os.environ["LDP_IDENTITY"] = identity
        if instance_root_value:
            os.environ["LDP_INSTANCE_ROOT"] = str(Path(instance_root_value).expanduser().resolve(strict=False))
            os.environ["LDP_MANIFEST_DIR"] = str(
                Path(instance_root_value).expanduser().resolve(strict=False) / "manifest")
        else:
            for key in env_keys[4:]:
                os.environ.pop(key, None)
        profile_path = Path(profile_value).expanduser().resolve(strict=True)
        manifest_path = Path(manifest_value).expanduser().resolve(strict=True)
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        from ldcommon import Episode, Ld, PipelineError, device_workspace_enabled
        ld = Ld(profile_path)
        if str(manifest_data.get("episode", "")).upper() != code:
            raise WorkflowAppError("manifest episode does not match the selected episode")
        if str(manifest_data.get("series", "DS9")).upper() != series:
            raise WorkflowAppError(f"manifest series does not match selected identity {identity}")
        if manifest_data.get("record_status") in ("template", "measured-partial"):
            raise WorkflowAppError("stage console requires an initialized, one-part episode manifest")
        if manifest_data.get("format") == "two_part":
            raise WorkflowAppError("the stack console pilot does not handle two-part episodes")
        if device_workspace_enabled(ld.profile) and not instance_root_value:
            raise WorkflowAppError("resolve the shared/device episode workspace before building a stack plan")
        episode_data = Episode(code, manifest_data, ld, series)
        labels = episode_data.capture_set
        if not 2 <= len(labels) <= 6:
            raise WorkflowAppError(f"weighted stack requires 2..6 captures; manifest lists {len(labels)}")
        downloads = episode_data.downloads().expanduser().resolve(strict=True)
        project = episode_data.path("stack_project").expanduser().resolve(strict=False)
        master = episode_data.path("stack_master").expanduser().resolve(strict=False)
        probe = Path(os.path.expandvars(os.path.expanduser(str(ld.probe())))).resolve(strict=False)
        completed_reference = _find_verified_completed_stack_stage(
            profile_path, instance_root_value, code, series, "stack-reference", project)
        completed_render = _find_verified_completed_stack_stage(
            profile_path, instance_root_value, code, series, "render-stack", master)
    except WorkflowAppError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PipelineError,
            RuntimeError, TypeError, ValueError) as exc:
        raise WorkflowAppError(f"cannot resolve stack-console inputs from the selected manifest/profile: {exc}") from exc
    finally:
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if not downloads.is_dir():
        raise WorkflowAppError(f"manifest download/capture folder is missing: {downloads}")
    reference = str(manifest_data.get("reference") or "").strip()
    alignment_map = str(manifest_data.get("alignment_map") or "").strip()
    completed_stage = completed_render or completed_reference
    stack_frames = 0
    if master.exists():
        if not completed_render:
            raise WorkflowAppError(
                f"a stack master exists but no fingerprint-verified completed render step owns it; "
                f"inspect before continuing: {master}")
        phase = "render-complete"
        measurement_requirements = []
    elif project.exists():
        if not completed_reference:
            raise WorkflowAppError(
                f"an Orc project exists but no fingerprint-verified completed reference-selection step owns it; "
                f"inspect before continuing: {project}")
        if not reference or reference not in labels:
            raise WorkflowAppError(
                f"completed reference-selection output exists, but manifest reference is incomplete: {project}")
        measurement_requirements = []
        if not alignment_map:
            measurement_requirements.append("record the measured alignment_map from the verified selection report")
        try:
            stack_frames = int((manifest_data.get("stack") or {}).get("frames"))
        except (TypeError, ValueError):
            stack_frames = 0
        if stack_frames <= 0:
            measurement_requirements.append("record the measured positive stack.frames from the refreshed Orc evidence")
        phase = "reference-complete" if measurement_requirements else "render"
    else:
        phase = "reference"
        measurement_requirements = []
    return {
        "episode": code,
        "series": series,
        "identity": identity,
        "title": str(manifest_data.get("title") or code),
        "profile": profile_path,
        "manifest": manifest_path,
        "instance_root": (Path(instance_root_value).expanduser().resolve(strict=False)
                          if instance_root_value else None),
        "downloads": downloads,
        "project": project,
        "master": master,
        "probe": probe,
        "capture_count": len(labels),
        "capture_labels": list(labels),
        "reference": reference,
        "alignment_map": alignment_map,
        "stack_frames": stack_frames,
        "phase": phase,
        "completed_stage": completed_stage,
        "reference_report": next((Path(path) for path in
                                   (completed_reference or {}).get("outputs", [])
                                   if str(path).casefold().endswith(".md") and Path(path).is_file()), None),
        "measurement_requirements": measurement_requirements,
        "fingerprint_policy": "balanced",
    }


def parse_verified_orc_reference_report(report_text: str, capture_labels: list[str],
                                        reference: str) -> dict[str, Any]:
    """Read the final, saved Orc alignment-map and its exhaustive coverage counts."""
    status = re.search(r"(?mi)^- Status:\s*\*\*([^*]+)\*\*\s*$", report_text)
    if not status or status.group(1).strip().casefold() != "selected":
        raise WorkflowAppError("the verified Orc report does not have SELECTED status")
    maps = re.findall(r"(?mi)^Measured alignment map:\s*`([^`]+)`\s*$", report_text)
    if not maps:
        raise WorkflowAppError("the verified Orc report has no measured alignment map")
    alignment_map = maps[-1].strip()
    map_parts = [part.strip() for part in alignment_map.split(",")]
    if len(map_parts) != len(capture_labels):
        raise WorkflowAppError("the Orc alignment map does not cover each refreshed source")
    for index, part in enumerate(map_parts, start=1):
        match = re.fullmatch(r"(\d+)\s*\+\s*(\d+)", part)
        if not match or int(match.group(1)) != index:
            raise WorkflowAppError("the Orc alignment map is malformed or out of project order")

    rows: list[list[str]] = []
    for line in report_text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().replace("`", "") for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 9 and cells[0].isdigit():
            rows.append(cells)
    if not rows:
        raise WorkflowAppError("the verified Orc report has no completed refresh-decision table")
    if len(maps) != len(rows):
        raise WorkflowAppError("the Orc report is missing a measured alignment map for a completed refresh pass")
    final = rows[-1]
    try:
        order = [label.strip() for label in final[2].split(",")]
        suggested = re.match(r"\s*([^\s(]+)\s*\(input\s+(\d+)\)", final[3])
        raw_coverage = [value.strip() for value in final[6].split("/")]
    except (IndexError, TypeError) as exc:
        raise WorkflowAppError("the final Orc report row lacks source/frame evidence") from exc
    if (len(order) != len(capture_labels) or set(order) != set(capture_labels) or
            not order or order[0].casefold() != reference.casefold()):
        raise WorkflowAppError("the final Orc report input order does not match the preserved project")
    if not suggested or suggested.group(1).casefold() != reference.casefold() or int(suggested.group(2)) != 1:
        raise WorkflowAppError("the final Orc refresh did not confirm its reference at input 1")
    if len(raw_coverage) != 3 or any(not value.isdigit() for value in raw_coverage):
        raise WorkflowAppError("the Orc report's 1/2/3+ source coverage counts are invalid")
    coverage = [int(value) for value in raw_coverage]
    frames = sum(coverage)
    if frames <= 0:
        raise WorkflowAppError("the Orc report's measured stack frame count is not positive")
    if final[8].casefold().replace(" ", "") not in ("true/true", "yes/yes", "1/1"):
        raise WorkflowAppError("the final Orc refresh row is not recorded as saved and completed")
    return {"alignment_map": alignment_map, "stack_frames": frames,
            "input_order": order, "coverage": coverage}


def build_orc_reference_measurement_plan(context: dict[str, Any],
                                          fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build a noninteractive plan to persist measurements from a verified Orc report."""
    if fingerprint_policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint_policy must be 'balanced' or 'full'")
    completed = context.get("completed_stage") or {}
    report = context.get("reference_report")
    if completed.get("stage") != "stack-reference" or not report:
        raise WorkflowAppError("a verified completed Orc reference report is required")
    report = Path(report).resolve(strict=True)
    manifest = Path(context["manifest"]).resolve(strict=False)
    downloads = Path(context["downloads"]).resolve(strict=False)
    project = Path(context["project"]).resolve(strict=False)
    outputs = {Path(value).resolve(strict=False) for value in completed.get("outputs", [])}
    if report not in outputs or project not in outputs:
        raise WorkflowAppError("the Orc report/project are not both verified outputs of the completed step")
    try:
        manifest_bytes = manifest.read_bytes()
        manifest_data = json.loads(manifest_bytes.decode("utf-8-sig"))
        report_text = report.read_text(encoding="utf-8-sig")
        measurements = parse_verified_orc_reference_report(
            report_text, list(context.get("capture_labels") or []),
            str(manifest_data.get("reference") or ""))
        current_map = str(manifest_data.get("alignment_map") or "").strip()
        current_frames = (manifest_data.get("stack") or {}).get("frames")
        if current_map and current_map != measurements["alignment_map"]:
            raise WorkflowAppError("manifest alignment_map conflicts with the verified Orc report")
        if current_frames not in (None, ""):
            current_frames = int(current_frames)
            if current_frames < 0 or (current_frames > 0 and
                                      current_frames != measurements["stack_frames"]):
                raise WorkflowAppError("manifest stack.frames conflicts with the verified Orc report")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    except WorkflowAppError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise WorkflowAppError(f"could not fingerprint the episode manifest: {exc}") from exc
    token = uuid.uuid4().hex[:12]
    identity = str(context["identity"])
    plan = {
        "schema": PLAN_SCHEMA,
        "job_id": f"record-stack-reference-{identity.lower().replace(':', '-')}-{token}",
        "episode": str(context["episode"]),
        "series": str(context["series"]).upper(),
        "profile": str(context["profile"]),
        "instance_root": str(context["instance_root"]) if context.get("instance_root") else None,
        "fingerprint_policy": fingerprint_policy,
        "decisions": [{
            "id": "verified-orc-report",
            "value": "Use the final saved/completed Orc refresh row; persist its measured alignment map and total frame coverage.",
        }],
        "steps": [{
            "id": "record-orc-reference-measurements",
            "stage": "record-orc-reference-measurements",
            "args": ["--report", str(report), "--expected-manifest-sha256", manifest_sha256],
            "inputs": [str(downloads), str(project), str(report)],
            "outputs": [str(manifest)],
        }],
    }
    return plan


def estimate_stack_render_gib(stack_frames: int) -> int:
    """Estimate SD ProRes HQ plus scratch from the measured output frame count."""
    if isinstance(stack_frames, bool) or not isinstance(stack_frames, int) or stack_frames <= 0:
        raise WorkflowAppError("automatic stack-space budgeting requires measured positive stack frames")
    gib = 1024 ** 3
    # The per-frame envelope includes headroom over the measured 720x486 ProRes-HQ class;
    # one extra GiB covers Orc temporary files. The normal free-space gate remains mandatory.
    return math.ceil(stack_frames * 320 * 1024 / gib + 1.0)


def _find_verified_completed_stack_stage(profile_path: Path, instance_root_value: str | Path | None,
                                          episode: str, series: str, stage_name: str,
                                          output_path: Path) -> dict[str, Any] | None:
    """Find a durable successful stack step whose declared output still verifies exactly."""
    if not instance_root_value:
        return None
    instance = Path(instance_root_value).expanduser().resolve(strict=False)
    logs_dir = instance / "logs"
    plan_dirs = [instance / "plans"]
    device_root = profile_path.parent.parent
    plan_dirs.append(device_root / "plans")
    if not logs_dir.is_dir() or logs_dir.is_symlink():
        return None
    profile_sha256 = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    plans_by_digest: dict[str, tuple[Path, dict[str, Any]]] = {}
    for directory in plan_dirs:
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                raw = path.read_bytes()
                plan = json.loads(raw.decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(plan, dict) and plan.get("schema") == PLAN_SCHEMA:
                plans_by_digest[hashlib.sha256(raw).hexdigest()] = (path, plan)
    if not plans_by_digest:
        return None

    from run_workflow import (_downstream_completed_output_ids, _verify_passed_step,
                              validate_resume_state)
    matches = []
    for state_path in logs_dir.glob("workflow-*.json"):
        if state_path.is_symlink() or not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (not isinstance(state, dict) or
                state.get("status") == "REJECTED" or
                str(state.get("episode", "")).upper() != episode.upper() or
                str(state.get("series", "DS9")).upper() != series.upper() or
                state.get("identity", f"DS9:{episode}") != f"{series}:{episode}" or
                state.get("profile_sha256") != profile_sha256):
            continue
        found = plans_by_digest.get(str(state.get("plan_sha256", "")))
        if not found:
            continue
        plan_path, plan = found
        validation_plan = dict(plan)
        if not validation_plan.get("job_id"):
            validation_plan["job_id"] = state.get("job_id")
        try:
            validate_resume_state(state, validation_plan,
                                  str(state.get("plan_sha256", "")), profile_sha256)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            continue
        for index, (saved, step) in enumerate(zip(state.get("steps", []), plan.get("steps", []))):
            if (not isinstance(saved, dict) or not isinstance(step, dict) or
                    step.get("kind", "stage") != "stage" or saved.get("status") != "PASS"):
                continue
            stage = str(step.get("stage", ""))
            outputs = [Path(value).expanduser().resolve(strict=False)
                       for value in step.get("outputs", []) if isinstance(value, str)]
            inputs = [Path(value).expanduser().resolve(strict=False)
                      for value in step.get("inputs", []) if isinstance(value, str)]
            reference_evidence_handoff = (
                stage_name == "stack-reference" and
                stage == "record-orc-reference-measurements" and
                output_path.resolve(strict=False) in inputs)
            if ((stage != stage_name and not reference_evidence_handoff) or
                    (stage == stage_name and output_path.resolve(strict=False) not in outputs)):
                continue
            try:
                _verify_passed_step(
                    saved, step, str(plan.get("fingerprint_policy", "balanced")),
                    superseded_path_ids=_downstream_completed_output_ids(plan, state, index))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                continue
            finished = str(saved.get("finished_utc", ""))
            verified_artifacts = outputs
            if reference_evidence_handoff:
                verified_artifacts = list(dict.fromkeys([*outputs, *inputs]))
            matches.append((finished, {
                "stage": stage_name, "step_id": step["id"],
                "job_id": str(state.get("job_id", state_path.stem)),
                "state": state, "state_path": state_path.resolve(strict=False),
                "plan": plan, "plan_path": plan_path.resolve(strict=False),
                "outputs": [str(path) for path in verified_artifacts],
                "finished_utc": finished,
            }))
    return max(matches, key=lambda item: item[0])[1] if matches else None


def build_stack_console_plan(context: dict[str, Any], *, planned_gib: float | None = None,
                             fingerprint_policy: str = "balanced",
                             restart_completed: bool = False) -> dict[str, Any]:
    """Build a bounded validate/reference-or-render plan with explicit review gates."""
    if fingerprint_policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint_policy must be 'balanced' or 'full'")
    phase = str(context.get("phase", ""))
    completed = context.get("completed_stage") or {}
    if phase == "reference-complete":
        if not restart_completed or completed.get("stage") != "stack-reference":
            raise WorkflowAppError("continue from the verified reference output; do not build another reference plan")
        phase = "reference"
    elif phase == "render-complete":
        if not restart_completed or completed.get("stage") != "render-stack":
            raise WorkflowAppError("the verified stack render is already complete")
        phase = "render"
    episode = str(context.get("episode", ""))
    series = str(context.get("series", "DS9")).upper()
    if series not in ("DS9", "VOY"):
        raise WorkflowAppError("stage console supports DS9 or VOY series")
    manifest = Path(context["manifest"]).resolve(strict=False)
    downloads = Path(context["downloads"]).resolve(strict=False)
    project = Path(context["project"]).resolve(strict=False)
    master = Path(context["master"]).resolve(strict=False)
    probe = Path(context["probe"]).resolve(strict=False)
    token = uuid.uuid4().hex[:12]
    if phase == "reference":
        report = probe / f"{episode}-reference-selection-{token}.md"
        overwrite_project = bool(restart_completed and completed.get("stage") == "stack-reference")
        if report.exists() or (project.exists() and not overwrite_project):
            raise WorkflowAppError("refusing to reuse an existing reference report or Orc project")
        select_args = ["--report-out", str(report)]
        if overwrite_project:
            select_args.extend(("--reselect", "--overwrite-project"))
        steps = [
            {"id": "validate-stack-inputs", "stage": "validate-inputs",
             "inputs": [str(downloads), str(manifest)], "outputs": []},
            {"id": "select-stack-reference", "stage": "stack-reference",
             "args": select_args, "inputs": [str(downloads)],
             "outputs": [str(manifest), str(project), str(report)]},
        ]
        decision = "reference-selection phase only; no stack master render is included"
        if overwrite_project:
            decision += "; operator chose restart and overwrite of the existing Orc project, with backup"
    elif phase == "render":
        try:
            estimate = (float(planned_gib) if planned_gib is not None else
                        float(estimate_stack_render_gib(int(context.get("stack_frames") or 0))))
        except (TypeError, ValueError) as exc:
            raise WorkflowAppError("stack render needs a measured positive frame count for automatic space budgeting") from exc
        if not math.isfinite(estimate) or estimate <= 0:
            raise WorkflowAppError("automatic stack-space estimate must be a positive finite GiB value")
        report = probe / f"{episode}-stack-render-{token}.json"
        overwrite_master = bool(restart_completed and completed.get("stage") == "render-stack")
        if report.exists() or (master.exists() and not overwrite_master):
            raise WorkflowAppError("refusing to reuse an existing stack master or render report")
        render_args = ["--planned-gib", f"{estimate:.6f}", "--report-out", str(report)]
        if overwrite_master:
            render_args.append("--overwrite-stack")
        steps = [
            {"id": "validate-stack-inputs", "stage": "validate-inputs",
             "inputs": [str(downloads), str(manifest)], "outputs": []},
            {"id": "render-stack", "stage": "render-stack",
             "args": render_args,
             "inputs": [str(downloads), str(manifest), str(project)],
             "outputs": [str(master), str(report)]},
            {"id": "review-stack-evidence", "kind": "review",
             "for_stage": "render-stack",
             "prompt": ("Inspect the stack master and render evidence report. Confirm expected frame count, "
                        "capture contributions, recovery/pairing evidence, and quality against the episode's "
                        "measured references. Approval is required before cadence work."),
             "inputs": [str(master), str(report), str(project), str(manifest)], "outputs": []},
        ]
        decision = (f"render phase; automatic peak-space budget={estimate:.3f} GiB "
                    f"from measured stack.frames={int(context.get('stack_frames') or 0)}")
        if overwrite_master:
            decision += "; operator chose restart and overwrite of the existing stack master, with backup"
    else:
        raise WorkflowAppError(f"unsupported stack-console phase: {phase!r}")

    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "job_id": (f"stack-{phase}-{episode.lower()}-{token}" if series == "DS9" else
                   f"stack-{phase}-{series.lower()}-{episode.lower()}-{token}"),
        "episode": episode,
        "series": series,
        "identity": f"{series}:{episode}",
        "episode_title": str(context.get("title") or episode),
        "profile": str(Path(context["profile"]).resolve(strict=False)),
        "manifest_path": str(Path(context["manifest"]).resolve(strict=False)),
        "fingerprint_policy": fingerprint_policy,
        "decisions": [
            {"id": "stage-console-phase", "value": phase},
            {"id": "operator-scope", "value": decision},
            {"id": "final-encode-remux", "value": "not included in this pilot"},
        ],
        "steps": steps,
    }
    instance_root = context.get("instance_root")
    if instance_root:
        plan["instance_root"] = str(Path(instance_root).resolve(strict=False))
    return plan


def inspect_retime_candidate_context(profile_value: str | Path, manifest_value: str | Path,
                                    episode: str, series: str,
                                    instance_root_value: str | Path | None = None) -> dict[str, Any]:
    """Resolve the V15 report-first inputs without running media or creating outputs."""
    code = str(episode).strip().upper()
    series = str(series or "DS9").strip().upper()
    if not EPISODE_RE.fullmatch(code) or series not in ("DS9", "VOY"):
        raise WorkflowAppError("retime candidate preparation needs a DS9/VOY SERIES:SxxExx identity")
    env_keys = ("LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY",
                "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR", "LDP_INSTANCE_MODE",
                "LDP_INSTANCE_LOGS", "LDP_CUT_PAD_IMAGES")
    prior_env = {key: os.environ.get(key) for key in env_keys}
    try:
        profile_path = Path(profile_value).expanduser().resolve(strict=True)
        raw_manifest_path = Path(manifest_value).expanduser()
        if raw_manifest_path.is_symlink():
            raise WorkflowAppError(f"manifest may not be a symlink: {raw_manifest_path}")
        manifest_path = raw_manifest_path.resolve(strict=True)
        os.environ["LDP_PROFILE"] = str(profile_path)
        os.environ["LDP_SERIES"] = series
        os.environ["LDP_EPISODE"] = code
        os.environ["LDP_IDENTITY"] = f"{series}:{code}"
        if instance_root_value:
            root = Path(instance_root_value).expanduser().resolve(strict=True)
            os.environ["LDP_INSTANCE_ROOT"] = str(root)
            os.environ["LDP_MANIFEST_DIR"] = str(root / "manifest")
        else:
            for key in env_keys[4:]:
                os.environ.pop(key, None)
        from ldcommon import (Ld, PipelineError, device_workspace_enabled,
                              episode_manifest_path, load_episode)
        ld = Ld(profile_path)
        if device_workspace_enabled(ld.profile) and not instance_root_value:
            raise WorkflowAppError("resolve the series-scoped workspace before preparing retime candidates")
        episode_data = load_episode(f"{series}:{code}", ld)
        expected_manifest = episode_manifest_path(code, ld, series=series).resolve(strict=False)
        if manifest_path != expected_manifest:
            raise WorkflowAppError(
                f"selected manifest does not match {series}:{code}: {manifest_path} != {expected_manifest}")
        raw_paths = {
            "master_descriptors": episode_data.path("master_descriptors"),
            "alignment": episode_data.path("align_npy"),
            "dvd": episode_data.path("dvd"),
            "stack_master": episode_data.path("stack_master"),
        }
        for role, path in raw_paths.items():
            if path.is_symlink():
                raise WorkflowAppError(f"retime candidate input {role} may not be a symlink: {path}")
        raw_probe = ld.probe()
        if raw_probe.is_symlink():
            raise WorkflowAppError(f"profile probe root may not be a symlink: {raw_probe}")
        paths = {
            "master_descriptors": raw_paths["master_descriptors"].resolve(strict=False),
            "alignment": raw_paths["alignment"].resolve(strict=False),
            "dvd": raw_paths["dvd"].resolve(strict=False),
            "stack_master": raw_paths["stack_master"].resolve(strict=False),
            "probe": raw_probe.resolve(strict=False),
            "manifest": manifest_path,
        }
        for role in ("master_descriptors", "alignment", "dvd", "stack_master"):
            path = paths[role]
            if path.is_symlink() or not path.is_file():
                raise WorkflowAppError(f"retime candidate input {role} is missing or unsafe: {path}")
        if paths["probe"].is_symlink() or not paths["probe"].is_dir():
            raise WorkflowAppError(f"profile probe root is missing or unsafe: {paths['probe']}")
        alignment_review = (episode_data.data.get("video") or {}).get("dvd_alignment_review") or {}
        raw_assets = str(alignment_review.get("review_assets_path") or "")
        review_assets = None
        if alignment_review.get("status") == "APPROVED" and not raw_assets:
            raise WorkflowAppError("approved alignment is missing its paired visual review assets")
        if raw_assets:
            asset_path = Path(raw_assets).expanduser()
            if not asset_path.is_absolute():
                parts = asset_path.parts
                if not parts or parts[0].casefold() != "probe" or ".." in parts:
                    raise WorkflowAppError("approved alignment review assets have an unsafe path")
                asset_path = paths["probe"].joinpath(*parts[1:])
            asset_path = asset_path.resolve(strict=False)
            if (asset_path.is_symlink() or not asset_path.is_file() or
                    hashlib.sha256(asset_path.read_bytes()).hexdigest() !=
                    alignment_review.get("review_assets_sha256")):
                raise WorkflowAppError("approved alignment review assets changed; rebuild/review the alignment")
            review_assets = asset_path
        return {
            "series": series,
            "episode": code,
            "identity": f"{series}:{code}",
            "title": episode_data.title,
            "manifest_data": episode_data.data,
            "alignment_review_assets": review_assets,
            "profile": profile_path,
            "manifest": manifest_path,
            "instance_root": (Path(instance_root_value).expanduser().resolve(strict=False)
                              if instance_root_value else None),
            **paths,
        }
    except WorkflowAppError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, TypeError, ValueError) as exc:
        raise WorkflowAppError(f"cannot resolve V15 retime-candidate inputs: {exc}") from exc
    finally:
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_retime_candidate_plan(context: dict[str, Any], parameters: dict[str, Any], *,
                                fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Create an immutable V15 preparation plan; it only generates reviewable reports."""
    from retime_break_candidates import CANDIDATE_SCHEMA

    series = str(context.get("series", "DS9")).upper()
    episode = str(context.get("episode", "")).upper()
    identity = f"{series}:{episode}"
    if series not in ("DS9", "VOY") or not EPISODE_RE.fullmatch(episode):
        raise WorkflowAppError("retime candidate plan needs a valid series-qualified episode")
    if fingerprint_policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint_policy must be balanced or full")

    def number(key: str, *, minimum: float | None = None, maximum: float | None = None,
               exclusive_minimum: bool = False) -> float:
        try:
            value = float(parameters[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowAppError(f"enter a numeric value for {key}") from exc
        if not math.isfinite(value):
            raise WorkflowAppError(f"{key} must be finite")
        if minimum is not None and (value <= minimum if exclusive_minimum else value < minimum):
            comparator = "greater than" if exclusive_minimum else "at least"
            raise WorkflowAppError(f"{key} must be {comparator} {minimum:g}")
        if maximum is not None and value > maximum:
            raise WorkflowAppError(f"{key} must be at most {maximum:g}")
        return value

    raw_thresholds = parameters.get("ld_thresholds", (5, 10, 15))
    try:
        thresholds = [float(value) for value in raw_thresholds]
    except (TypeError, ValueError) as exc:
        raise WorkflowAppError("LD threshold-sensitivity values must be numbers") from exc
    if (not thresholds or len(set(thresholds)) != len(thresholds) or
            any(not math.isfinite(value) or value <= 0 for value in thresholds)):
        raise WorkflowAppError("LD threshold-sensitivity values must be distinct finite positives")
    min_run = number("ld_min_duration_s", minimum=0.0)
    ld_threshold = number("ld_threshold", minimum=0.0, exclusive_minimum=True)
    if not any(math.isclose(ld_threshold, value, rel_tol=0.0, abs_tol=1e-9) for value in thresholds):
        raise WorkflowAppError("selected LD threshold must appear in the threshold-sensitivity report")
    dvd_noise = number("dvd_silence_noise_db", maximum=-0.001)
    dvd_min = number("dvd_silence_min_duration_s", minimum=0.0, exclusive_minimum=True)
    pair_tolerance = number("pair_tolerance_s", minimum=0.0, exclusive_minimum=True)
    max_spread = number("max_offset_spread_frames", minimum=0.0)
    selector = str(parameters.get("dvd_audio_selector", "")).strip()
    if selector and not re.fullmatch(r"0:a:\d+", selector):
        raise WorkflowAppError("DVD audio selector must have form 0:a:N")

    probe = Path(context["probe"]).resolve(strict=True)
    token = uuid.uuid4().hex[:12]
    ld_report = probe / f"{episode}-ld-black-intervals-{token}.json"
    candidate_report = probe / f"{episode}-retime-break-candidates-{token}.json"
    review_assets = probe / f"{episode}-retime-review-audio-{token}"
    if ld_report.exists() or candidate_report.exists() or review_assets.exists():
        raise WorkflowAppError("refusing to reuse an existing V15 retime candidate report path")
    measure_args = ["--thresholds", *[f"{value:g}" for value in thresholds],
                    "--min-duration", f"{min_run:g}", "--report-out", str(ld_report)]
    prepare_args = ["--ld-black-report", str(ld_report),
                    "--ld-threshold", f"{ld_threshold:g}",
                    "--dvd-silence-noise-db", f"{dvd_noise:g}",
                    "--dvd-silence-min-duration", f"{dvd_min:g}",
                    "--pair-tolerance-s", f"{pair_tolerance:g}",
                    "--max-offset-spread-frames", f"{max_spread:g}",
                    "--report-out", str(candidate_report)]
    if selector:
        prepare_args.extend(("--dvd-audio-selector", selector))
    manifest = Path(context["manifest"]).resolve(strict=True)
    descriptor = Path(context["master_descriptors"]).resolve(strict=True)
    alignment = Path(context["alignment"]).resolve(strict=True)
    dvd = Path(context["dvd"]).resolve(strict=True)
    master_source = Path(context["stack_master"]).resolve(strict=True)
    alignment_review_assets = context.get("alignment_review_assets")
    data = context.get("manifest_data")
    if not isinstance(data, dict):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"cannot verify approved alignment marker: {exc}") from exc
    review = (data.get("video") or {}).get("dvd_alignment_review") or {}
    if not alignment_review_assets:
        raise WorkflowAppError("retime candidate preparation needs the approved alignment's paired visual assets")
    alignment_review_assets = Path(alignment_review_assets).resolve(strict=True)
    if (hashlib.sha256(alignment_review_assets.read_bytes()).hexdigest() !=
            review.get("review_assets_sha256")):
        raise WorkflowAppError("paired visual review assets changed after alignment approval")
    raw_review_path = Path(str(review.get("path") or "")).expanduser()
    if not raw_review_path.is_absolute():
        if (not raw_review_path.parts or raw_review_path.parts[0].casefold() != "probe" or
                ".." in raw_review_path.parts):
            raise WorkflowAppError("retime candidate preparation requires a safe visual alignment approval")
        raw_review_path = probe / Path(*raw_review_path.parts[1:])
    if (review.get("status") != "APPROVED" or
            raw_review_path.resolve(strict=False) != alignment or
            hashlib.sha256(alignment.read_bytes()).hexdigest() != review.get("sha256") or
            hashlib.sha256(master_source.read_bytes()).hexdigest() != review.get("master_source_sha256") or
            hashlib.sha256(dvd.read_bytes()).hexdigest() != review.get("dvd_source_sha256")):
        raise WorkflowAppError(
            "retime candidate preparation requires a current, hash-bound visual DVD/LD alignment approval")
    steps = [
        {"id": "measure-ld-black-sensitivity", "stage": "measure-ld-black",
         "args": measure_args, "inputs": [str(manifest), str(descriptor)],
         "outputs": [str(ld_report)]},
        {"id": "prepare-retime-break-candidates", "stage": "prepare-retime-breaks",
          "args": prepare_args,
          "inputs": [str(manifest), str(ld_report), str(alignment), str(dvd)],
          "outputs": [str(candidate_report)]},
        {"id": "build-retime-audio-review-samples", "stage": "retime-review-assets",
         "args": ["--series", series, "--candidate-report", str(candidate_report),
                  "--out-dir", str(review_assets)],
         "inputs": [str(manifest), str(candidate_report), str(alignment), str(dvd)],
         "outputs": [str(review_assets)]},
        {"id": "review-retime-break-candidates", "kind": "review",
          "for_stage": "retime-review-assets",
          "prompt": (f"Review every candidate and pairing for {identity}. This is report-only: "
                     "confirm threshold sensitivity, alignment coverage, silence detection, "
                     "ambiguity/collisions, and unmatched candidates. This approval records no "
                     "manifest changes and chooses no offsets."),
          "inputs": [str(manifest), str(ld_report), str(candidate_report), str(review_assets),
                     *([str(alignment_review_assets)] if alignment_review_assets else [])],
          "outputs": []},
    ]
    decisions = [
        {"id": "retime-candidate-preparation", "value": "report-only; no manifest mutation"},
        {"id": "identity", "value": identity},
        {"id": "ld-threshold-selected-for-pairing", "value": f"{ld_threshold:g}"},
        {"id": "dvd-silence-noise-db", "value": f"{dvd_noise:g}"},
        {"id": "dvd-silence-min-duration-s", "value": f"{dvd_min:g}"},
        {"id": "pair-tolerance-s", "value": f"{pair_tolerance:g}"},
        {"id": "max-offset-spread-frames", "value": f"{max_spread:g}"},
    ]
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "job_id": f"retime-candidates-{series.lower()}-{episode.lower()}-{token}",
        "episode": episode, "series": series, "identity": identity,
        "episode_title": str(context.get("title") or episode),
        "profile": str(Path(context["profile"]).resolve(strict=False)),
        "manifest_path": str(Path(context["manifest"]).resolve(strict=False)),
        "fingerprint_policy": fingerprint_policy,
        "decisions": decisions,
        "steps": steps,
    }
    if context.get("instance_root"):
        plan["instance_root"] = str(Path(context["instance_root"]).resolve(strict=False))
    return plan


def build_retime_candidate_record_plan(context: dict[str, Any], report_path: str | Path,
                                       selected_labels: list[str], merged_labels: list[str],
                                       review_note: str, *,
                                       expected_report_sha256: str | None = None,
                                       fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build a separate, review-gated plan to apply only explicitly reviewed candidates."""
    from retime_break_candidates import CANDIDATE_SCHEMA, candidate_break_entries, sha256_file

    identity = str(context.get("identity", ""))
    if not identity or identity != f"{context.get('series')}:{context.get('episode')}":
        raise WorkflowAppError("candidate recorder context has no canonical series identity")
    if len(review_note.strip()) < 8:
        raise WorkflowAppError("recording candidates requires a human review note of at least 8 characters")
    if fingerprint_policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint_policy must be balanced or full")
    raw_candidate_path = Path(report_path).expanduser()
    if raw_candidate_path.is_symlink():
        raise WorkflowAppError(f"candidate report may not be a symlink: {raw_candidate_path}")
    path = raw_candidate_path.resolve(strict=True)
    if not path.is_file():
        raise WorkflowAppError(f"candidate report must be a regular non-symlink file: {path}")
    probe = Path(context["probe"]).resolve(strict=True)
    try:
        path.relative_to(probe)
    except ValueError as exc:
        raise WorkflowAppError("candidate report must reside in this episode's profile probe root") from exc
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cannot read candidate report: {exc}") from exc
    if (not isinstance(report, dict) or report.get("schema") != CANDIDATE_SCHEMA or
            report.get("status") != "REPORT_ONLY" or report.get("identity") != identity):
        raise WorkflowAppError(f"candidate report does not identify a V15 report for {identity}")
    digest = sha256_file(path)
    if expected_report_sha256 is not None and digest != expected_report_sha256:
        raise WorkflowAppError("candidate report changed after visual/audio review")
    try:
        candidate_break_entries(report, selected_labels, merged_labels,
                                reviewed_by=getpass.getuser(), review_note=review_note,
                                report_sha256=digest)
    except (ValueError, RuntimeError) as exc:
        raise WorkflowAppError(f"candidate selection is not safe to record: {exc}") from exc

    inputs = report.get("inputs") or {}
    relative = inputs.get("ld_black_report_relative_path")
    relative_posix = PurePosixPath(relative) if isinstance(relative, str) else None
    if (not relative or relative_posix is None or relative_posix.is_absolute() or
            not relative_posix.parts or ".." in relative_posix.parts or
            "\\" in relative or ":" in relative or relative_posix.as_posix() != relative):
        raise WorkflowAppError("candidate report has an unsafe LD-black report path")
    raw_ld_report = probe.joinpath(*relative_posix.parts)
    if raw_ld_report.is_symlink():
        raise WorkflowAppError("candidate report's LD-black source report may not be a symlink")
    ld_report = raw_ld_report.resolve(strict=True)
    if not ld_report.is_file():
        raise WorkflowAppError("candidate report's LD-black source report is missing or unsafe")
    manifest = Path(context["manifest"]).resolve(strict=True)
    if sha256_file(manifest) != report.get("manifest_sha256"):
        raise WorkflowAppError("manifest changed since candidate report generation; prepare a fresh report")
    alignment = Path(context["alignment"]).resolve(strict=True)
    dvd = Path(context["dvd"]).resolve(strict=True)
    selected_labels = list(selected_labels)
    merged_labels = list(merged_labels)
    verify_step = {"id": "validate-retime-candidate-inputs", "stage": "validate-inputs",
                   "inputs": [str(manifest), str(path)], "outputs": []}
    review_prompt = (
        f"Review the exact {identity} candidate report ({digest}). "
        f"Selected as unique cut records: {', '.join(selected_labels) or '(none)'}. "
        f"Selected as merged-through records: {', '.join(merged_labels) or '(none)'}. "
        f"Operator rationale: {review_note.strip()}. Approving permits only the V15 recorder "
        "to recheck report/input/manifest digests, create a verified backup, and atomically "
        "record intervals. It does not set offsets, steps, or gaps; those remain unmeasured."
    )
    review_step = {"id": "approve-retime-break-recording", "kind": "review",
                   "for_stage": "validate-inputs", "prompt": review_prompt,
                   "inputs": [str(manifest), str(path)], "outputs": []}
    args = ["--candidate-report", str(path), "--apply",
            "--confirm-review", identity,
            "--confirm-report-sha256", digest,
            "--review-note", review_note.strip()]
    for label in selected_labels:
        args.extend(("--select", label))
    for label in merged_labels:
        args.extend(("--merged-through", label))
    record_step = {"id": "record-reviewed-retime-break-candidates",
                   "stage": "record-retime-breaks", "args": args,
                   "inputs": [str(path), str(ld_report), str(alignment), str(dvd)],
                   "outputs": [str(manifest)]}
    token = uuid.uuid4().hex[:12]
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "job_id": f"record-retime-candidates-{identity.replace(':', '-').lower()}-{token}",
        "episode": context["episode"], "series": context["series"], "identity": identity,
        "episode_title": str(context.get("title") or context["episode"]),
        "profile": str(Path(context["profile"]).resolve(strict=False)),
        "manifest_path": str(manifest),
        "fingerprint_policy": fingerprint_policy,
        "decisions": [
            {"id": "retime-break-report-reviewed", "value": f"sha256:{digest}"},
            {"id": "unique-cut-candidates", "value": ",".join(selected_labels) or "none"},
            {"id": "merged-through-candidates", "value": ",".join(merged_labels) or "none"},
            {"id": "human-review-note", "value": review_note.strip()},
            {"id": "offsets-and-steps", "value": "remain null; separate adjudication required"},
        ],
        "steps": [verify_step, review_step, record_step],
    }
    if context.get("instance_root"):
        plan["instance_root"] = str(Path(context["instance_root"]).resolve(strict=False))
    return plan


def default_profile_path(*, system: str | None = None,
                         env: dict[str, str] | None = None,
                         home: Path | None = None) -> Path:
    """Compute the per-user profile path, preserving simulated foreign-OS spelling."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    system = (system or platform.system()).casefold()
    if system in ("win32",):
        system = "windows"
    elif system in ("darwin",):
        system = "macos"
    if system in ("windows", "win32"):
        root = Path(env.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    elif system in ("darwin", "macos"):
        root = Path(env.get("HOME") or home) / "Library" / "Application Support"
    else:
        root = Path(env.get("XDG_CONFIG_HOME") or (Path(env.get("HOME") or home) / ".config"))
    target = root / "ld-pipeline" / "profiles" / "machine.json"
    host = platform.system().casefold()
    host = "windows" if host == "windows" else "macos" if host == "darwin" else "linux"
    return target.resolve(strict=False) if system == host else target


def choose_existing_profile(default_path: Path, template: dict[str, Any], *,
                            hostname: str | None = None) -> Path:
    """Use the default profile, or a unique/host-matching legacy profile in its folder."""
    default_path = default_path.expanduser().resolve(strict=False)
    if default_path.is_file():
        return default_path
    operator = template.get("_operator_inputs_EDIT") or {}
    hint = operator.get("profile")
    hint_value = hint.get("value") if isinstance(hint, dict) else hint
    if isinstance(hint_value, str) and hint_value and "REPLACE_" not in hint_value:
        hinted = Path(os.path.expandvars(os.path.expanduser(hint_value)))
        if hinted.is_absolute() and hinted.is_file() and hinted.parent.resolve() == default_path.parent:
            return hinted.resolve()

    candidates = []
    for candidate in default_path.parent.glob("machine*.json"):
        if ".backup-" in candidate.name or not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        candidates.append((candidate.resolve(), str(data.get("machine", ""))))
    if len(candidates) == 1:
        return candidates[0][0]
    host = (hostname or socket.gethostname()).casefold()
    matching = [path for path, machine in candidates if machine.casefold() == host]
    if len(matching) == 1:
        return matching[0]
    if candidates:
        raise WorkflowAppError(
            f"multiple machine profiles exist in the default profile folder {default_path.parent}; "
            "rename the intended one to machine.json or correct its machine name")
    return default_path


def create_default_profile(default_path: Path, bundle_root: Path, *,
                           system: str | None = None,
                           hostname: str | None = None) -> tuple[Path, bool]:
    """Ensure a per-user profile exists at the selected default path, without overwriting."""
    path = default_path.expanduser().resolve(strict=False)
    if path.is_file():
        try:
            profile = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"existing machine profile is invalid and was not changed: {path}: {exc}") from exc
        if not isinstance(profile, dict):
            raise WorkflowAppError(f"existing machine profile must be a JSON object: {path}")
        return path, False
    return create_profile_from_template(path, bundle_root, system=system,
                                        hostname=hostname), True


def create_app_device_profile(bundle_root: Path, *, system: str | None = None,
                              hostname: str | None = None) -> tuple[Path, bool]:
    """Bootstrap this app's device folder, then create its device-bound profile there."""
    from shared_workspace import LockError, bootstrap_device_profile

    try:
        return bootstrap_device_profile(bundle_root, system=system, machine=hostname)
    except LockError as exc:
        raise WorkflowAppError(str(exc)) from exc


def create_profile_from_template(path: Path, bundle_root: Path, *,
                                 system: str | None = None,
                                 hostname: str | None = None) -> Path:
    """Create a new local profile once; never replace an existing profile."""
    path = path.expanduser().resolve(strict=False)
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"existing machine profile is invalid and was not changed: {path}: {exc}") from exc
        return path
    system = (system or platform.system()).casefold()
    target_os = "windows" if system in ("windows", "win32") else "darwin" if system in ("darwin", "macos") else "linux"
    template_name = {
        "windows": "machine.example.json",
        "darwin": "machine-macos.example.json",
        "linux": "machine-linux-x86_64.example.json",
    }[target_os]
    template_path = Path(bundle_root) / template_name
    try:
        profile = json.loads(template_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cannot read portable machine profile template {template_path}: {exc}") from exc
    host = hostname or socket.gethostname()
    profile["machine"] = host
    profile["os"] = target_os
    if (Path(bundle_root) / "devices").is_dir():
        profile["shared"] = {"enabled": True, "release_root": str(Path(bundle_root).resolve()),
                             "device_name": host}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        return path
    except OSError as exc:
        raise WorkflowAppError(f"could not create default machine profile {path}: {exc}") from exc
    return path


def archive_item_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in ("archive.org", "www.archive.org"):
        raise WorkflowAppError("Archive URL must use HTTPS on archive.org")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        raise WorkflowAppError("Archive URL must identify an item under /details/ or /download/")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", parts[1]):
        raise WorkflowAppError(f"unexpected Archive item identifier: {parts[1]!r}")
    return parts[1]


def episode_code_from_url_identifier(url: str) -> str | None:
    """Fast path: infer the episode from an Archive item ID without network access."""
    item = archive_item_id(url)
    found = {match.upper() for match in EPISODE_SEARCH_RE.findall(item)}
    if len(found) > 1:
        raise WorkflowAppError(f"Archive item identifier contains multiple episode codes: {sorted(found)}")
    return next(iter(found), None)


def episode_code_in_archive_url(url: str) -> str | None:
    """Return an unambiguous SxxExx code embedded in the Archive item identifier."""
    return episode_code_from_url_identifier(url)


def archive_series_from_url_identifier(url: str) -> str | None:
    """Return a show prefix from the Archive item ID, if one is present."""
    item = archive_item_id(url)
    match = re.match(r"^(DS9|VOY|TNG)-", item, re.IGNORECASE)
    return match.group(1).upper() if match else None


def resolve_episode_code(url: str, metadata_reader=None) -> tuple[str, str]:
    """Resolve SxxExx from the item identifier, then Archive metadata/file names."""
    item = archive_item_id(url)
    direct = episode_code_in_archive_url(url)
    if direct:
        return direct, "Archive item identifier"
    if metadata_reader is None:
        def metadata_reader(item_id: str) -> dict[str, Any]:
            request = Request(f"https://archive.org/metadata/{item_id}",
                              headers={"User-Agent": "FromStunToKill-LaserDisc-Workflow"})
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
    try:
        metadata = metadata_reader(item)
    except Exception as exc:
        raise WorkflowAppError(f"could not resolve episode code from Archive metadata: {exc}") from exc
    info = metadata.get("metadata") if isinstance(metadata, dict) else None
    if not isinstance(info, dict):
        raise WorkflowAppError("Archive metadata response has no metadata object")
    texts = [str(info.get(key, "")) for key in ("identifier", "title", "description")]
    files = metadata.get("files") or []
    if isinstance(files, list):
        texts.extend(str(row.get("name", "")) for row in files if isinstance(row, dict))
    found = {match.upper() for text in texts for match in EPISODE_SEARCH_RE.findall(text)}
    if len(found) != 1:
        detail = "no episode code was found" if not found else f"multiple episode codes were found: {sorted(found)}"
        raise WorkflowAppError(f"cannot uniquely determine episode code from Archive item {item!r}: {detail}")
    return next(iter(found)), "Archive metadata"


def _title_from_archive_metadata(info: dict[str, Any], files: list[Any],
                                 episode: str, item: str) -> tuple[str, str]:
    """Prefer Archive TBC filenames; use a non-slug Archive title as fallback."""
    file_titles: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", ""))
        if Path(name).suffix.casefold() != ".tbc":
            continue
        stem = Path(name).stem
        capture = re.search(r"[-_](?:[LMP]\d+[A-Z]?)(?:[-_]BAD)?$", stem, re.IGNORECASE)
        episode_end = re.search(re.escape(episode) + r"[-_]", stem, re.IGNORECASE)
        if not capture or not episode_end or episode_end.end() >= capture.start():
            continue
        title = stem[episode_end.end():capture.start()].strip(" -_")
        if title:
            file_titles.add(title)
    if len(file_titles) == 1:
        return next(iter(file_titles)), "Archive capture filename"
    if len(file_titles) > 1:
        raise WorkflowAppError(
            f"Archive capture filenames disagree about the episode title: {sorted(file_titles)}")

    raw_title = str(info.get("title", "")).strip()
    if not raw_title or raw_title.casefold() == item.casefold():
        return "", ""
    candidate = raw_title
    after_code = re.search(re.escape(episode) + r"[\s:_-]+(.+)$", candidate, re.IGNORECASE)
    if after_code:
        candidate = after_code.group(1)
    candidate = re.sub(
        r"(?i)(?:[-_\s]+(?:laser\s*disc|laserdisc|copies|captures?|tbc|efm).*)$", "", candidate)
    candidate = re.sub(r"(?i)^(?:(?:star\s+trek\s*:?\s*)?(?:deep\s+space\s+nine|ds9)[\s:_-]*)+", "", candidate)
    candidate = re.sub(re.escape(episode), "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"[\s._-]+", " ", candidate).strip()
    if candidate.casefold() in {"", "laserdisc", "laser disc", "copies", "capture", "captures"}:
        return "", ""
    return candidate, "Archive metadata title"


def resolve_series_episode_identity(url: str, metadata_reader=None,
                                    requested_series: str | None = None) -> tuple[str, str, str, str]:
    """Resolve series, episode code, and title; reject conflicting Archive/request identities."""
    from archive_catalog import identify_series

    item = archive_item_id(url)
    if metadata_reader is None:
        def metadata_reader(item_id: str) -> dict[str, Any]:
            request = Request(f"https://archive.org/metadata/{item_id}",
                              headers={"User-Agent": "FromStunToKill-LaserDisc-Workflow"})
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
    try:
        metadata = metadata_reader(item)
    except Exception as exc:
        raise WorkflowAppError(f"could not resolve Archive episode title/metadata: {exc}") from exc
    info = metadata.get("metadata") if isinstance(metadata, dict) else None
    if not isinstance(info, dict):
        raise WorkflowAppError("Archive metadata response has no metadata object")
    files = metadata.get("files") or []
    file_names = [str(row.get("name", "")) for row in files if isinstance(row, dict)] if isinstance(files, list) else []
    texts = [item, str(info.get("identifier", "")), str(info.get("title", "")),
             str(info.get("description", ""))]
    texts.extend(file_names)
    try:
        identified = {identify_series(text) for text in texts}
    except ValueError as exc:
        raise WorkflowAppError(f"could not resolve Archive series: {exc}") from exc
    identified.discard(None)
    if len(identified) > 1:
        raise WorkflowAppError(f"Archive metadata contains multiple series identifiers: {sorted(identified)}")
    archive_series = next(iter(identified), None)
    requested = str(requested_series or "").strip().upper() or None
    if requested and requested not in ("DS9", "VOY"):
        raise WorkflowAppError("series must be DS9 or VOY for single-episode workflow intake")
    if archive_series == "TNG":
        raise WorkflowAppError("TNG combined/extra items are not supported for single-episode workflow intake")
    if archive_series and requested and archive_series != requested:
        raise WorkflowAppError(
            f"Archive item identifies {archive_series}, but the selected series is {requested}")
    series = archive_series or requested or "DS9"
    direct = episode_code_in_archive_url(url)
    if direct:
        episode = direct
        source = "Archive item identifier"
    else:
        texts = [str(info.get(key, "")) for key in ("identifier", "title", "description")]
        texts.extend(file_names)
        found = {match.upper() for text in texts for match in EPISODE_SEARCH_RE.findall(text)}
        if len(found) != 1:
            detail = "no episode code was found" if not found else f"multiple episode codes were found: {sorted(found)}"
            raise WorkflowAppError(f"cannot uniquely determine episode code from Archive item {item!r}: {detail}")
        episode = next(iter(found))
        source = "Archive metadata"
    if series == "DS9" and episode in ("S01E01", "S01E02"):
        raise WorkflowAppError("Emissary is a two-part special case; use its dedicated Part I/II workflow")
    title, title_source = _title_from_archive_metadata(info, files, episode, item)
    if not title:
        raise WorkflowAppError(
            f"Archive item {item!r} identifies {episode}, but does not provide a human episode title "
            "in its metadata or TBC filenames; choose local intake or correct the Archive item metadata")
    return series, episode, title, f"{source}; title from {title_source}"


def resolve_episode_identity(url: str, metadata_reader=None) -> tuple[str, str, str]:
    """Backward-compatible series-agnostic wrapper for existing app integrations."""
    _series, episode, title, source = resolve_series_episode_identity(url, metadata_reader)
    return episode, title, source


def resolve_local_source(path_value: str | Path) -> dict[str, Any]:
    """Inspect a local rip without requiring an Archive download manifest."""
    from local_source import LocalSourceError, inspect_local_source
    try:
        return inspect_local_source(path_value)
    except LocalSourceError as exc:
        raise WorkflowAppError(str(exc)) from exc


def read_intake_template(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cannot read intake plan/template {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != PLAN_SCHEMA:
        raise WorkflowAppError(f"template schema must be {PLAN_SCHEMA!r}")
    if not isinstance(data.get("_operator_inputs_EDIT"), dict):
        raise WorkflowAppError("intake template needs an _operator_inputs_EDIT object")
    return data


def _absolute(value: str, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowAppError(f"{field} is required")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise WorkflowAppError(f"{field} must be an absolute path")
    if path.is_symlink():
        raise WorkflowAppError(f"{field} must not be a symbolic link")
    return path.resolve(strict=False)


def _set_decision(decisions: list[dict[str, Any]], decision_id: str, value: str) -> None:
    for decision in decisions:
        if decision.get("id") == decision_id:
            decision["value"] = value
            return
    decisions.append({"id": decision_id, "value": value})


def build_intake_plan(template: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Build a new no-overwrite intake plan; never mutate the loaded template."""
    plan = copy.deepcopy(template)
    operator_inputs = plan.get("_operator_inputs_EDIT")
    if not isinstance(operator_inputs, dict):
        raise WorkflowAppError("intake template needs an _operator_inputs_EDIT object")

    source_mode = str(values.get("source_mode", "archive-url")).strip().casefold()
    if source_mode not in ("archive-url", "local"):
        raise WorkflowAppError("source_mode must be 'archive-url' or 'local'")
    selected_series = str(values.get("series", "DS9")).strip().upper() or "DS9"
    if selected_series not in ("DS9", "VOY"):
        raise WorkflowAppError("series must be DS9 or VOY for single-episode workflow intake")
    requested_dvd_mode = str(values.get("dvd_mode", "")).strip().casefold()
    required = ["profile", "dvd_mode", "manifest_path"]
    if requested_dvd_mode == "dvd-backed":
        required.append("dvd_path")
    elif requested_dvd_mode == "no-dvd":
        required.append("cut_pad_images")
    if source_mode == "archive-url":
        required.extend(("episode", "archive_url", "downloads_dir"))
    else:
        required.append("local_source_path")
    missing = [key for key in required if not str(values.get(key, "")).strip()]
    if missing:
        raise WorkflowAppError("complete these fields: " + ", ".join(missing))

    local_info = resolve_local_source(values["local_source_path"]) if source_mode == "local" else None
    if (local_info and local_info.get("series_inferred") and
            local_info.get("series") != selected_series):
        raise WorkflowAppError(
            f"local capture names identify series {local_info['series']}, not selected series {selected_series}")
    episode = (local_info["episode"] if local_info else values["episode"].strip().upper())
    series = selected_series
    if not EPISODE_RE.fullmatch(episode):
        raise WorkflowAppError("episode must use the SxxExx format")
    if local_info and values.get("episode") and values["episode"].strip().upper() != episode:
        raise WorkflowAppError("local source ArchiveId episode does not match the detected episode")
    supplied_title = str(values.get("confirmed_title", "")).strip()
    title = supplied_title or f"Title not supplied ({series}:{episode})"
    archive_url = values["archive_url"].strip() if not local_info else "not-applicable"
    if not local_info:
        parsed_url = urlparse(archive_url)
        if (parsed_url.scheme != "https" or parsed_url.hostname not in ("archive.org", "www.archive.org") or
                not parsed_url.path.startswith(("/details/", "/download/"))):
            raise WorkflowAppError("archive URL must be an HTTPS archive.org/details/<item> URL")
        item = archive_item_id(archive_url)
        if item.upper().startswith("TNG-"):
            raise WorkflowAppError("TNG combined/extra items are not supported for single-episode workflow intake")
        item_series = re.match(r"^(DS9|VOY)-", item, re.IGNORECASE)
        if item_series and item_series.group(1).upper() != series:
            raise WorkflowAppError(
                f"Archive item identifier identifies {item_series.group(1).upper()}, not selected series {series}")
        item_episode = episode_code_from_url_identifier(archive_url)
        if item_episode and item_episode != episode:
            raise WorkflowAppError(
                f"Archive item identifier identifies {item_episode}, not selected episode {episode}")

    profile_path = _absolute(values["profile"], "profile")
    downloads = (Path(local_info["source_dir"]).resolve() if local_info else
                 _absolute(values["downloads_dir"], "downloads_dir"))
    try:
        profile_data = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cannot read selected machine profile: {exc}") from exc
    # Archive downloads are the one user-selected input allowed outside this
    # device's normal roots. The absolute location is stored in the device-local
    # episode manifest; all manifests, plans, logs, work, and outputs stay in the
    # configured device/episode workspace.
    shared = profile_data.get("shared") or {}
    if shared.get("enabled") is True and not local_info and shared.get("release_root"):
        release_root = Path(os.path.expandvars(
            os.path.expanduser(str(shared["release_root"])))).resolve(strict=False)
        try:
            downloads.resolve(strict=False).relative_to(release_root)
        except ValueError:
            pass
        else:
            configured = (profile_data.get("roots") or {}).get("downloads_root")
            if not configured:
                raise WorkflowAppError(
                    "a download selected inside the shared release must be under this device's roots.downloads_root")
            configured_root = Path(os.path.expandvars(
                os.path.expanduser(str(configured)))).resolve(strict=False)
            try:
                downloads.resolve(strict=False).relative_to(configured_root)
            except ValueError as exc:
                raise WorkflowAppError(
                    "downloads inside the shared release must remain under this device's configured downloads root; "
                    "choose an external folder for other locations") from exc
    manifest = _absolute(values["manifest_path"], "manifest_path")
    if manifest.name.casefold() != f"{episode}.json".casefold():
        raise WorkflowAppError(f"manifest_path filename must be {episode}.json")
    try:
        manifest.relative_to(downloads)
    except ValueError:
        pass
    else:
        raise WorkflowAppError("manifest_path must not be inside downloads_dir")
    if manifest.exists():
        raise WorkflowAppError(f"refusing to target an existing manifest: {manifest}")

    dvd_mode = values["dvd_mode"].strip().casefold()
    if dvd_mode not in ("dvd-backed", "no-dvd"):
        raise WorkflowAppError("dvd_mode must be 'dvd-backed' or 'no-dvd'")
    cut_pad_choice = str(values.get("cut_pad_images", "")).strip().casefold()
    dvd_path_value = str(values.get("dvd_path", "")).strip()
    if dvd_mode == "dvd-backed":
        dvd_path = _absolute(dvd_path_value, "dvd_path")
        if not dvd_path.is_file():
            raise WorkflowAppError(f"selected DVD file does not exist: {dvd_path}")
        cut_pad_choice = "not-applicable"
        dvd_path_value = str(dvd_path)
    else:
        if cut_pad_choice not in ("use", "skip"):
            raise WorkflowAppError("no-DVD mode requires cut_pad_images to be 'use' or 'skip'")
        dvd_path_value = "not-applicable"

    policy = values.get("fingerprint_policy", plan.get("fingerprint_policy", "balanced"))
    if policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint_policy must be 'balanced' or 'full'")

    editable_values = {
        "episode": episode,
        "series": series,
        "profile": str(profile_path),
        "archive_url": archive_url,
        "downloads_dir": str(downloads),
        "source_mode": source_mode,
        "local_source_path": local_info["selected_path"] if local_info else "not-applicable",
        "fingerprint_policy": policy,
        "confirmed_title": title,
        "dvd_mode": dvd_mode,
        "cut_pad_images": cut_pad_choice,
        "manifest_path": str(manifest),
        "dvd_path": dvd_path_value,
    }
    intake_record = {}
    for key, value in editable_values.items():
        previous = operator_inputs.get(key)
        comment = previous.get("_comment", "Set in the local workflow app.") if isinstance(previous, dict) else "Set in the local workflow app."
        operator_inputs[key] = {"value": value, "_comment": comment}
        intake_record[key] = {"value": value, "_comment": comment}

    plan["series"] = series
    plan["identity"] = "${input:series}:${input:episode}"
    plan["fingerprint_policy"] = policy
    instance_root_value = str(values.get("instance_root", "")).strip()
    if instance_root_value:
        plan["instance_root"] = str(_absolute(instance_root_value, "instance_root"))
    else:
        plan.pop("instance_root", None)
    decisions = plan.get("decisions")
    if not isinstance(decisions, list):
        raise WorkflowAppError("template decisions must be an array")
    _set_decision(decisions, "dvd-mode", dvd_mode)
    _set_decision(decisions, "cut-pad-images", cut_pad_choice)
    _set_decision(decisions, "source-mode", source_mode)
    if supplied_title:
        _set_decision(decisions, "episode-title-confirmed", supplied_title)
        decisions[:] = [row for row in decisions if row.get("id") != "episode-title-status"]
    else:
        decisions[:] = [row for row in decisions if row.get("id") != "episode-title-confirmed"]
        _set_decision(decisions, "episode-title-status",
                      f"not supplied; manifest placeholder: {title}")
    if local_info:
        _set_decision(decisions, "archive-item-confirmed", "not supplied; local inventory provenance")
        _set_decision(decisions, "local-source", local_info["selected_path"])
    else:
        _set_decision(decisions, "archive-item-confirmed", archive_url)
        decisions[:] = [row for row in decisions if row.get("id") != "local-source"]
    if dvd_mode == "dvd-backed":
        _set_decision(decisions, "dvd-reference", "${input:dvd_path}")
    else:
        decisions[:] = [row for row in decisions if row.get("id") != "dvd-reference"]

    steps = plan.get("steps")
    if not isinstance(steps, list) or [step.get("stage") for step in steps] != [
            "download", "init-episode", "validate-inputs"]:
        raise WorkflowAppError("GUI intake currently supports download/init/validate templates")
    download_step, init_step, validate_step = steps
    init_args = (["--local-source", "${input:local_source_path}",
                  "--series", "${input:series}",
                  "--fingerprint-policy", "${input:fingerprint_policy}",
                  "--confirmed-title", "${input:confirmed_title}"] if local_info else
                 ["--url", "${input:archive_url}", "--downloads", "${input:downloads_dir}",
                  "--series", "${input:series}"])
    init_inputs = ["${input:downloads_dir}"]
    validate_inputs = ["${input:downloads_dir}", "${input:manifest_path}"]
    if local_info:
        for evidence in (local_info["source_dir"], local_info["selected_path"]):
            if Path(evidence).resolve() != downloads.resolve():
                init_inputs.append(evidence)
                validate_inputs.append(evidence)
    if dvd_mode == "dvd-backed":
        init_args.extend(("--dvd", "${input:dvd_path}"))
        init_inputs.append("${input:dvd_path}")
        validate_inputs.append("${input:dvd_path}")
    else:
        init_args.extend(("--no-dvd", "--cut-pad-images", "${input:cut_pad_images}"))
    init_args.extend(("--manifest-out", "${input:manifest_path}"))

    if local_info:
        init_step["id"] = "initialize-local-episode"
        init_step["stage"] = "init-local-episode"
        init_step["outputs"] = ["${input:manifest_path}"]
        plan["steps"] = [init_step, validate_step]
    else:
        download_step["args"] = ["-Url", "${input:archive_url}", "-Destination", "${input:downloads_dir}"]
        download_step["inputs"] = []
        download_step["outputs"] = ["${input:downloads_dir}"]
    init_step["args"] = init_args
    init_step["inputs"] = init_inputs
    if not local_info:
        init_step["outputs"] = ["${input:manifest_path}"]
    validate_step["inputs"] = validate_inputs
    validate_step["outputs"] = []
    if local_info:
        plan.setdefault("_instructions_DO_NOT_EDIT", []).append(
            "Local intake skips download and creates a clearly labeled local inventory. Archive origin/authenticity "
            "is not asserted; inspect the selected capture set and the inventory evidence before proceeding.")
    if not supplied_title:
        plan.setdefault("_instructions_DO_NOT_EDIT", []).append(
            f"Episode title was not supplied. The manifest contains the explicit placeholder {title!r}; "
            "confirm and replace it from evidence before downstream episode processing.")
    for instruction_index, instruction in enumerate(plan.get("_instructions_DO_NOT_EDIT", [])):
        if not isinstance(instruction, str):
            continue
        if "This is an S01E07, no-DVD example." in instruction:
            plan["_instructions_DO_NOT_EDIT"][instruction_index] = (
                "This plan was generated by the desktop app; review its concrete series, episode, "
                "title, source mode, DVD choice, and exact selected paths before execution.")
        elif "no-dvd intake example" in instruction.casefold():
            if local_info:
                plan["_instructions_DO_NOT_EDIT"][instruction_index] = (
                    "This plan was generated from a local source selection. Confirm its inferred/selected "
                    "series and episode identity and preserve the local-only provenance record.")
            elif dvd_mode == "dvd-backed":
                plan["_instructions_DO_NOT_EDIT"][instruction_index] = (
                    "This plan was generated by the desktop app in DVD-backed mode. Confirm the exact selected "
                    "DVD file matches the Archive item and episode identity before execution.")
    for instruction_index, instruction in enumerate(plan.get("_instructions_DO_NOT_EDIT", [])):
        if isinstance(instruction, str) and "initialize command is hardcoded to --no-dvd" in instruction:
            plan["_instructions_DO_NOT_EDIT"][instruction_index] = (
                "The workflow app generates an initialize command that matches the selected DVD mode; "
                "review the generated plan before execution.")
    # V15's built-in example-plan expander intentionally enforces its no-DVD-only
    # template. The GUI supports both DVD modes, so save a fully concrete app plan
    # with its choices recorded separately rather than routing it through that
    # template-specific operator-input policy.
    plan.pop("_operator_inputs_EDIT", None)
    plan["_fromstuntokill_intake"] = intake_record
    input_ref = re.compile(r"\$\{input:([A-Za-z0-9_]+)\}")

    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            def replace(match: re.Match[str]) -> str:
                key = match.group(1)
                if key not in editable_values:
                    raise WorkflowAppError(f"plan references unknown GUI intake value {key!r}")
                return str(editable_values[key])
            result = input_ref.sub(replace, value)
            if "${input:" in result:
                raise WorkflowAppError(f"generated app plan has an unresolved input reference: {result}")
            return result
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    return {key: (value if key in ("_instructions_DO_NOT_EDIT", "_fromstuntokill_intake")
                  else substitute(value)) for key, value in plan.items()}


def write_new_plan(path: Path, plan: dict[str, Any]) -> Path:
    target = path.expanduser().resolve(strict=False)
    if target.exists():
        raise WorkflowAppError(f"refusing to overwrite existing plan: {target}")
    if not target.parent.is_dir():
        raise WorkflowAppError(f"plan destination folder does not exist: {target.parent}")
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise WorkflowAppError(f"cannot save workflow plan {target}: {exc}") from exc
    return target
