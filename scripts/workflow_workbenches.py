#!/usr/bin/env python3
"""Immutable guided plan builders for report-only diagnostics and delivery gates."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
import re
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from workflow_app_model import WorkflowAppError


def _base_plan(context: dict[str, Any], fingerprint_policy: str, job_id: str) -> dict[str, Any]:
    if fingerprint_policy not in ("balanced", "full"):
        raise WorkflowAppError("fingerprint policy must be balanced or full")
    return {
        "schema": "ld-pipeline-workflow-plan-v1",
        "job_id": job_id,
        "episode": context["episode"], "series": context["series"],
        "identity": context["identity"], "episode_title": context.get("title") or context["episode"],
        "profile": str(Path(context["profile"]).resolve(strict=True)),
        "manifest_path": str(Path(context["manifest"]).resolve(strict=True)),
        "instance_root": str(Path(context["instance"]).resolve(strict=True)),
        "fingerprint_policy": fingerprint_policy,
        "decisions": [], "steps": [],
    }


def _path(context: dict[str, Any], key: str, *, required: bool = True,
          must_exist: bool = True) -> Path | None:
    value = (context.get("file_paths") or {}).get(key)
    if value:
        raw_path = Path(value).expanduser()
        if raw_path.is_symlink():
            raise WorkflowAppError(f"episode artifact {key!r} may not be a symlink: {raw_path}")
        path = raw_path.resolve(strict=False)
    else:
        path = None
    if required and path is None:
        raise WorkflowAppError(f"required episode artifact {key!r} is not recorded in the manifest")
    if must_exist and path is not None and not path.is_file():
        raise WorkflowAppError(f"required episode artifact {key!r} is missing: {path or '<not recorded>'}")
    return path


def _token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]


def _existing_unique_paths(paths: list[str | Path | None]) -> list[str]:
    resolved = []
    seen = set()
    for value in paths:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_symlink():
            raise WorkflowAppError(f"delivery input may not be a symlink: {path}")
        path = path.resolve(strict=False)
        key = str(path).casefold()
        if path.is_file() and key not in seen:
            resolved.append(str(path))
            seen.add(key)
    return resolved


def _unique_paths(paths: list[str | Path | None]) -> list[str]:
    """Deduplicate planned inputs while retaining outputs produced by earlier steps."""
    resolved = []
    seen = set()
    for value in paths:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_symlink():
            raise WorkflowAppError(f"delivery input may not be a symlink: {path}")
        path = path.resolve(strict=False)
        key = str(path).casefold()
        if key not in seen:
            resolved.append(str(path))
            seen.add(key)
    return resolved


def _delivery_validation_inputs(context: dict[str, Any]) -> list[str]:
    """Fingerprint the existing manifest artifacts used by the audio/remux/QC gates."""
    manifest = Path(context["manifest"]).resolve(strict=True)
    inputs: list[str | Path | None] = [manifest]
    paths = context.get("file_paths") or {}
    inputs.extend(paths.values())
    downloads = Path(context["downloads"]).resolve(strict=False)
    for capture in context.get("captures") or []:
        if not isinstance(capture, dict):
            continue
        for key in ("tbc_file", "json_file", "efm_file"):
            name = str(capture.get(key) or "")
            relative = Path(name)
            if not name or relative.is_absolute() or len(relative.parts) != 1 or ".." in relative.parts:
                continue
            inputs.append(downloads / relative)
    # A V18 selected-capture gate also consumes the immutable report and its text report.
    audio = (context.get("manifest_data") or {}).get("audio") or {}
    selected = str(audio.get("capture") or "")
    provenance = (audio.get("efm_provenance") or {}).get(selected) if selected else None
    if isinstance(provenance, list) and provenance and isinstance(provenance[-1], dict):
        relative = Path(str(provenance[-1].get("report_path") or ""))
        instance = Path(context["instance"]).resolve(strict=False)
        if (not relative.is_absolute() and relative.parts[:1] == ("references",) and
                ".." not in relative.parts):
            provenance_path = instance / relative
            inputs.append(provenance_path)
            if provenance_path.is_file() and not provenance_path.is_symlink():
                try:
                    provenance_report = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
                    efm_report_rel = Path(str(provenance_report.get("efm_report_relative_path") or ""))
                    if (not efm_report_rel.is_absolute() and ".." not in efm_report_rel.parts):
                        inputs.append(Path(context["audio_root"]) / efm_report_rel)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass  # The validator will report the malformed/missing evidence explicitly.
    return _existing_unique_paths(inputs)


def build_audio_rate_plan(context: dict[str, Any], *, raw_provisional: bool = False,
                          skip_s: float | None = None, points: list[float] | None = None,
                          search_s: float = 8.0, min_corr: float = 0.20,
                          dvd_audio_selector: str = "",
                          fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build a report-only LD/DVD audio-rate diagnostic; it never changes measurements."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    if not re.fullmatch(r"S\d{2}E\d{2}", episode) or series not in ("DS9", "VOY"):
        raise WorkflowAppError("audio-rate diagnostics require a DS9/VOY episode identity")
    if raw_provisional and (skip_s is None or not math.isfinite(skip_s) or skip_s < 0):
        raise WorkflowAppError("raw-provisional mode requires an explicit non-negative window-center skip")
    if not raw_provisional and skip_s is not None:
        raise WorkflowAppError("an explicit skip is only accepted in raw-provisional mode")
    if (not math.isfinite(search_s) or not math.isfinite(min_corr) or
            search_s <= 0 or not 0 < min_corr < 1):
        raise WorkflowAppError("search radius must be positive and min correlation must be between 0 and 1")
    wav, dvd, alignment = (_path(context, key) for key in ("ld_native", "dvd", "align_npy"))
    probe = Path(context["probe"]).resolve(strict=True)
    token = _token()
    report = probe / f"{episode}-audio-rate-diagnostic-{token}.tsv"
    summary = probe / f"{episode}-audio-rate-summary-{token}.json"
    args = ["--out", str(report), "--summary-out", str(summary),
            "--search", f"{search_s:g}", "--min-corr", f"{min_corr:g}"]
    if raw_provisional:
        args.extend(("--raw-provisional", "--skip-s", f"{skip_s:g}"))
    if points:
        if any(not math.isfinite(value) or value < 0 for value in points):
            raise WorkflowAppError("diagnostic window starts must be finite and non-negative")
        args.extend(("--points", *[f"{value:g}" for value in points]))
    if dvd_audio_selector:
        if not re.fullmatch(r"0:a:\d+", dvd_audio_selector.strip()):
            raise WorkflowAppError("DVD audio selector must be 0:a:N")
        args.extend(("--dvd-audio", dvd_audio_selector.strip()))
    plan = _base_plan(context, fingerprint_policy,
                      f"audio-rate-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "audio-rate-mode", "value": "raw-provisional" if raw_provisional else "origin-verified"},
        {"id": "audio-rate-approval", "value": "report-only; no manifest or rate decision"},
        {"id": "audio-rate-search-s", "value": search_s},
        {"id": "audio-rate-min-corr", "value": min_corr},
        {"id": "audio-rate-points-s", "value": points or "helper-selected content windows"},
    ]
    plan["steps"] = [{
        "id": "measure-report-only-audio-rate", "stage": "measure-audio-rate", "args": args,
        "inputs": [str(Path(context["manifest"]).resolve(strict=True)), str(wav), str(dvd), str(alignment)],
        "outputs": [str(report), str(summary)],
    }]
    return plan


def build_pad_audio_occupancy_plan(context: dict[str, Any], *, window_ms: float = 100.0,
                                   control_offsets_s: list[float] | None = None,
                                   minimum_controls: int = 10,
                                   fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build a report-only PAD audio occupancy plan with a collision-safe report path."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    if not math.isfinite(window_ms) or window_ms <= 0 or minimum_controls < 1:
        raise WorkflowAppError("window duration and minimum control count must be positive")
    offsets = list(control_offsets_s or (5.0, 10.0, 20.0))
    if not offsets or any(not math.isfinite(value) or value <= 0 for value in offsets):
        raise WorkflowAppError("control offsets must be finite positive non-zero seconds")
    wav, project = _path(context, "ld_native"), _path(context, "stack_project")
    probe = Path(context["probe"]).resolve(strict=True)
    token = _token()
    report = probe / f"{episode}-pad-audio-occupancy-{token}.json"
    args = ["--window-ms", f"{window_ms:g}", "--minimum-controls", str(minimum_controls),
            "--control-offsets", *[f"{value:g}" for value in offsets], "--report-out", str(report)]
    plan = _base_plan(context, fingerprint_policy,
                      f"pad-audio-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "pad-audio-mode", "value": "report-only"},
        {"id": "pad-audio-window-ms", "value": window_ms},
        {"id": "pad-audio-control-offsets-s", "value": offsets},
        {"id": "pad-audio-minimum-controls", "value": minimum_controls},
        {"id": "pad-audio-rate-decision", "value": "not included"},
    ]
    plan["steps"] = [{
        "id": "measure-report-only-pad-audio-occupancy", "stage": "measure-pad-audio", "args": args,
        "inputs": [str(Path(context["manifest"]).resolve(strict=True)), str(wav), str(project)],
        "outputs": [str(report)],
    }]
    return plan


def build_motion_trace_plan(context: dict[str, Any], *,
                            fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Plan a source motion trace aligned to the measured audio-pad frame count."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    frames = (context.get("manifest_data", {}).get("audio") or {}).get("pad_frames")
    if type(frames) is not int or frames < 0:
        raise WorkflowAppError("motion trace requires measured audio.pad_frames; do not guess the trim")
    source = _path(context, "stack_master")
    probe = Path(context["probe"]).resolve(strict=True)
    token = _token()
    output = probe / f"{episode}-motion-trace-{token}.txt"
    plan = _base_plan(context, fingerprint_policy,
                      f"motion-trace-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [{"id": "motion-trace-alignment", "value": f"drop measured {frames} source-pad frame(s)"},
                         {"id": "motion-trace-output", "value": str(output)}]
    plan["steps"] = [{"id": "measure-source-motion-trace", "stage": "motion-trace",
                      "args": ["--out", str(output), "--trim-head", str(frames)],
                      "inputs": [str(Path(context["manifest"]).resolve(strict=True)), str(source)],
                      "outputs": [str(output)]}]
    return plan


def build_cadence_map_plan(context: dict[str, Any], tfm_log: str | Path,
                           motion_trace: str | Path | None = None, *,
                           mac_repair: str = "none",
                           fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Plan the platform-specific cadence-map/encode-script emitter without choosing trims."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    manifest_data = context.get("manifest_data") or {}
    video = manifest_data.get("video") or {}
    audio = manifest_data.get("audio") or {}
    dvd_available = manifest_data.get("dvd_reference_available") is True
    if manifest_data.get("dvd_reference_available") is None:
        dvd_path = (context.get("file_paths") or {}).get("dvd")
        dvd_available = bool(dvd_path and Path(dvd_path).is_file())
    measured = [video.get("head_trim"), video.get("tail_trim"), audio.get("pad_frames")]
    if any(type(value) is not int or value < 0 for value in measured):
        raise WorkflowAppError("cadence map requires measured head_trim, tail_trim, and audio.pad_frames")
    platform = str(context.get("platform") or "").casefold()
    if platform not in ("windows", "win32", "macos", "darwin", "mac"):
        raise WorkflowAppError("cadence map is only enabled for the validated Windows/macOS paths")
    source = _path(context, "stack_master")
    output = _path(context, "avs_encode", must_exist=False)
    if output is None:
        raise WorkflowAppError("manifest files.avs_encode is required for the cadence source script")
    if output.exists() or output.is_symlink():
        raise WorkflowAppError(f"refusing to overwrite existing source-derived cadence script: {output}")
    manifest = Path(context["manifest"]).resolve(strict=True)
    inputs = [str(manifest), str(source)]
    outputs = [str(output)]
    args: list[str] = []
    if platform in ("windows", "win32"):
        log_raw = Path(tfm_log).expanduser()
        if log_raw.is_symlink() or not log_raw.is_file():
            raise WorkflowAppError(f"full-side TFM log is missing or unsafe: {log_raw}")
        log_path = log_raw.resolve(strict=True)
        if not motion_trace:
            raise WorkflowAppError("Windows cadence-map plan requires the episode's measured motion trace")
        raw_motion = Path(motion_trace).expanduser()
        if raw_motion.is_symlink() or not raw_motion.is_file():
            raise WorkflowAppError(f"motion trace is missing or unsafe: {raw_motion}")
        motion_path = raw_motion.resolve(strict=True)
        summary = _path(context, "cadence_summary", required=False, must_exist=False)
        di_map = _path(context, "di_map", required=False, must_exist=False)
        if summary is None:
            summary = Path(context["probe"]).resolve(strict=True) / f"{log_path.stem}-cadence-summary.txt"
        if di_map is None:
            di_map = Path(context["probe"]).resolve(strict=True) / f"{log_path.stem}-di-map.txt"
        cadence = Path(context["probe"]).resolve(strict=True) / f"{log_path.stem}-full-cadence.txt"
        for existing in (summary, di_map, cadence):
            if existing and (existing.exists() or existing.is_symlink()):
                raise WorkflowAppError(f"refusing to overwrite an existing cadence report: {existing}")
        args.extend(("-Log", str(log_path), "-Source", str(source),
                     "-MapOut", str(output), "-SumOut", str(summary),
                     "-DiOut", str(di_map), "-CadOut", str(cadence),
                     "-SourcePad", str(audio["pad_frames"]),
                     "-TrimStart", str(video["head_trim"]),
                     "-TrimEnd", str(video["tail_trim"])))
        args.extend(("-Motion", str(motion_path)))
        inputs.append(str(log_path))
        inputs.append(str(motion_path))
        outputs.extend(str(path) for path in (summary, di_map, cadence))
        plan_mode = "windows-tfm-log-to-cadence-map"
    else:
        di_map = _path(context, "di_map")
        if mac_repair not in ("none", "qtgmc", "pp2"):
            raise WorkflowAppError("macOS repair choice must be none, qtgmc, or pp2")
        args.extend(("--di-map", str(di_map), "--out", str(output), "--repair", mac_repair))
        inputs.append(str(di_map))
        plan_mode = "macos-di-map-to-vpy"
    token = _token()
    plan = _base_plan(context, fingerprint_policy,
                      f"cadence-map-{series.lower()}-{episode.lower()}-{token[-8:]}")
    trim_policy = (
        "DVD reference: independently map visual and native-audio start/end points onto the LD timeline; "
        "preserve measured DVD A/V offset. Keep the LD internal commercial-break timeline; use mapped "
        "edges to retime DVD tracks, and judge success by final A/V QC, not equal black durations."
        if dvd_available else
        "No DVD reference: use only measured LD-side head/tail trims; do not infer DVD correspondence "
        "or alter internal breaks.")
    plan["decisions"] = [
        {"id": "cadence-map-mode", "value": plan_mode},
        {"id": "measured-audio-pad-frames", "value": audio["pad_frames"]},
        {"id": "measured-video-head-trim", "value": video["head_trim"]},
        {"id": "measured-video-tail-trim", "value": video["tail_trim"]},
        {"id": "dvd-boundary-policy", "value": trim_policy},
        {"id": "cadence-map-output", "value": str(output)},
    ]
    review_inputs = [*outputs, *inputs]
    if dvd_available:
        review_inputs.extend(_existing_unique_paths([
            (context.get("file_paths") or {}).get(key)
            for key in ("dvd", "align_npy", "master_descriptors", "dvd_descriptors",
                        "ld_native", "ld_corrected", "ld_synced", "audio_rate_report",
                        "audio_timing_report")]))
        review_inputs = list(dict.fromkeys(review_inputs))
    plan["steps"] = [
        {"id": "build-cadence-map-and-source-script", "stage": "build-map", "args": args,
         "inputs": inputs, "outputs": outputs},
        {"id": "review-cadence-evidence", "kind": "review", "for_stage": "build-map",
          "prompt": (f"Review {episode}'s cadence summary, deinterlace map, and source-derived script. "
                     "Resolve any CHECK/hard-spot/hybrid intervals from evidence; this plan does not "
                    "write cadence measurements or choose trims in the manifest. " + trim_policy),
          "inputs": review_inputs, "outputs": []},
    ]
    return plan


def build_delivery_plan(context: dict[str, Any], *, planned_encode_gib: float | None,
                        fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build encode/remux/QC plan with separate explicit approvals before encode and remux.

    Existing outputs are never overwritten: the builder selects QC-only, remux+QC, or the
    complete encode/remux/QC chain from the current artifact inventory.
    """
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    manifest = Path(context["manifest"]).resolve(strict=True)
    data = context.get("manifest_data") or {}
    profile_policy = fingerprint_policy
    token = _token()
    probe = Path(context["probe"]).resolve(strict=True)
    qc_report = probe / f"{episode}-qc-report-{token}.md"
    if qc_report.exists() or qc_report.is_symlink():
        raise WorkflowAppError(f"refusing to reuse an existing QC report path: {qc_report}")
    final_video = _path(context, "padfixed_video", required=False, must_exist=False)
    deliverable_key = str(data.get("deliverable") or "padfixed")
    deliverable = _path(context, deliverable_key, required=False, must_exist=False)
    mode = "qc-only" if deliverable and deliverable.is_file() else (
        "remux-qc" if final_video and final_video.is_file() else "encode-remux-qc")
    if mode == "encode-remux-qc":
        if (planned_encode_gib is None or not math.isfinite(planned_encode_gib) or
                planned_encode_gib <= 0):
            raise WorkflowAppError("final encode space estimate must be a positive measured GiB estimate")
        final_video = _path(context, "padfixed_video", must_exist=False)
        if final_video.exists():
            raise WorkflowAppError(f"final-video output already exists; refusing to plan an overwrite: {final_video}")
        padfix = _path(context, "avs_padfix")
        stack_master = _path(context, "stack_master")
    else:
        padfix = stack_master = None
    if mode != "qc-only":
        if deliverable is None:
            raise WorkflowAppError(f"manifest files.{deliverable_key} is missing; cannot identify remux output")
        if deliverable.exists():
            raise WorkflowAppError(f"remux output already exists; refusing to plan an overwrite: {deliverable}")
        if final_video is None or final_video.resolve(strict=False) == deliverable.resolve(strict=False):
            raise WorkflowAppError("encoded video and remux deliverable paths must be distinct")
        ld_audio_key = str(data.get("ld_audio_source") or "")
        if not ld_audio_key:
            raise WorkflowAppError("manifest ld_audio_source must be explicitly selected before remux")
        ld_audio = _path(context, ld_audio_key)
        mux_tracks = list(data.get("mux_tracks") or [])
        if "ld" not in mux_tracks:
            raise WorkflowAppError("manifest mux_tracks must retain the LD default audio track")
        remux_inputs = [str(manifest), str(final_video), str(ld_audio)]
        if "51" in mux_tracks:
            remux_inputs.append(str(_path(context, "retimed_51")))
        if "srt" in mux_tracks:
            remux_inputs.append(str(_path(context, "retimed_srt")))
    else:
        remux_inputs = []
    plan = _base_plan(context, profile_policy,
                      f"delivery-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "delivery-plan-mode", "value": mode},
        {"id": "delivery-output", "value": str(deliverable) if deliverable else str(final_video)},
        {"id": "final-encode-space-estimate-gib", "value": planned_encode_gib if mode == "encode-remux-qc" else "not applicable"},
        {"id": "final-encode-remux-approval", "value": "separate workflow review gates; no implicit approval"},
    ]
    steps: list[dict[str, Any]] = []
    if mode == "encode-remux-qc":
        steps.extend([
            {"id": "validate-final-encode-chain", "stage": "encode-validate",
             "inputs": _existing_unique_paths([manifest, padfix, stack_master]), "outputs": []},
            {"id": "approve-one-final-encode", "kind": "review",
             "for_stage": "encode-validate",
             "prompt": (f"Review {episode}'s source-derived PAD-repaired chain, measured frame count/duration, "
                        f"and {planned_encode_gib:g} GiB space estimate. Approval authorizes exactly one final video encode to "
                        f"{final_video}; no remux is approved by this gate."),
             "inputs": _existing_unique_paths([manifest, padfix, stack_master]), "outputs": []},
            {"id": "encode-final-video-once", "stage": "encode-final",
             "inputs": _existing_unique_paths([manifest, padfix, stack_master]), "outputs": [str(final_video)]},
        ])
    if mode == "remux-qc":
        steps.append({"id": "validate-remux-inputs", "stage": "validate-audio",
                      "inputs": _delivery_validation_inputs(context), "outputs": []})
        steps.append({"id": "approve-stream-copy-remux", "kind": "review",
                      "for_stage": "validate-audio",
                      "prompt": (f"Review {episode}'s existing encoded video, selected LD audio, declared mux tracks, "
                                 f"and empty output destination {deliverable}. Approval authorizes a stream-copy remux only."),
                      "inputs": _existing_unique_paths([manifest, *remux_inputs[1:]]), "outputs": []})
    elif mode == "encode-remux-qc":
        steps.append({"id": "validate-remux-audio-inputs", "stage": "validate-audio",
                      "inputs": _delivery_validation_inputs(context), "outputs": []})
        steps.append({"id": "approve-stream-copy-remux", "kind": "review",
                      "for_stage": "validate-audio",
                      "prompt": (f"Review the completed {episode} video encode and the declared audio/subtitle sources. "
                                 f"Approval authorizes a stream-copy remux to {deliverable}; it does not alter the encoded video."),
                      "inputs": _unique_paths([final_video, *remux_inputs[2:]]), "outputs": []})
    if mode != "qc-only":
        steps.append({"id": "remux-approved-tracks", "stage": "remux",
                      "inputs": _unique_paths(remux_inputs), "outputs": [str(deliverable)]})
    else:
        deliverable = _path(context, deliverable_key, must_exist=True)
        steps.append({"id": "validate-existing-deliverable", "stage": "validate-deliverable",
                      "inputs": _existing_unique_paths([manifest, deliverable]), "outputs": []})
    qc_inputs = _delivery_validation_inputs(context)
    deliverable_input = str(deliverable)
    if all(Path(value).resolve(strict=False) != Path(deliverable_input).resolve(strict=False)
           for value in qc_inputs):
        qc_inputs.append(deliverable_input)
    steps.append({"id": "run-delivery-qc", "stage": "qc",
                  "args": ["--report-out", str(qc_report)],
                  "inputs": qc_inputs, "outputs": [str(qc_report)]})
    steps.append({"id": "review-delivery-qc", "kind": "review", "for_stage": "qc",
                  "prompt": (f"Review the complete QC report for {episode}. PASS requires every hard gate to pass; "
                             "WARN/LIMIT is not PASS. This gate grants no further encode/remux permission."),
                  "inputs": [str(qc_report), str(deliverable)], "outputs": []})
    plan["steps"] = steps
    return plan


