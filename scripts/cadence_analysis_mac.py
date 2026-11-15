#!/usr/bin/env python3
"""Run the measured PowerShell cadence analysis on macOS and save review evidence.

The Windows-authoritative ``build_map.ps1`` parser is reused on macOS through
PowerShell 7. Its temporary AviSynth text is not executed. The measured deinterlace
map/report are saved for a human review step. Only after that review should a
separate ``cadence-vpy`` stage emit the VapourSynth chain and choose a repair path.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dependency_lock import redact_shared_text  # noqa: E402
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def resolve_pwsh(ld: Ld) -> str:
    configured = str((ld.profile.get("tools") or {}).get("pwsh") or "").strip()
    if configured:
        path = Path(os.path.expandvars(os.path.expanduser(configured)))
        if path.is_file():
            return str(path)
        raise PipelineError(f"tools.pwsh points to a missing executable: {path}")
    found = shutil.which("pwsh")
    if found:
        return found
    raise PipelineError("PowerShell 7 (pwsh) is required for the shared cadence-analysis stage on macOS")


def run_logged(command: list[str]) -> tuple[int, str]:
    print("COMMAND: " + subprocess.list2cmdline(command), flush=True)
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        raise PipelineError(f"could not start cadence tool {command[0]}: {exc}") from exc
    assert proc.stdout is not None
    output = []
    for line in proc.stdout:
        output.append(line)
        print(line, end="", flush=True)
    proc.stdout.close()
    return proc.wait(), "".join(output)


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--report-out", required=True, help="new JSON report path for cadence evidence")
    args = parser.parse_args()

    ld = Ld()
    if str(ld.profile.get("os", "")).casefold() not in ("darwin", "macos"):
        raise PipelineError("cadence_analysis_mac.py is only for the macOS VapourSynth path")
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("generic cadence analysis does not implement two-part episodes")
    tfm_log = ep.path("tfm_log")
    if not tfm_log.is_file():
        raise PipelineError(f"full-side TFM log missing: {tfm_log}; run the tfm-log stage first")
    ps = resolve_pwsh(ld)
    report_path = Path(args.report_out).expanduser().resolve()
    assert_writable_path(report_path, ld)
    if report_path.exists():
        raise PipelineError(f"refusing to overwrite cadence report: {report_path}")

    started = datetime.now(timezone.utc).isoformat()
    analysis_output = []
    with tempfile.TemporaryDirectory(prefix=f"ld-cadence-{ep.code}-") as temporary:
        scratch_avs = Path(temporary) / f"{ep.code}-cadence-analysis-only.avs"
        analysis_cmd = [ps, "-NoProfile", "-File",
                        str(Path(__file__).with_name("build_map.ps1")),
                        "-Episode", ep.code, "-MapOut", str(scratch_avs)]
        code, text = run_logged(analysis_cmd)
        analysis_output.append(text)
        if code != 0:
            raise PipelineError(f"build_map.ps1 cadence analysis failed (exit {code}); preserve its report/log artifacts")
        if not scratch_avs.is_file():
            raise PipelineError("cadence analysis did not produce its temporary AVS artifact")

    di_map = ep.path("di_map")
    summary = ep.path("cadence_summary")
    if not di_map.is_file() or not summary.is_file():
        raise PipelineError(f"cadence analysis omitted expected artifacts: di_map={di_map.is_file()} summary={summary.is_file()}")
    map_text = di_map.read_text(encoding="utf-8", errors="replace")
    ranges = map_text.count("[")
    summary_text = summary.read_text(encoding="utf-8", errors="replace")
    flags = []
    folded = summary_text.casefold()
    if "need inspection before encoding" in folded or "<== check" in folded:
        flags.append("cadence report flags interval(s) for inspection")
    if "motion trace not supplied" in folded:
        flags.append("native-30p motion gate did not run; inspect the cadence verdict")
    if "<<< inspect" in folded:
        flags.append("long combed/hard-spot interval(s) require review")

    report = {
        "schema": "ld-pipeline-cadence-analysis-v1",
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code,
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "tfm_log": str(tfm_log),
        "deinterlace_map": str(di_map),
        "cadence_summary": str(summary),
        "repair_ranges": ranges,
        "review_flags": flags,
        "human_review_required": True,
        "analysis_output": "\n".join(analysis_output)[-8000:],
    }
    report_text = redact_shared_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", ld)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(report_text)
    print(f"CADENCE ANALYSIS COMPLETE: report={report_path}; di-map={di_map}; human review required before building the VapourSynth chain", flush=True)
    for flag in flags:
        print("REVIEW FLAG: " + flag, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"CADENCE ANALYSIS ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
