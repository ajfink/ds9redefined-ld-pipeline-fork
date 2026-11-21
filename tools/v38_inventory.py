#!/usr/bin/env python3
"""Generate docs/v38-inventory.md: every v38 source file → its new home and port status.

Usage: python tools/v38_inventory.py <path-to-v38-tree> [--check]

``--check`` fails if a v38 file is missing from MAP (so nothing is forgotten) or MAP names
a file that no longer exists in the reference tree.

Status values:
  ported   – functionality exists in fstk with tests (behaviour may be improved)
  partial  – some of the file's responsibilities are ported
  planned  – not started; target module named
  retired  – intentionally not carried over (reason given)
"""
from __future__ import annotations

import sys
from pathlib import Path

# v38 path -> (target, status, note)
MAP: dict[str, tuple[str, str, str]] = {
    # ---- shared foundations
    "ldcommon.py": ("fstk.core.identity, fstk.config.profile, fstk.tools.ffmpeg.select_chunks",
                    "partial", "Ld/Episode replaced by Profile + Manifest; series is a registry"),
    "scripts/fileutil.py": ("fstk.core.hashing", "ported", ""),
    "scripts/manifest_io.py": ("fstk.core.fsio, fstk.manifest.store", "ported",
                               ".bak copies replaced by journal.jsonl"),
    "scripts/numeric.py": ("fstk.media.numeric", "planned", ""),
    "scripts/streamutil.py": ("fstk.tools.process", "ported", ""),
    "scripts/stage_clock.py": ("fstk.workflow.clock", "planned", ""),
    "scripts/space_ledger.py": ("fstk.workspace.space", "ported", ""),
    "scripts/check_space_budget.py": ("fstk.workspace.space", "ported", "space gate as a library call"),
    "scripts/efm_paths.py": ("fstk.stages.efm (paths)", "planned", ""),
    "scripts/local_source.py": ("fstk.workspace.local_source", "ported", ""),
    "scripts/vspipe_util.py": ("fstk.tools.vapoursynth", "partial", ""),
    "scripts/dependency_lock.py": ("fstk.config.dependencies", "planned", ""),
    "scripts/dependency_updates.py": ("fstk.config.dependencies", "planned", ""),
    "scripts/dependency_update_window.py": ("fstk.ui (dependency page)", "planned", ""),
    "scripts/bootstrap_deps.py": ("pyproject extras + fstk doctor --install", "planned", ""),
    "scripts/doctor.py": ("fstk.cli doctor / fstk.tools.doctor", "partial", ""),
    "scripts/preflight_mac.py": ("fstk.tools.doctor", "planned", "one cross-platform doctor"),
    "preflight.ps1": ("fstk.tools.doctor", "retired", "PowerShell retired; doctor is Python"),
    "scripts/read_log.py": ("fstk.core.fsio.decode_text + fstk.cli logs", "partial", ""),
    "scripts/shared_workspace.py": ("fstk.workspace.shared", "planned", ""),
    "scripts/backup_device_workspace.py": ("fstk.workspace.backup", "planned", ""),
    "scripts/post_run_cleanup.py": ("fstk.workspace.cleanup", "planned", "never deletes without approval"),
    "scripts/run_tests.py": ("pytest", "retired", "pytest discovers tests directly"),
    # ---- intake / sources
    "scripts/init_episode.py": ("fstk.workspace.episodes + capture-inventory", "partial", "DVD title/cut-pad choices pending"),
    "scripts/init_local_episode.py": ("fstk.workspace.episodes + capture-inventory", "ported", ""),
    "scripts/download_archive.ps1": ("fstk.tools.archive_http + stages.intake (archive-download)", "ported", "Python; incremental MD5 on resume"),
    "scripts/resolve_archive_episode.py": ("fstk catalog search / fstk.catalog.index", "ported", ""),
    "scripts/archive_catalog.py": ("fstk.catalog.archive", "ported", ""),
    "scripts/build_archive_catalog.py": ("fstk.stages.intake.catalog", "planned", ""),
    "scripts/dvd_prep.py": ("fstk.stages.dvd (dvd-probe)", "partial", ""),
    "scripts/validate_episode.py": ("fstk.stages.gates", "planned", "gates become stage prerequisites"),
    # ---- stack (Orc)
    "scripts/build_stack_project.py": ("fstk.tools.orc_project.build_stack_project", "ported", ""),
    "scripts/select_stack_reference.py": ("fstk.stages.stack (stack-reference)", "ported", ""),
    "scripts/record_orc_reference_measurements.py": ("fstk.stages.stack (engine commit)", "ported", ""),
    "scripts/render_stack.py": ("fstk.stages.stack (stack-render)", "ported", ""),
    # ---- EFM audio
    "scripts/efm_decode.py": ("fstk.stages.efm.decode", "planned", ""),
    "scripts/decode_efm_captures.py": ("fstk.stages.efm.decode", "planned", ""),
    "scripts/efm_provisional.py": ("fstk.stages.efm.provisional", "planned", ""),
    "scripts/bind_efm_provisional.py": ("fstk.stages.efm.provisional", "planned", ""),
    "scripts/verify_efm_capture.py": ("fstk.stages.efm.verify", "planned", ""),
    "scripts/efm_report_table.py": ("fstk.media.efm_report", "planned", ""),
    "scripts/record_efm_capture_selection.py": ("fstk.stages.efm.select", "planned", ""),
    # ---- DVD alignment / descriptors
    "scripts/extract_video_descriptors.py": ("fstk.media.descriptors + fstk.stages.alignment", "planned", ""),
    "scripts/measure_dvd_alignment.py": ("fstk.media.alignment", "planned", ""),
    "scripts/build_alignment_review_assets.py": ("fstk.evidence.alignment", "planned", ""),
    "scripts/build_alignment_samples.py": ("fstk.evidence.alignment", "planned", ""),
    "scripts/dvd_alignment_review.py": ("fstk.ui.workbenches.alignment", "planned", ""),
    "scripts/record_dvd_alignment_review.py": ("fstk.evidence.approvals", "planned", ""),
    "scripts/extract_candidate_frames.py": ("fstk.evidence.frames", "planned", ""),
    # ---- cadence / trims / map
    "scripts/build_map.ps1": ("fstk.media.cadence_map (repair rules)", "retired",
                              "measurement rules ported to Python; AVS output retired (finding 0002)"),
    "scripts/build_map_vpy.py": ("fstk.media.cadence_map.ivtc_vpy_script", "partial", "IVTC encode-script stage pending"),
    "scripts/build_motion_trace.py": ("fstk.media.motion", "planned", ""),
    "scripts/motion-probe.ps1": ("fstk.media.motion", "retired", "PowerShell retired"),
    "scripts/cadence_analysis.py": ("fstk.media.cadence", "ported", ""),
    "scripts/generate_cadence_evidence.py": ("fstk.stages.cadence (cadence-evidence)", "partial", "side-by-side review GIFs pending"),
    "scripts/analyze_cadence_evidence.py": ("fstk.media.cadence", "planned", ""),
    "scripts/build_cadence_review_assets.py": ("fstk.evidence.cadence", "planned", ""),
    "scripts/cadence_review_window.py": ("fstk.ui.workbenches.cadence", "planned", ""),
    "scripts/cadence_trim_review.py": ("fstk.ui.workbenches.trim", "planned", ""),
    "scripts/record_cadence_measurements.py": ("engine deferred commit + approvals", "ported", ""),
    "scripts/record_video_trim_review.py": ("fstk.evidence.approvals", "planned", ""),
    # ---- audio timing
    "scripts/audio_timing_diagnostic.py": ("fstk.media.audio_timing", "partial", ""),
    "scripts/measure_audio_rate.py": ("fstk.media.audio_timing", "partial", ""),
    "scripts/fade_midpoint_fit.py": ("fstk.media.fade_fit", "partial", ""),
    "scripts/measure_pad_audio.py": ("fstk.media.pad_audio", "planned", ""),
    "scripts/measure_ld_black.py": ("fstk.media.black", "planned", ""),
    "scripts/ld_dvd_audio_compare.py": ("fstk.media.audio_compare", "planned", ""),
    "scripts/ld_only_sync_check.py": ("fstk.media.sync", "planned", ""),
    "scripts/sync_check.py": ("fstk.media.sync", "planned", ""),
    "scripts/build_ld_synced.py": ("fstk.media.audio_timing (sample_plan) + stage", "partial", ""),
    # ---- PAD repair
    "scripts/pad_audit.py": ("fstk.media.pads + fstk.tools.orc_project", "partial", ""),
    "scripts/fullres_check.py": ("fstk.media.pads", "planned", ""),
    "scripts/rife_padframes.py": ("fstk.stages.pads.rife", "planned", ""),
    "scripts/verify_pad_identities.py": ("fstk.media.pads", "planned", ""),
    "scripts/pad_review.py": ("fstk.ui.workbenches.pads", "planned", ""),
    "scripts/pad_classification_review.py": ("fstk.ui.workbenches.pads", "planned", ""),
    "scripts/record_pad_classifications.py": ("fstk.evidence.approvals", "planned", ""),
    "scripts/build_padfix_avs.py": ("—", "retired", "AviSynth retired"),
    "scripts/build_padfix_vpy.py": ("fstk.stages.pads.padfix", "planned", ""),
    "scripts/scan_padfixed_black.py": ("fstk.media.pads", "planned", ""),
    # ---- retime (DVD 5.1 / SRT)
    "scripts/prepare_retime_breaks.py": ("fstk.stages.retime", "planned", ""),
    "scripts/retime_break_candidates.py": ("fstk.media.retime", "ported", ""),
    "scripts/retime_break_review.py": ("fstk.ui.workbenches.retime", "planned", ""),
    "scripts/record_retime_breaks.py": ("fstk.evidence.approvals", "planned", ""),
    "scripts/measure_break_edges.py": ("fstk.media.retime", "planned", ""),
    "scripts/retime_edge_review.py": ("fstk.ui.workbenches.retime", "planned", ""),
    "scripts/record_retime_edge_measurements.py": ("fstk.evidence.approvals", "planned", ""),
    "scripts/build_retime_review_assets.py": ("fstk.evidence.retime", "planned", ""),
    "scripts/retime_dvd_assets.py": ("fstk.stages.retime.assets", "planned", ""),
    # ---- encode / remux / QC / delivery
    "scripts/encode_episode.ps1": ("fstk.stages.encode", "retired", "ported to Python"),
    "scripts/encode_episode_mac.py": ("fstk.stages.encode + fstk.media.encode", "ported", "final pass; validate-pass (3-frame decode) folded into info check"),
    "scripts/encode_padfixed.ps1": ("fstk.stages.encode", "retired", "ported to Python"),
    "scripts/remux_padfixed.ps1": ("fstk.stages.remux", "retired", "ported to Python"),
    "scripts/remux_padfixed_mac.py": ("fstk.stages.remux + fstk.media.remux", "ported", ""),
    "scripts/pipeline.ps1": ("fstk.tools.ffmpeg", "retired", "Invoke-LdFfmpeg → fstk.tools.process"),
    "scripts/process_args.ps1": ("fstk.tools.process", "retired", "PowerShell retired"),
    "scripts/validate_encoded_video.py": ("fstk.media.encode.check_encoded_stream", "ported", "raster now from the master, not hard-coded 768x484"),
    "scripts/av_check_output.py": ("fstk.media.qc", "planned", ""),
    "scripts/qc_report.py": ("fstk.stages.qc", "planned", ""),
    "scripts/build_delivery_review_assets.py": ("fstk.evidence.delivery", "planned", ""),
    "scripts/build_delivery_preview_assets.py": ("fstk.evidence.delivery", "planned", ""),
    "scripts/delivery_review_window.py": ("fstk.ui.workbenches.delivery", "planned", ""),
    # ---- workflow engine / app
    "scripts/run_stage.py": ("fstk.stages.base (registry)", "partial", "STAGES dict → declared stage classes"),
    "scripts/run_stage_task.py": ("fstk.workflow.engine", "planned", ""),
    "scripts/run_workflow.py": ("fstk.workflow.engine", "partial", ""),
    "scripts/run_status.py": ("fstk.workflow.status", "planned", ""),
    "scripts/workflow_recovery.py": ("fstk.workflow.recovery", "planned", ""),
    "scripts/workflow_process_control.py": ("fstk.tools.process", "partial", ""),
    "scripts/job_monitor.py": ("fstk.workflow.monitor", "planned", ""),
    "scripts/episode_progress.py": ("fstk.workflow.status", "planned", ""),
    "scripts/episode_dashboard.py": ("fstk.ui.dashboard", "planned", ""),
    "scripts/open_review_gate.py": ("fstk.evidence.approvals", "partial", ""),
    "scripts/review_inbox.py": ("fstk.evidence.inbox", "planned", ""),
    "scripts/review_popup.py": ("fstk.ui", "retired", "Tkinter retired; PySide6 dialogs"),
    "scripts/workflow_app.py": ("fstk.ui.app", "partial", "Tkinter → PySide6"),
    "scripts/workflow_app_model.py": ("fstk.ui.services", "partial", ""),
    "scripts/workflow_app_process.py": ("fstk.workflow.runner", "planned", ""),
    "scripts/workflow_workbenches.py": ("fstk.ui.workbenches", "planned", ""),
    # ---- LLM runbook tooling (retired per REFACTOR-PLAN §7.2)
    "scripts/opencode_help.py": ("—", "retired", "LLM-operator runbooks retired"),
    "scripts/generate_runbooks.py": ("fstk.cli docs (stage reference)", "retired", "LLM runbooks retired"),
    "scripts/regen_plain_text.py": ("—", "retired", "LLM runbooks retired"),
}


