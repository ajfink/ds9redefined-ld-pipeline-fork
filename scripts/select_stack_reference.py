#!/usr/bin/env python3
"""Use Orc's own refresh recommendation to select a weighted-stack reference.

The first declared capture is only a scratch sample-grid seed when the manifest
reference is blank. Orc refreshes that scratch project, reports its suggested
reference, and the helper repeats with the suggested capture first until Orc's
recommendation is stable (bounded to three refreshes). It then saves the selected
reference and refreshed project. It never renders the stack master or invents a
quality score.

  python scripts\\select_stack_reference.py --episode S01E10
  python scripts\\select_stack_reference.py --episode S01E10 --dry-run
  python scripts\\select_stack_reference.py --episode S01E05 --reselect
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import (BUNDLE, Ld, PipelineError, episode_manifest_path,
                      load_episode, redact_shared_text)  # noqa: E402

MIN_CAPTURES = 2
MAX_CAPTURES = 6
MAX_REFERENCE_PASSES = 3


def parse_refresh_report(texts: list[str]) -> dict:
    """Find Orc's JSON refresh object in output that may also contain log text."""
    decoder = JSONDecoder()
    for text in texts:
        for match in re.finditer(r"\{", text):
            try:
                report, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if (isinstance(report, dict)
                    and {"nodes", "completed", "saved"}.issubset(report)):
                return report
    raise PipelineError("Orc refresh output did not contain a parseable JSON report")


