"""Orc (Decode-Orc ``orc-cli``) adapter and report interpretation.

The parsing/validation functions are ported verbatim from v38 ``render_stack.py`` and
``select_stack_reference.py`` (validated on real S01E05/E07/E18/E20 runs) with
``PipelineError`` → ``ArtifactError``. Rules they encode:

* Orc's exit code is not trusted. ``tools refresh`` can exit non-zero while its JSON says
  ``completed: true, saved: true``; a render must show one ``Loaded`` line per capture,
  StackerStage multi-input evidence, zero unrecoverable samples, and a written-frame count
  equal to both the measured stack frames and an independent FFprobe decode count.
* Orc's own ``suggestedReference`` selects the stack reference; no custom score is used.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from json import JSONDecoder
from pathlib import Path

from fstk.core.errors import ArtifactError as PipelineError  # v38 name kept in ported bodies
from fstk.core.errors import ToolError
from fstk.tools import process

LOADED_RE = re.compile(r"\btbc_source:\s*Loaded\b", re.IGNORECASE)
WRITTEN_RE = re.compile(r"Successfully\s+wrote\s+([\d,]+)\s+frames", re.IGNORECASE)
UNRECOVERABLE_RE = re.compile(r"unrecoverable(?:\s+samples?)?\D{0,20}([\d,]+)", re.IGNORECASE)
PAIR_NUMBER = r"\d+(?:,\d{3})*"
PAIR_RE = re.compile(rf"(?:\bpairs?\s*[:=]\s*({PAIR_NUMBER})|({PAIR_NUMBER})\s+pairs?\b)", re.IGNORECASE)
MIN_STACK_CAPTURES = 2


def expected_pair_count(input_count: int) -> int:
    """Orc reports all source pairs; pressing groups are checked separately."""
    return input_count * (input_count - 1) // 2


def expected_pressing_groups(labels: list[str], groups) -> list[tuple[int, ...]] | None:
    """Map explicit manifest capture groups to 1-based Orc input indices."""
    if not isinstance(groups, list) or not groups:
        return None
    index = {label: position + 1 for position, label in enumerate(labels)}
    expected = []
    seen = []
    for group in groups:
        if isinstance(group, dict):
            members = group.get("value") or group.get("captures") or group.get("labels")
        else:
            members = group
        if not isinstance(members, list) or not all(isinstance(x, str) for x in members):
            return None
        if any(member not in index for member in members):
            return None
        seen.extend(members)
        expected.append(tuple(sorted(index[member] for member in members)))
    if len(seen) != len(set(seen)) or set(seen) != set(labels):
        return None
    return sorted(expected)


def project_source_labels(project: Path) -> list[str]:
    text = project.read_text(encoding="utf-8", errors="replace")
    labels = []
    for block in re.split(r"(?m)^    - id: ", text)[1:]:
        stage = re.search(r"(?m)^      stage: (\S+)\s*$", block)
        label = re.search(r"(?m)^      user_label: TBC Source (.+?)\s*$", block)
        if stage and stage.group(1) == "tbc_source" and label:
            labels.append(label.group(1))
    return labels


def stacker_reports(lines: list[str]) -> list[dict]:
    decoder = json.JSONDecoder()
    reports = []
    for line in lines:
        if "StackerStage" not in line:
            continue
        for match in re.finditer(r"\{", line):
            try:
                value, _ = decoder.raw_decode(line, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("inputs"), list):
                reports.append(value)
                break
    return reports


def reported_pressing_groups(text: str) -> list[tuple[int, ...]] | None:
    pattern = re.compile(r"inputs grouped by pressing[^:]*:\s*((?:\{\s*\d+(?:\s*,\s*\d+)*\s*\}\s*)+)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    groups = [tuple(sorted(int(value) for value in re.findall(r"\d+", item)))
              for item in re.findall(r"\{[^}]+\}", match.group(1))]
    return sorted(groups)


def validate_render_evidence(text: str, labels: list[str], expected_frames: int,
                             actual_frames: int, groups) -> dict:
    loaded_lines = [line for line in text.splitlines() if LOADED_RE.search(line)]
    loaded_counts = {label: sum(label.casefold() in line.casefold() for line in loaded_lines)
                     for label in labels}
    written = [int(value.replace(",", "")) for value in WRITTEN_RE.findall(text)]
    unrecoverable = [int(value.replace(",", "")) for value in UNRECOVERABLE_RE.findall(text)]
    lines = text.splitlines()
    stacker_lines = [line for line in lines if "StackerStage" in line]
    reports = stacker_reports(lines)
    input_counts = [len(report["inputs"]) for report in reports]
    pair_counts = [int(report["pairs"]) for report in reports
                   if isinstance(report.get("pairs"), (int, float))]
    if not pair_counts:
        pair_counts = [int((left or right).replace(",", "")) for line in stacker_lines
                       for left, right in PAIR_RE.findall(line)]
    wanted_pairs = expected_pair_count(len(labels))
    expected_groups = expected_pressing_groups(labels, groups)
    reported_groups = reported_pressing_groups(text)
    issues = []
    review_flags = []
    if len(loaded_lines) != len(labels) or any(count != 1 for count in loaded_counts.values()):
        issues.append(f"expected one Loaded line per capture; got {len(loaded_lines)} lines, labels={loaded_counts}")
    if not stacker_lines:
        issues.append("no StackerStage multi-input evidence was found")
    if not reports:
        issues.append("no structured StackerStage input-count report was found")
    elif any(count != len(labels) for count in input_counts):
        issues.append(f"StackerStage input counts {input_counts} differ from {len(labels)} captures")
    if not unrecoverable:
        review_flags.append("render log did not report an unrecoverable-sample count; verify in Orc output")
    elif any(count != 0 for count in unrecoverable):
        issues.append(f"unrecoverable samples were reported: {unrecoverable}")
    if not written:
        issues.append("render log did not contain 'Successfully wrote N frames'")
    elif any(count != expected_frames for count in written):
        issues.append(f"rendered frame count(s) {written} differ from measured stack.frames={expected_frames}")
    if actual_frames != expected_frames:
        issues.append(f"FFprobe frame count {actual_frames} differs from measured stack.frames={expected_frames}")
    if written and any(count != actual_frames for count in written):
        issues.append(f"Orc written-frame count {written} differs from FFprobe frame count {actual_frames}")
    pair_check = "not-reported"
    if pair_counts:
        if any(count != wanted_pairs for count in pair_counts):
            pair_check = f"fail: reported={pair_counts}, expected={wanted_pairs}"
            issues.append(f"Orc pair count(s) {pair_counts} differ from {len(labels)}-capture all-pairs expectation {wanted_pairs}")
        else:
            pair_check = f"pass: {wanted_pairs} all-pairs"
    else:
        review_flags.append("StackerStage pair count was not reported; verify actual capture-pair evidence")
    if expected_groups is None:
        review_flags.append("manifest pressing-group membership is unavailable or incomplete")
        group_check = "review-required"
    elif reported_groups is None:
        review_flags.append("Orc log did not report pressing groups; verify against manifest")
        group_check = "not-reported"
    elif reported_groups != expected_groups:
        issues.append(f"Orc pressing groups {reported_groups} differ from manifest groups {expected_groups}")
        group_check = "fail"
    else:
        group_check = "pass"
    return {
        "loaded_capture_lines": len(loaded_lines),
        "loaded_per_capture": loaded_counts,
        "stacker_stage_lines": len(stacker_lines),
        "stacker_input_counts": input_counts,
        "orc_written_frames": written,
        "ffprobe_frames": actual_frames,
        "expected_frames": expected_frames,
        "unrecoverable_counts": unrecoverable,
        "pair_count_check": pair_check,
        "pressing_group_check": group_check,
        "issues": issues,
        "review_flags": review_flags,
        "ok": not issues,
    }


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


# --------------------------------------------------------------------------- invocation
@dataclass
class RefreshResult:
    report: dict
    process: process.ProcessResult


class Orc:
    """Thin wrapper around ``orc-cli``. Every call is followed by artifact validation."""

    def __init__(self, exe: str | Path, *, log_dir: Path | None = None):
        self.exe = str(exe)
        self.log_dir = log_dir

    def _log(self, name: str) -> Path | None:
        return self.log_dir / f"{name}.log" if self.log_dir else None

    def version(self) -> str:
        """``orc-cli --version`` (first non-empty line), or "unknown". Recorded in EFM provenance:
        the report parser fails closed, so newer Orc versions are allowed and the version is
        evidence (owner, 2026-11-21: best output, not a pin)."""
        try:
            res = process.run([self.exe, "--version"], capture_stdout=True, log_path=self._log("orc-version"))
        except (OSError, ToolError):        # the version is evidence, never a gate
            return "unknown"
        text = (res.stdout or b"").decode("utf-8", errors="replace")
        m = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
        return m.group(1) if res.returncode == 0 and m else "unknown"

    def identity(self) -> dict:
        """Version (if Orc reports one) and the executable's SHA-256, which identifies the build exactly."""
        from fstk.core.hashing import sha256_file

        exe = Path(self.exe)
        digest = sha256_file(exe) if exe.is_file() else None
        return {"version": self.version(), "exe": exe.name, "exe_sha256": digest}

    def refresh(self, project: Path, *, log_name: str = "orc-refresh", cancel=None) -> RefreshResult:
        """Refresh analysis/maps only (never ``--process``); parse the JSON from mixed output."""
        texts: list[str] = []
        res = process.run([self.exe, "tools", "refresh", str(project), "--json"], capture_stdout=True,
                          log_path=self._log(log_name), cancel=cancel,
                          on_line=lambda s, line: texts.append(line) if s != "stdout" else None)
        stdout = (res.stdout or b"").decode("utf-8", errors="replace")
        report = parse_refresh_report([stdout, "\n".join(texts)])
        return RefreshResult(report, res)

    def process_project(self, project: Path, *, log_name: str = "orc-render", cancel=None,
                        on_line=None) -> tuple[process.ProcessResult, str]:
        """Render a project. Returns the process result and the full combined output text."""
        lines: list[str] = []

        def collect(stream: str, line: str) -> None:
            lines.append(line)
            if on_line:
                on_line(stream, line)

        res = process.run([self.exe, str(project), "--process"], log_path=self._log(log_name),
                          on_line=collect, cancel=cancel)
        return res, "\n".join(lines)

    def decode_efm(self, project: Path, orc_log: Path, *, log_name: str = "orc-efm", cancel=None,
                   on_line=None) -> process.ProcessResult:
        """Run a two-node EFMSink project, asking Orc for its own log file (v38 command line).

        The returned exit status is informational only: callers must validate the Orc log's
        path bindings, the WAV format and the decoding report (``fstk.tools.orc_efm``).
        """
        return process.run([self.exe, str(project), "--process", "--log-file", str(orc_log),
                            "--log-out", "both"], log_path=self._log(log_name), on_line=on_line,
                           cancel=cancel)