def collect(tree: Path) -> dict[str, int]:
    files = {}
    for p in sorted([*tree.glob("*.py"), *tree.glob("*.ps1"), *tree.glob("scripts/*.py"),
                     *tree.glob("scripts/*.ps1")]):
        rel = p.relative_to(tree).as_posix()
        files[rel] = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
    return files


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    tree = Path(argv[0])
    files = collect(tree)
    missing = sorted(set(files) - set(MAP))
    stale = sorted(set(MAP) - set(files))
    if "--check" in argv:
        for m in missing:
            print(f"UNMAPPED: {m}")
        for s in stale:
            print(f"STALE: {s}")
        return 1 if missing or stale else 0
    counts: dict[str, list[int]] = {}
    rows = []
    for rel, loc in files.items():
        target, status, note = MAP.get(rel, ("?", "unmapped", ""))
        counts.setdefault(status, [0, 0])
        counts[status][0] += 1
        counts[status][1] += loc
        rows.append(f"| `{rel}` | {loc} | {target} | {status} | {note} |")
    out = ["# v38 → fstk inventory", "",
           "Generated by `tools/v38_inventory.py`; do not edit by hand.", "",
           "| Status | Files | Lines |", "|---|---|---|"]
    out += [f"| {k} | {v[0]} | {v[1]} |" for k, v in sorted(counts.items())]
    out += ["", "| v38 file | Lines | New home | Status | Note |", "|---|---|---|---|---|", *rows, ""]
    target = Path(__file__).resolve().parents[1] / "docs" / "v38-inventory.md"
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {target} ({len(rows)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