def source_alignment_decision(report: dict, ordered_labels: list[str]) -> dict:
    """Validate Orc's recommendation and map its 1-based input index to a label."""
    if report.get("completed") is not True or report.get("saved") is not True:
        raise PipelineError(
            "Orc refresh did not report completed=true and saved=true; "
            "reference selection is not committed"
        )
    nodes = report.get("nodes")
    if not isinstance(nodes, list):
        raise PipelineError("Orc refresh JSON has no nodes array")
    matches = [node for node in nodes
               if node.get("stage") == "source_align"
               and node.get("tool") == "source_alignment"]
    if len(matches) != 1:
        raise PipelineError(
            f"expected one source_alignment result, found {len(matches)}"
        )
    node = matches[0]
    if node.get("status") != "success":
        raise PipelineError(
            f"Orc source alignment status is {node.get('status')!r}, not success"
        )
    stats = node.get("statistics") or {}
    try:
        source_count = int(stats["sourceCount"])
        suggested_index = int(stats["suggestedReference"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(
            "Orc source-alignment report lacks sourceCount/suggestedReference"
        ) from exc
    if source_count != len(ordered_labels):
        raise PipelineError(
            f"Orc analyzed {source_count} sources, expected {len(ordered_labels)}"
        )
    if not 1 <= suggested_index <= source_count:
        raise PipelineError(
            f"Orc suggestedReference={suggested_index} is outside 1..{source_count}"
        )

    # The JSON report carries the source labels as an independent check that the
    # recommendation index is being interpreted in the generated project's order.
    source_rows = node.get("sources")
    if isinstance(source_rows, list) and source_rows:
        reported_labels = []
        for row in source_rows:
            label = str(row.get("label", ""))
            prefix = "TBC Source "
            reported_labels.append(label[len(prefix):] if label.startswith(prefix) else label)
        if len(reported_labels) == len(ordered_labels) and reported_labels != ordered_labels:
            raise PipelineError(
                f"Orc source order {reported_labels} does not match project order "
                f"{ordered_labels}"
            )

    runner_up = stats.get("referenceRunnerUp")
    try:
        runner_up_index = int(runner_up) if runner_up is not None else None
    except (TypeError, ValueError):
        runner_up_index = None
    runner_up_label = None
    if runner_up_index is not None and 1 <= runner_up_index <= source_count:
        runner_up_label = ordered_labels[runner_up_index - 1]

    return {
        "status": node["status"],
        "source_count": source_count,
        "suggested_index": suggested_index,
        "suggested_label": ordered_labels[suggested_index - 1],
        "runner_up_index": runner_up_index,
        "runner_up_label": runner_up_label,
        "stack_sigma": stats.get("stackSigma"),
        "equal_sigma": stats.get("equalSigma"),
        "best_single_sigma": stats.get("bestSingleSigma"),
        "fit_trusted": stats.get("fitTrusted"),
        "fell_back_to_mean": stats.get("fellBackToMean"),
        "pressing_groups": stats.get("pressingGroups"),
        "worst_misfit": stats.get("worstMisfit"),
        "frames_with_1_source": stats.get("framesWith1Source"),
        "frames_with_2_sources": stats.get("framesWith2Sources"),
        "frames_with_3plus_sources": stats.get("framesWith3PlusSources"),
        "alignment_map": (node.get("parameter_changes") or {}).get("alignmentMap"),
    }


def _tail(text: str, count: int = 32) -> str:
    return "\n".join(text.splitlines()[-count:])


def _run_and_log(command: list[str], log_path: Path) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            cwd=str(BUNDLE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise PipelineError(f"could not run {command[0]}: {exc}") from exc
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_text = (
        "Command: " + subprocess.list2cmdline(command) + "\n"
        + f"Exit code: {result.returncode}\n\n"
        + "--- stdout ---\n" + result.stdout
        + "\n--- stderr ---\n" + result.stderr
    )
    ld = Ld()
    log_path.write_text(redact_shared_text(log_text, ld), encoding="utf-8")
    return result


def _build_scratch_project(
    episode: str,
    reference: str,
    project_path: Path,
    sink_path: Path,
    builder_log: Path,
) -> None:
    command = [
        sys.executable,
        str(BUNDLE / "scripts" / "build_stack_project.py"),
        "--episode", episode,
        "--reference-override", reference,
        "--project-out", str(project_path),
        "--master-out", str(sink_path),
    ]
    result = _run_and_log(command, builder_log)
    if result.returncode != 0 or not project_path.is_file():
        raise PipelineError(
            "could not build Orc reference-probe project; see "
            f"{builder_log}\n{_tail(result.stdout + chr(10) + result.stderr)}"
        )


def _run_orc_refresh(orc: Path, project_path: Path, log_path: Path) -> tuple[dict, int]:
    # Intentionally omit --process: this refreshes analysis/maps, never renders video.
    command = [str(orc), "tools", "refresh", str(project_path), "--json"]
    result = _run_and_log(command, log_path)
    report = parse_refresh_report([
        result.stdout,
        result.stderr,
        result.stdout + "\n" + result.stderr,
        result.stderr + "\n" + result.stdout,
    ])
    # Orc can return non-zero even when the saved report says the refresh completed.
    return report, result.returncode


def _report_summary(
    episode: str,
    run_id: str,
    original_reference: str | None,
    decisions: list[dict],
    final_project: Path | None,
    manifest_path: Path,
    status: str,
    error: str | None = None,
) -> str:
    lines = [
        f"# Automatic Orc reference selection — {episode}",
        "",
        f"- Run ID: `{run_id}`",
        f"- Status: **{status}**",
        f"- Manifest reference before selection: `{original_reference or '(unset)'}`",
        f"- Manifest: `{manifest_path}`",
    ]
    if final_project:
        lines.append(f"- Refreshed project: `{final_project}`")
    if error:
        lines.extend(["", "## Stop reason", "", error])
    if decisions:
        lines.extend([
            "",
            "## Orc refresh decisions",
            "",
            "| Pass | Sources | Input order | Suggested reference | Runner-up | Stack sigma | Coverage 1/2/3+ | Fit trusted | Saved/completed |",
            "|---:|---:|---|---|---|---:|---|---|---|",
        ])
        for decision in decisions:
            lines.append(
                "| {pass_no} | {source_count} | `{order}` | `{suggested}` (input {index}) | `{runner}` | "
                "{sigma} | {coverage} | {fit} | {saved}/{completed} |".format(
                    pass_no=decision["pass"],
                    source_count=decision["source_count"],
                    order=", ".join(decision["order"]),
                    suggested=decision["suggested_label"],
                    index=decision["suggested_index"],
                    runner=decision["runner_up_label"] or "not reported",
                    sigma=(decision["stack_sigma"] if decision["stack_sigma"] is not None
                           else "not reported"),
                    coverage="{}/{}/{}".format(
                        decision["frames_with_1_source"] if decision["frames_with_1_source"] is not None else "?",
                        decision["frames_with_2_sources"] if decision["frames_with_2_sources"] is not None else "?",
                        decision["frames_with_3plus_sources"] if decision["frames_with_3plus_sources"] is not None else "?",
                    ),
                    fit=(decision["fit_trusted"] if decision["fit_trusted"] is not None
                         else "not reported"),
                    saved=decision["saved"],
                    completed=decision["completed"],
                )
            )
            if decision.get("alignment_map"):
                lines.append(f"\nMeasured alignment map: `{decision['alignment_map']}`")
            lines.append(f"\nRaw Orc refresh log: `{decision['log_path']}`")
    lines.extend([
        "",
        "Orc's `suggestedReference` is the selector; no noise-only custom score is used.",
        "This helper does not render the master. Continue through the existing stack-quality gate.",
    ])
    return "\n".join(lines) + "\n"


def _commit_manifest_reference(manifest_path: Path, original_sha: str,
                               reference: str, run_id: str) -> None:
    current_bytes = manifest_path.read_bytes()
    if hashlib.sha256(current_bytes).hexdigest() != original_sha:
        raise PipelineError(
            f"episode manifest changed during Orc refresh; not overwriting {manifest_path}"
        )
    data = json.loads(current_bytes.decode("utf-8"))
    data["reference"] = reference
    temp_path = manifest_path.with_name(f".{manifest_path.name}.autoref-{run_id}.tmp")
    if temp_path.exists():
        raise PipelineError(f"refusing to overwrite temporary manifest {temp_path}")
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != original_sha:
            raise PipelineError(
                f"episode manifest changed before commit; not overwriting {manifest_path}"
            )
        os.replace(temp_path, manifest_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--reselect", action="store_true",
                    help="allow Orc to replace an already declared manifest reference")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the refresh loop and report the choice without committing project/manifest")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("two-part Emissary needs one project per side; this selector handles one-part episodes only")
    captures = ep.capture_set
    if not MIN_CAPTURES <= len(captures) <= MAX_CAPTURES:
        raise PipelineError(f"weighted stack needs {MIN_CAPTURES}..{MAX_CAPTURES} captures; found {len(captures)}")
    if len(captures) != len(set(captures)):
        raise PipelineError(f"capture labels must be unique: {captures}")

    manifest_path = episode_manifest_path(ep.code, ld)
    original_bytes = manifest_path.read_bytes()
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    original_data = json.loads(original_bytes.decode("utf-8"))
    original_reference = original_data.get("reference") or None
    if original_reference and original_reference not in captures:
        raise PipelineError(f"manifest reference {original_reference!r} is not in captures {captures}")
    final_project = ep.path("stack_project")
    if not args.dry_run and final_project.exists():
        raise PipelineError(
            f"refusing to overwrite existing Orc project {final_project}; "
            "finish or archive that run before selecting a new reference"
        )

    # Do not spend time mapping/choosing references until the downloaded inputs pass.
    validator = BUNDLE / "scripts" / "validate_episode.py"
    validation = subprocess.run(
        [sys.executable, str(validator), "--episode", ep.code, "--stage", "inputs"],
        cwd=str(BUNDLE), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if validation.returncode != 0:
        raise PipelineError(
            "input validation must pass before Orc reference selection:\n"
            + _tail(validation.stdout + "\n" + validation.stderr)
        )

    probe_root = ld.root("probe")
    log_root = ld.root("logs")
    probe_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    orc = ld.tool("orc")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    decisions: list[dict] = []
    seen: list[str] = []
    current_seed = original_reference or captures[0]
    selected: str | None = None
    stable_project: Path | None = None
    final_report_path = log_root / f"{ep.code}-reference-selection-{run_id}.md"

    try:
        for pass_no in range(1, MAX_REFERENCE_PASSES + 1):
            if current_seed in seen:
                raise PipelineError(
                    "Orc recommendation cycled through " + ", ".join(seen)
                )
            seen.append(current_seed)
            order = [current_seed] + [label for label in captures if label != current_seed]
            project = probe_root / f"{ep.code}-reference-{run_id}-pass{pass_no}.orcprj"
            # Refresh does not process the sink; keep the canonical master path in
            # the project so the stable, promoted project is ready for the render step.
            sink = ep.path("stack_master")
            builder_log = log_root / f"{ep.code}-reference-{run_id}-build{pass_no}.log"
            _build_scratch_project(ep.code, current_seed, project, sink, builder_log)

            refresh_log = log_root / f"{ep.code}-reference-{run_id}-orc{pass_no}.log"
            report, exit_code = _run_orc_refresh(orc, project, refresh_log)
            decision = source_alignment_decision(report, order)
            decision.update({
                "pass": pass_no,
                "order": order,
                "log_path": str(refresh_log),
                "completed": report.get("completed"),
                "saved": report.get("saved"),
                "exit_code": exit_code,
            })
            decisions.append(decision)
            if exit_code != 0:
                print(f"WARN: Orc returned exit {exit_code}, but completed/saved artifacts were verified")
            if not project.is_file() or project.stat().st_size == 0:
                raise PipelineError(f"Orc did not save the scratch project {project}")

            recommended = decision["suggested_label"]
            if original_reference and not args.reselect and recommended != original_reference:
                raise PipelineError(
                    f"Orc recommends {recommended!r}, but the manifest already declares "
                    f"{original_reference!r}; rerun with --reselect only if you intend to replace it"
                )
            if recommended == current_seed:
                selected, stable_project = current_seed, project
                break
            current_seed = recommended

        if selected is None or stable_project is None:
            raise PipelineError(
                f"Orc's recommendation did not stabilize within {MAX_REFERENCE_PASSES} full refreshes"
            )

        status = "DRY RUN — not committed" if args.dry_run else "SELECTED"
        if not args.dry_run:
            if final_project.exists():
                raise PipelineError(f"refusing to overwrite existing Orc project {final_project}")
            final_project.parent.mkdir(parents=True, exist_ok=True)
            with final_project.open("xb") as destination, stable_project.open("rb") as source:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            if hashlib.sha256(final_project.read_bytes()).digest() != hashlib.sha256(stable_project.read_bytes()).digest():
                raise PipelineError(f"promoted Orc project copy failed hash verification: {final_project}")
            try:
                if selected != original_reference:
                    _commit_manifest_reference(manifest_path, original_sha, selected, run_id)
            except Exception:
                if (final_project.is_file()
                        and hashlib.sha256(final_project.read_bytes()).digest()
                        == hashlib.sha256(stable_project.read_bytes()).digest()):
                    final_project.unlink()
                raise

        summary = _report_summary(ep.code, run_id, original_reference, decisions,
                                  stable_project if args.dry_run else final_project,
                                  manifest_path, status)
        final_report_path.write_text(redact_shared_text(summary, ld), encoding="utf-8")
        final_decision = decisions[-1]
        print(f"Orc-selected reference: {selected}")
        print(f"stable input order: {', '.join(final_decision['order'])}")
        print(f"refreshed project: {stable_project if args.dry_run else final_project}")
        print(f"selection report: {final_report_path}")
        print("No master render was run; continue with the existing stack-quality gate.")
        return 0
    except PipelineError as exc:
        summary = _report_summary(ep.code, run_id, original_reference, decisions,
                                  stable_project, manifest_path, "STOP", str(exc))
        final_report_path.write_text(redact_shared_text(summary, ld), encoding="utf-8")
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"selection report: {final_report_path}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