class AudioDiagnosticWindow:
    """Small parameter form for report-only rate-fit and PAD-occupancy diagnostics."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any],
                 on_plan: Any, fingerprint_policy: str = "balanced"):
        self.parent = parent
        self.context = context
        self.on_plan = on_plan
        self.fingerprint_policy = fingerprint_policy
        self.window: tk.Toplevel | None = None
        self.kind = tk.StringVar(value="audio-rate")
        self.raw_provisional = tk.BooleanVar(value=False)
        self.values: dict[str, tk.StringVar] = {}

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Audio diagnostics — {self.context['identity']}")
        self.window.geometry("720x520")
        self.window.minsize(650, 480)
        self.window.transient(self.parent)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Report-only audio workbench — {self.context['identity']}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(body, text="These diagnostics write immutable reports only. They never select a rate or edit the manifest.",
                  wraplength=680).pack(anchor="w", pady=(4, 10))
        mode = ttk.LabelFrame(body, text="Diagnostic", padding=8)
        mode.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(mode, text="LD/DVD audio-rate comparison", variable=self.kind,
                        value="audio-rate").pack(anchor="w")
        ttk.Radiobutton(mode, text="PAD audio occupancy", variable=self.kind,
                        value="pad-occupancy").pack(anchor="w")

        form = ttk.LabelFrame(body, text="Explicit parameters", padding=8)
        form.pack(fill="both", expand=True)
        fields = [
            ("skip_s", "Provisional window-center skip (s)", "", "Only used when Raw provisional is checked."),
            ("points", "Output window starts (s, optional)", "", "Blank lets the diagnostic select content windows."),
            ("dvd_audio_selector", "DVD audio selector (optional)", "", "Use 0:a:N only after checking streams."),
            ("search_s", "Rate-fit search radius (s)", "8", "Used only for the LD/DVD rate comparison."),
            ("min_corr", "Minimum correlation", "0.20", "Screening threshold, not a rate approval."),
            ("window_ms", "PAD window (ms)", "100", "Used only for PAD audio occupancy."),
            ("control_offsets", "PAD control offsets (s)", "5 10 20", "Used only for PAD audio occupancy."),
            ("minimum_controls", "Minimum PAD controls", "10", "Used only for PAD audio occupancy."),
        ]
        for row, (key, label, default, help_text) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            variable = tk.StringVar(value=default)
            self.values[key] = variable
            entry = ttk.Entry(form, textvariable=variable, width=32)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            ttk.Label(form, text=help_text, wraplength=270).grid(
                row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        ttk.Checkbutton(form, text="Raw provisional rate diagnostic (no origin/rate promotion)",
                        variable=self.raw_provisional).grid(row=len(fields), column=0, columnspan=2,
                                                            sticky="w", pady=(6, 0))
        form.columnconfigure(1, weight=1)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Cancel", command=self.window.destroy).pack(side="right", padx=5)
        ttk.Button(actions, text="Build diagnostic plan…", command=self._build).pack(side="right")

    def _build(self) -> None:
        assert self.window is not None
        try:
            if self.kind.get() == "audio-rate":
                points_text = self.values["points"].get().strip()
                points = ([float(value) for value in re.split(r"[\s,]+", points_text) if value]
                          if points_text else None)
                skip_text = self.values["skip_s"].get().strip()
                if self.raw_provisional.get() and not skip_text:
                    raise WorkflowAppError("raw-provisional mode requires an explicit window-center skip")
                if not self.raw_provisional.get() and skip_text:
                    raise WorkflowAppError("clear the provisional skip unless Raw provisional is selected")
                plan = build_audio_rate_plan(
                    self.context, raw_provisional=self.raw_provisional.get(),
                    skip_s=float(skip_text) if skip_text else None, points=points,
                    search_s=float(self.values["search_s"].get()),
                    min_corr=float(self.values["min_corr"].get()),
                    dvd_audio_selector=self.values["dvd_audio_selector"].get().strip(),
                    fingerprint_policy=self.fingerprint_policy)
                suffix = "audio-rate-diagnostic-plan.json"
                scope = "report-only audio-rate diagnostic"
            else:
                offsets = [float(value) for value in re.split(
                    r"[\s,]+", self.values["control_offsets"].get().strip()) if value]
                plan = build_pad_audio_occupancy_plan(
                    self.context, window_ms=float(self.values["window_ms"].get()),
                    control_offsets_s=offsets,
                    minimum_controls=int(self.values["minimum_controls"].get()),
                    fingerprint_policy=self.fingerprint_policy)
                suffix = "pad-audio-occupancy-plan.json"
                scope = "report-only PAD audio occupancy diagnostic"
        except (WorkflowAppError, TypeError, ValueError) as exc:
            messagebox.showerror("Diagnostic plan not built", str(exc), parent=self.window)
            return
        name = f"{plan['series']}-{plan['episode']}-{suffix}"
        if self.on_plan(plan, name, scope):
            self.window.destroy()


class DeliveryPlanWindow:
    """Builds an explicitly gated encode/remux/QC plan; it does not execute it."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any], on_plan: Any,
                 fingerprint_policy: str = "balanced"):
        self.parent = parent
        self.context = context
        self.on_plan = on_plan
        self.fingerprint_policy = fingerprint_policy
        self.window: tk.Toplevel | None = None
        self.planned_gib = tk.StringVar(value="")

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Delivery plan — {self.context['identity']}")
        self.window.geometry("740x360")
        self.window.minsize(640, 320)
        self.window.transient(self.parent)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Encode / remux / QC plan — {self.context['identity']}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(body,
                  text="The builder detects existing outputs and chooses QC-only, remux+QC, or the full chain. "
                       "It never overwrites outputs. Final encode and remux each have separate human review gates; "
                       "Run also asks for explicit confirmation.",
                  wraplength=700, justify="left").pack(anchor="w", pady=(5, 12))
        row = ttk.Frame(body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Peak additional space for final encode (GiB; required only when encoding):").pack(side="left")
        ttk.Entry(row, textvariable=self.planned_gib, width=12).pack(side="left", padx=8)
        ttk.Label(body, text="QC may decode the complete deliverable and can take a long time. "
                            "No encode/remux runs when you build or preview this plan.",
                  wraplength=700).pack(anchor="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="Build gated delivery plan…", command=self._build).pack(side="right")

    def _build(self) -> None:
        assert self.window is not None
        raw = self.planned_gib.get().strip()
        try:
            planned = float(raw) if raw else None
            plan = build_delivery_plan(
                self.context, planned_encode_gib=planned,
                fingerprint_policy=self.fingerprint_policy)
        except (WorkflowAppError, TypeError, ValueError) as exc:
            messagebox.showerror("Delivery plan not built", str(exc), parent=self.window)
            return
        name = f"{plan['series']}-{plan['episode']}-delivery-plan.json"
        if self.on_plan(plan, name, "explicitly gated delivery"):
            self.window.destroy()


class CadenceWorkbenchWindow:
    """Guided plans for measured source motion and platform-specific cadence-map emission."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any], on_plan: Any,
                 fingerprint_policy: str = "balanced"):
        self.parent = parent
        self.context = context
        self.on_plan = on_plan
        self.fingerprint_policy = fingerprint_policy
        self.window: tk.Toplevel | None = None
        self.tfm_log = tk.StringVar(value=str(context.get("file_paths", {}).get("tfm_log", "")))
        motion_default = Path(str(context.get("file_paths", {}).get("motion", "")))
        self.motion_trace = tk.StringVar(value=str(motion_default if motion_default.is_file() else ""))
        self.repair = tk.StringVar(value="none")

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Cadence workbench — {self.context['identity']}")
        self.window.geometry("820x390")
        self.window.minsize(700, 340)
        self.window.transient(self.parent)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Cadence evidence plans — {self.context['identity']}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(body, text=("Plans use measured stack/audio-pad/head/tail values and refuse output overwrites. "
                              "They generate evidence or a source-derived script; they do not choose/record trims."),
                  wraplength=780).pack(anchor="w", pady=(4, 10))
        fields = ttk.Frame(body)
        fields.pack(fill="x", expand=True)
        platform = str(self.context.get("platform", "")).casefold()
        if platform in ("windows", "win32"):
            ttk.Label(fields, text="Existing full-side TFM log").grid(row=0, column=0, sticky="w", pady=5)
            ttk.Entry(fields, textvariable=self.tfm_log).grid(row=0, column=1, sticky="ew", pady=5)
            ttk.Button(fields, text="Browse…", command=self._browse_tfm).grid(row=0, column=2, padx=5)
            ttk.Label(fields, text="Measured motion trace (required for the native-30p gate)").grid(
                row=1, column=0, sticky="w", pady=5)
            ttk.Entry(fields, textvariable=self.motion_trace).grid(row=1, column=1, sticky="ew", pady=5)
            ttk.Button(fields, text="Browse…", command=self._browse_motion).grid(row=1, column=2, padx=5)
        elif platform in ("macos", "darwin", "mac"):
            ttk.Label(fields, text=("macOS map emission consumes an existing measured di-map; "
                                    "TFM/di-map generation is not inferred here."),
                      wraplength=750).grid(row=0, column=0, columnspan=3, sticky="w", pady=5)
            ttk.Label(fields, text="Interlaced repair policy").grid(row=1, column=0, sticky="w", pady=5)
            ttk.Combobox(fields, textvariable=self.repair, values=("none", "qtgmc", "pp2"),
                         state="readonly", width=16).grid(row=1, column=1, sticky="w", pady=5)
        else:
            ttk.Label(fields, text=f"Unsupported cadence platform: {platform}",
                      wraplength=750, foreground="#8b1a1a").grid(
                          row=0, column=0, columnspan=3, sticky="w", pady=5)
        fields.columnconfigure(1, weight=1)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="Build cadence-map plan…", command=self._build_map).pack(side="right")
        ttk.Button(buttons, text="Plan motion trace…", command=self._build_motion).pack(side="left")

    def _browse_tfm(self) -> None:
        if not self.window:
            return
        path = filedialog.askopenfilename(
            parent=self.window, title="Select this episode's full-side TFM log",
            initialdir=str(self.context["probe"]),
            filetypes=(("TFM log", "*.txt *.log"), ("All files", "*.*")))
        if path:
            self.tfm_log.set(path)

    def _browse_motion(self) -> None:
        if not self.window:
            return
        path = filedialog.askopenfilename(
            parent=self.window, title="Select this episode's measured motion trace",
            initialdir=str(self.context["probe"]),
            filetypes=(("Motion trace", "*.txt"), ("All files", "*.*")))
        if path:
            self.motion_trace.set(path)

    def _build_motion(self) -> None:
        assert self.window is not None
        try:
            plan = build_motion_trace_plan(self.context, fingerprint_policy=self.fingerprint_policy)
        except WorkflowAppError as exc:
            messagebox.showerror("Motion trace plan not built", str(exc), parent=self.window)
            return
        name = f"{plan['series']}-{plan['episode']}-motion-trace-plan.json"
        if self.on_plan(plan, name, "measured source motion trace"):
            self.window.destroy()

    def _build_map(self) -> None:
        assert self.window is not None
        try:
            plan = build_cadence_map_plan(
                self.context, self.tfm_log.get().strip(), self.motion_trace.get().strip() or None,
                mac_repair=self.repair.get(), fingerprint_policy=self.fingerprint_policy)
        except WorkflowAppError as exc:
            messagebox.showerror("Cadence map plan not built", str(exc), parent=self.window)
            return
        name = f"{plan['series']}-{plan['episode']}-cadence-map-plan.json"
        if self.on_plan(plan, name, "source cadence map and script"):
            self.window.destroy()
