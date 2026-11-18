#!/usr/bin/env python3
"""Immutable guided plan builders for report-only diagnostics and delivery gates."""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import re
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from fileutil import sha256_file
from efm_paths import decode_artifact_paths
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


def _alignment_review_path(context: dict[str, Any], raw_value: Any) -> Path | None:
    """Resolve the portable probe-root marker, while tolerating legacy absolute markers."""
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    raw = Path(raw_value.strip()).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)
    parts = raw.parts
    if not parts or parts[0].casefold() != "probe" or ".." in parts:
        return None
    return (Path(context["probe"]) / Path(*parts[1:])).resolve(strict=False)


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
    if (skip_s is not None and
            (not math.isfinite(skip_s) or skip_s < 0)):
        raise WorkflowAppError("provisional window-center skip must be finite and non-negative")
    if not raw_provisional and skip_s is not None:
        raise WorkflowAppError("an explicit skip is only accepted in raw-provisional mode")
    if raw_provisional:
        data = context.get("manifest_data") or {}
        delta = (data.get("video") or {}).get("delta_s")
        pad = (data.get("audio") or {}).get("pad_frames")
        if (isinstance(delta, bool) or not isinstance(delta, (int, float)) or
                not math.isfinite(float(delta)) or type(pad) is not int or pad < 0):
            raise WorkflowAppError(
                "raw-provisional rate diagnostics require measured video.delta_s and audio.pad_frames")
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
        args.append("--raw-provisional")
        if skip_s is not None:
            args.extend(("--skip-s", f"{skip_s:g}"))
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


def build_audio_evidence_plan(context: dict[str, Any], *,
                              fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Run every currently eligible report-only audio timing diagnostic."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    data = context.get("manifest_data") or {}
    audio = data.get("audio") or {}
    video = data.get("video") or {}
    if not audio.get("capture") or type(audio.get("pad_frames")) is not int or audio.get("pad_frames") < 0:
        raise WorkflowAppError(
            "audio timing evidence requires the verified selected capture and measured audio.pad_frames")
    native = _path(context, "ld_native")
    project = _path(context, "stack_project")
    manifest = Path(context["manifest"]).resolve(strict=True)
    probe = Path(context["probe"]).resolve(strict=True)
    token = _token()
    steps: list[dict[str, Any]] = []
    available: list[str] = []
    deferred: list[str] = []

    duration_report = probe / f"{episode}-audio-duration-pad-{token}.json"
    if duration_report.exists() or duration_report.is_symlink():
        raise WorkflowAppError(f"refusing to reuse audio timing report: {duration_report}")
    steps.append({
        "id": "measure-report-only-audio-duration-pad",
        "stage": "audio-diagnostic",
        "args": ["--report-out", str(duration_report)],
        "inputs": [str(manifest), str(native), str(project)],
        "outputs": [str(duration_report)],
    })
    available.append("native-PCM duration vs verified Orc frame-map PAD")

    delta = video.get("delta_s")
    if isinstance(delta, (int, float)) and not isinstance(delta, bool) and math.isfinite(float(delta)):
        master = _path(context, "stack_master")
        dvd = _path(context, "dvd", required=False)
        fade_report = probe / f"{episode}-fade-midpoint-fit-{token}.json"
        if fade_report.exists() or fade_report.is_symlink():
            raise WorkflowAppError(f"refusing to reuse fade-midpoint report: {fade_report}")
        fade_inputs = [str(manifest), str(master), str(native)]
        if dvd and dvd.is_file():
            fade_inputs.append(str(dvd))
        steps.append({
            "id": "measure-report-only-fade-midpoints",
            "stage": "fade-midpoint-fit",
            "args": ["--out", str(fade_report)],
            "inputs": fade_inputs, "outputs": [str(fade_report)],
        })
        available.append("primary master/WAV fade-midpoint fit")
        if dvd and dvd.is_file():
            available.append("optional DVD/WAV fade-midpoint corroboration")
    else:
        deferred.append("fade-midpoint fits: measured video.delta_s is not available")

    dvd_available = data.get("dvd_reference_available")
    if dvd_available is None:
        dvd_available = bool((context.get("file_paths") or {}).get("dvd"))
    align_value = (context.get("file_paths") or {}).get("align_npy")
    alignment = Path(str(align_value)).expanduser() if align_value else None
    alignment_review = video.get("dvd_alignment_review") or {}
    alignment_approved = False
    if alignment and alignment.is_file() and not alignment.is_symlink():
        try:
            reviewed_path = _alignment_review_path(context, alignment_review.get("path"))
            alignment_approved = (
                alignment_review.get("status") == "APPROVED" and
                reviewed_path is not None and
                reviewed_path.resolve(strict=True) == alignment.resolve(strict=True) and
                sha256_file(alignment) == alignment_review.get("sha256"))
        except (OSError, ValueError):
            alignment_approved = False
    pad = audio.get("pad_frames")
    if (dvd_available and isinstance(delta, (int, float)) and
            not isinstance(delta, bool) and math.isfinite(float(delta)) and
            type(pad) is int and pad >= 0 and alignment and alignment_approved):
        dvd = _path(context, "dvd")
        report = probe / f"{episode}-picture-audio-rate-{token}.tsv"
        summary = report.with_suffix(".summary.json")
        if any(path.exists() or path.is_symlink() for path in (report, summary)):
            raise WorkflowAppError(f"refusing to reuse picture/audio rate reports: {report}, {summary}")
        steps.append({
            "id": "measure-raw-provisional-picture-audio-rate",
            "stage": "measure-audio-rate",
            "args": ["--raw-provisional", "--out", str(report), "--summary-out", str(summary)],
            "inputs": [str(manifest), str(native), str(dvd), str(alignment)],
            "outputs": [str(report), str(summary)],
        })
        available.append("raw-provisional DVD-picture/DVD-audio correlation (corroboration only)")
    elif dvd_available:
        deferred.append(
            "picture/audio correlation: requires measured delta_s/audio.pad_frames and a visually approved, hash-bound DVD/LD alignment path")
    plan = _base_plan(
        context, fingerprint_policy,
        f"audio-evidence-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "audio-evidence-suite", "value": available},
        {"id": "audio-evidence-deferred", "value": deferred or "none"},
        {"id": "dvd-backed-audio-promotion", "value": "report-only; no automatic rate/origin promotion"},
    ]
    plan["steps"] = steps
    return plan


def build_alignment_samples_plan(context: dict[str, Any], reports: list[str | Path], *,
                                 fingerprint_policy: str = "balanced",
                                 duration_s: float = 8.0,
                                 max_samples: int = 8) -> dict[str, Any]:
    """Build disposable A/V comparison clips from immutable candidate reports."""
    if not reports:
        raise WorkflowAppError("alignment samples require at least one completed candidate report")
    if (not math.isfinite(duration_s) or not 2 <= duration_s <= 20 or
            type(max_samples) is not int or not 1 <= max_samples <= 12):
        raise WorkflowAppError("sample duration/count is outside the supported preview range")
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    manifest = Path(context["manifest"]).resolve(strict=True)
    probe = Path(context["probe"]).resolve(strict=True)
    master, wav = _path(context, "stack_master"), _path(context, "ld_native")
    inputs = [str(manifest), str(master), str(wav)]
    report_paths: list[Path] = []
    for value in reports:
        path = Path(value).expanduser()
        if path.is_symlink() or not path.is_file():
            raise WorkflowAppError(f"candidate report is missing or unsafe: {path}")
        path = path.resolve(strict=True)
        try:
            path.relative_to(probe)
        except ValueError as exc:
            raise WorkflowAppError(f"candidate reports must remain in the episode probe folder: {path}") from exc
        if path.suffix.casefold() != ".json":
            raise WorkflowAppError(f"candidate report must be structured JSON: {path}")
        try:
            schema = json.loads(path.read_text(encoding="utf-8-sig")).get("schema")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise WorkflowAppError(f"candidate report is not valid structured evidence: {path}") from exc
        if schema not in ("fade-midpoint-fit-v1", "dvd-picture-audio-rate-summary-v2"):
            continue
        report_paths.append(path)
        inputs.append(str(path))
    if not report_paths:
        raise WorkflowAppError("no supported fade-fit or picture/audio candidate reports are available")
    video = (context.get("manifest_data") or {}).get("video") or {}
    review = video.get("dvd_alignment_review") or {}
    alignment_value = (context.get("file_paths") or {}).get("align_npy")
    alignment = Path(str(alignment_value)).expanduser() if alignment_value else None
    if alignment and alignment.is_file() and not alignment.is_symlink():
        try:
            reviewed_path = _alignment_review_path(context, review.get("path"))
            if (review.get("status") == "APPROVED" and
                    reviewed_path is not None and
                    reviewed_path.resolve(strict=True) == alignment.resolve(strict=True) and
                    sha256_file(alignment) == review.get("sha256")):
                inputs.append(str(alignment.resolve(strict=True)))
        except (OSError, ValueError):
            pass
    token = _token()
    output_dir = probe / f"{episode}-alignment-samples-{token}"
    plan = _base_plan(
        context, fingerprint_policy,
        f"alignment-samples-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "alignment-sample-policy",
         "value": "candidate-only disposable A/V previews; no manifest or production audio writes"},
        {"id": "alignment-sample-duration-s", "value": duration_s},
        {"id": "alignment-sample-max-count", "value": max_samples},
    ]
    args = ["--series", series, "--out-dir", str(output_dir),
            "--duration-s", f"{duration_s:g}", "--max-samples", str(max_samples)]
    for path in report_paths:
        args.extend(("--report", str(path)))
    plan["steps"] = [{
        "id": "build-candidate-alignment-samples",
        "stage": "alignment-samples",
        "args": args,
        "inputs": list(dict.fromkeys(inputs)),
        "outputs": [str(output_dir)],
    }]
    return plan


def build_dvd_alignment_review_plan(context: dict[str, Any], *,
                                    fingerprint_policy: str = "balanced",
                                    min_offset: int = -3000,
                                    max_offset: int = 500,
                                    sample_step: int = 15,
                                    warn_ncc: float = 0.50) -> dict[str, Any]:
    """Plan descriptor extraction, a report-only alignment candidate, and visual-review assets."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    data = context.get("manifest_data") or {}
    if data.get("dvd_reference_available") is False:
        raise WorkflowAppError("DVD/LD alignment review is unavailable for an explicit no-DVD episode")
    if (type(min_offset) is not int or type(max_offset) is not int or
            max_offset <= min_offset or max_offset - min_offset > 5000):
        raise WorkflowAppError("alignment offset search range is invalid or exceeds the bounded memory budget")
    if type(sample_step) is not int or sample_step <= 0:
        raise WorkflowAppError("alignment sample step must be a positive integer")
    if not math.isfinite(warn_ncc) or not -1 <= warn_ncc <= 1:
        raise WorkflowAppError("low-correlation marker must be between -1 and 1")
    master = _path(context, "stack_master")
    dvd = _path(context, "dvd")
    manifest = Path(context["manifest"]).resolve(strict=True)
    probe = Path(context["probe"]).resolve(strict=True)
    token = _token()
    master_desc = probe / f"{episode}-master-desc-{token}.npy"
    dvd_desc = probe / f"{episode}-dvd-desc-{token}.npy"
    alignment = probe / f"{episode}-dvd-alignment-{token}.npy"
    diagnostics = probe / f"{episode}-dvd-alignment-{token}.tsv"
    review_dir = probe / f"{episode}-dvd-alignment-review-{token}"
    planned_outputs = [
        master_desc, master_desc.with_suffix(master_desc.suffix + ".meta.json"),
        dvd_desc, dvd_desc.with_suffix(dvd_desc.suffix + ".meta.json"),
        alignment, diagnostics, review_dir,
    ]
    if any(path.exists() or path.is_symlink() for path in planned_outputs):
        raise WorkflowAppError("refusing to reuse an existing DVD/LD alignment review output")
    video = data.get("video") or {}
    required_frames: list[int] = []
    delta = video.get("delta_s")
    if isinstance(delta, (int, float)) and not isinstance(delta, bool) and math.isfinite(float(delta)):
        required_frames.append(round(float(delta) * (30000.0 / 1001.0)))
        duration = video.get("duration_s")
        if (isinstance(duration, (int, float)) and not isinstance(duration, bool) and
                math.isfinite(float(duration)) and float(duration) > 0):
            required_frames.append(round((float(delta) + float(duration)) * (30000.0 / 1001.0)))
    plan = _base_plan(
        context, fingerprint_policy,
        f"dvd-alignment-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "dvd-alignment-review", "value": "candidate-only; explicit visual approval required"},
        {"id": "dvd-alignment-offset-range", "value": [min_offset, max_offset]},
        {"id": "dvd-alignment-sample-step", "value": sample_step},
        {"id": "dvd-alignment-warn-ncc", "value": warn_ncc},
        {"id": "dvd-alignment-output", "value": str(alignment)},
    ]
    master_meta = master_desc.with_suffix(master_desc.suffix + ".meta.json")
    dvd_meta = dvd_desc.with_suffix(dvd_desc.suffix + ".meta.json")
    steps: list[dict[str, Any]] = [
        {"id": "extract-master-descriptors", "stage": "master-descriptors",
         "args": ["--out", str(master_desc)],
         "inputs": [str(manifest), str(master)],
         "outputs": [str(master_desc), str(master_meta)]},
        {"id": "extract-dvd-descriptors-sequentially", "stage": "dvd-descriptors",
         "args": ["--out", str(dvd_desc)],
         "inputs": [str(manifest), str(dvd)],
         "outputs": [str(dvd_desc), str(dvd_meta)]},
    ]
    alignment_args = [
        "--master-desc", str(master_desc), "--dvd-desc", str(dvd_desc),
        "--out", str(alignment), "--report", str(diagnostics),
        "--min-offset", str(min_offset), "--max-offset", str(max_offset),
        "--step", str(sample_step), "--warn-ncc", f"{warn_ncc:g}",
        "--transition-threshold", "20",
    ]
    for frame in required_frames:
        alignment_args.extend(("--required-master-frame", str(frame)))
    steps.append({
        "id": "measure-continuity-constrained-dvd-alignment",
        "stage": "measure-dvd-alignment",
        "args": alignment_args,
        "inputs": [str(manifest), str(master_desc), str(master_meta), str(dvd_desc), str(dvd_meta),
                   str(master), str(dvd)],
        "outputs": [str(alignment), str(diagnostics)],
    })
    steps.append({
        "id": "build-alignment-visual-review-assets",
        "stage": "alignment-review-assets",
        "args": ["--series", series, "--alignment", str(alignment),
                 "--report", str(diagnostics), "--master-desc", str(master_desc),
                 "--dvd-desc", str(dvd_desc), "--out-dir", str(review_dir),
                 "--warn-ncc", f"{warn_ncc:g}", "--transition-threshold", "20",
                 "--min-offset", str(min_offset), "--max-offset", str(max_offset),
                 "--sample-step", str(sample_step)],
        "inputs": [str(manifest), str(master), str(dvd), str(master_desc), str(master_meta),
                   str(dvd_desc), str(dvd_meta), str(alignment), str(diagnostics)],
        "outputs": [str(review_dir)],
    })
    plan["steps"] = steps
    return plan


def build_dvd_alignment_review_decision_plan(
        context: dict[str, Any], index_path: str | Path, decision: str, review_note: str, *,
        expected_index_sha256: str,
        source_rate_acknowledged: bool = False,
        fingerprint_policy: str = "balanced",
        replace_approved: bool = False) -> dict[str, Any]:
    """Create a separate report/manifest step for an explicit visual approve/reject choice."""
    from ldcommon import episode_manifest_path

    decision = str(decision).upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise WorkflowAppError("visual alignment decision must be APPROVED or REJECTED")
    if len(review_note.strip()) < 8:
        raise WorkflowAppError("alignment review note must contain at least 8 characters")
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    probe = Path(context["probe"]).resolve(strict=True)
    manifest = Path(context["manifest"]).resolve(strict=True)
    raw_index = Path(index_path).expanduser()
    if raw_index.is_symlink() or not raw_index.is_file():
        raise WorkflowAppError("alignment review index must be a regular, non-symlink file")
    index = raw_index.resolve(strict=True)
    try:
        index.relative_to(probe)
        payload = json.loads(index.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"alignment review index is missing, malformed, or outside probe: {exc}") from exc
    if (payload.get("schema") != "dvd-alignment-review-assets-v1" or
            payload.get("candidate_is_approval") is not False or
            payload.get("identity") != context["identity"]):
        raise WorkflowAppError("alignment review index is not candidate evidence for this episode")
    if sha256_file(index) != expected_index_sha256:
        raise WorkflowAppError("alignment review index changed after visual adjudication")
    descriptor_summary = payload.get("descriptor_summary") or {}
    source_rate_mismatch = False
    for role in ("master", "dvd"):
        row = descriptor_summary.get(role) or {}
        if (row.get("ffprobe_r_frame_rate") and row.get("ffprobe_avg_frame_rate") and
                row.get("ffprobe_r_frame_rate") != row.get("ffprobe_avg_frame_rate")):
            source_rate_mismatch = True
    if decision == "APPROVED" and source_rate_mismatch and source_rate_acknowledged is not True:
        raise WorkflowAppError("source frame-rate mismatch must be acknowledged before alignment approval")
    if payload.get("manifest_sha256") != sha256_file(manifest):
        raise WorkflowAppError("manifest changed after the alignment visual review; regenerate the candidate")
    alignment = Path(str(payload.get("alignment_path") or "")).resolve(strict=True)
    diagnostics = Path(str(payload.get("diagnostics_path") or "")).resolve(strict=True)
    review_dir = index.parent
    for label, path in (("alignment", alignment), ("diagnostics", diagnostics),
                        ("review assets", review_dir)):
        try:
            path.relative_to(probe)
        except ValueError as exc:
            raise WorkflowAppError(f"{label} must remain in the episode probe folder") from exc
    if (alignment.is_symlink() or diagnostics.is_symlink() or
            not alignment.is_file() or not diagnostics.is_file()):
        raise WorkflowAppError("alignment candidate or diagnostics are missing or unsafe")
    source_map = {"master_source": "stack_master", "dvd_source": "dvd"}
    for key, file_key in source_map.items():
        candidate_source = Path(str(payload.get(key) or "")).resolve(strict=True)
        if candidate_source != _path(context, file_key).resolve(strict=True):
            raise WorkflowAppError(f"review assets {file_key} source does not match the current manifest")
    for key in ("master_descriptors", "master_descriptors_meta",
                "dvd_descriptors", "dvd_descriptors_meta"):
        descriptor = Path(str(payload.get(key) or "")).resolve(strict=True)
        try:
            descriptor.relative_to(probe)
        except ValueError as exc:
            raise WorkflowAppError(f"{key} must remain in the episode probe folder") from exc
    if sha256_file(index) != sha256_file(raw_index):
        raise WorkflowAppError("alignment review index changed while planning")
    video = (context.get("manifest_data") or {}).get("video") or {}
    existing_review = video.get("dvd_alignment_review") or {}
    if decision == "APPROVED" and existing_review.get("status") == "APPROVED" and not replace_approved:
        raise WorkflowAppError("replacing an existing approved alignment requires explicit confirmation")
    token = _token()
    record = probe / f"{episode}-dvd-alignment-review-{decision.lower()}-{token}.json"
    if record.exists() or record.is_symlink():
        raise WorkflowAppError(f"refusing to reuse alignment review record: {record}")
    manifest_sha256 = sha256_file(manifest)
    asset_hash = expected_index_sha256
    args = [
        "--series", series, "--alignment", str(alignment), "--report", str(diagnostics),
        "--assets-index", str(index), "--decision", decision, "--review-note", review_note.strip(),
        "--report-out", str(record), "--confirm-manifest-sha256", manifest_sha256,
        "--confirm-alignment-sha256", str(payload.get("alignment_sha256") or ""),
        "--confirm-diagnostics-sha256", str(payload.get("diagnostics_sha256") or ""),
        "--confirm-assets-sha256", asset_hash,
    ]
    if replace_approved:
        args.append("--replace-approved-alignment")
    if source_rate_acknowledged:
        args.append("--ack-source-rate-mismatch")
    inputs = [
        str(manifest), str(review_dir), str(alignment), str(diagnostics),
        str(Path(str(payload.get("master_source")))),
        str(Path(str(payload.get("dvd_source")))),
        str(Path(str(payload.get("master_descriptors")))),
        str(Path(str(payload.get("master_descriptors_meta")))),
        str(Path(str(payload.get("dvd_descriptors")))),
        str(Path(str(payload.get("dvd_descriptors_meta")))),
    ]
    plan = _base_plan(
        context, fingerprint_policy,
        f"dvd-alignment-review-{decision.lower()}-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "alignment-visual-decision", "value": decision},
        {"id": "alignment-visual-review-note", "value": review_note.strip()},
        {"id": "alignment-visual-approval", "value": (
            "hash-bound visual approval; no retime/audio-origin fields approved"
            if decision == "APPROVED" else "candidate rejected; manifest remains unchanged")},
        {"id": "alignment-review-manifest-sha256", "value": manifest_sha256},
    ]
    outputs = [str(record)]
    if decision == "APPROVED":
        outputs.append(str(episode_manifest_path(episode, context["ld"], series=series)))
    plan["steps"] = [{
        "id": f"record-dvd-alignment-{decision.lower()}",
        "stage": "record-dvd-alignment-review",
        "args": args,
        "inputs": list(dict.fromkeys(inputs)),
        "outputs": outputs,
    }]
    return plan


def build_no_dvd_audio_promotion_plan(context: dict[str, Any], *,
                                      fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Plan the existing explicit no-DVD duration/PAD promotion when all measured inputs exist."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    data = context.get("manifest_data") or {}
    audio, video = data.get("audio") or {}, data.get("video") or {}
    if data.get("dvd_reference_available") is not False or data.get("no_dvd_proceed_approved") is not True:
        raise WorkflowAppError("automatic duration/PAD promotion requires the recorded no-DVD choice")
    if set(data.get("deliverable_tracks") or ["ld"]) != {"ld"}:
        raise WorkflowAppError("DVD-free promotion is only valid for LD stereo delivery")
    if not audio.get("capture") or type(audio.get("pad_frames")) is not int or audio["pad_frames"] < 0:
        raise WorkflowAppError("DVD-free audio promotion requires verified capture and measured pad_frames")
    if (type(video.get("frames")) is not int or video["frames"] <= 0 or
            type(video.get("head_trim")) is not int or video["head_trim"] < 0 or
            type(video.get("tail_trim")) is not int or video["tail_trim"] < 0):
        raise WorkflowAppError(
            "DVD-free promotion requires measured frame count and adjudicated head/tail trims")
    manifest = Path(context["manifest"]).resolve(strict=True)
    native = _path(context, "ld_native")
    project = _path(context, "stack_project")
    probe = Path(context["probe"]).resolve(strict=True)
    audio_root = Path(context["audio_root"]).resolve(strict=True)
    token = _token()
    report = probe / f"{episode}-dvd-free-audio-timing-{token}.json"
    corrected = audio_root / f"{episode}-LD20-dvd-free-{token}-corrected.flac"
    synced = audio_root / f"{episode}-LD20-dvd-free-{token}-synced.flac"
    for path in (report, corrected, synced):
        if path.exists() or path.is_symlink():
            raise WorkflowAppError(f"refusing to reuse DVD-free audio output: {path}")
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    plan = _base_plan(
        context, fingerprint_policy,
        f"no-dvd-audio-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "no-dvd-route-approval", "value": "explicitly recorded before this plan"},
        {"id": "no-dvd-audio-promotion", "value": "duration/PAD; existing ±1-frame endpoint gate"},
        {"id": "no-dvd-audio-manifest-sha256", "value": manifest_sha256},
    ]
    plan["steps"] = [{
        "id": "promote-measured-no-dvd-audio",
        "stage": "audio-promote-no-dvd",
        "report_out": str(report),
        "args": ["--corrected-out", str(corrected), "--synced-out", str(synced),
                 "--confirm-manifest-sha256", manifest_sha256],
        "inputs": [str(native), str(project)],
        "outputs": [str(report), str(corrected), str(synced), str(manifest)],
    }]
    return plan


def build_ld_synced_plan(context: dict[str, Any], *,
                         fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build the sample-accurate LD sync stage only from an approved measured origin."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    data = context.get("manifest_data") or {}
    audio, video = data.get("audio") or {}, data.get("video") or {}
    skip_s = audio.get("skip_s")
    if isinstance(skip_s, bool) or not isinstance(skip_s, (int, float)) or not math.isfinite(skip_s) or skip_s < 0:
        raise WorkflowAppError("LD sync requires human-approved measured audio.skip_s; no geometric default is applied")
    if type(video.get("frames")) is not int or video["frames"] <= 0:
        raise WorkflowAppError("LD sync requires measured positive video.frames")
    duration = video.get("duration_s")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise WorkflowAppError("LD sync requires measured video.duration_s")
    manifest = Path(context["manifest"]).resolve(strict=True)
    corrected = _path(context, "ld_corrected")
    synced = _path(context, "ld_synced", must_exist=False)
    if synced.exists() or synced.is_symlink():
        raise WorkflowAppError(f"refusing to overwrite existing sample-synced LD audio: {synced}")
    plan = _base_plan(
        context, fingerprint_policy,
        f"build-ld-synced-{series.lower()}-{episode.lower()}-{_token()[-8:]}")
    plan["decisions"] = [
        {"id": "approved-audio-skip-s", "value": skip_s},
        {"id": "target-video-frames", "value": video["frames"]},
        {"id": "target-video-duration-s", "value": duration},
        {"id": "sync-promotion", "value": "sample-accurate output; no manifest mutation"},
    ]
    plan["steps"] = [{
        "id": "build-sample-accurate-ld-synced-audio", "stage": "build-ld-synced",
        "inputs": [str(manifest), str(corrected)],
        "outputs": [str(synced)],
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


def build_cadence_evidence_plan(context: dict[str, Any], *,
                                fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Plan the complete Windows TFM->motion->map->machine-summary->visual-review chain."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    if str(context.get("platform") or "").casefold() not in ("windows", "win32"):
        raise WorkflowAppError("automated full-side TFM cadence evidence is currently Windows-only")
    data = context.get("manifest_data") or {}
    audio, video, stack = data.get("audio") or {}, data.get("video") or {}, data.get("stack") or {}
    pad = audio.get("pad_frames")
    head, tail = video.get("head_trim"), video.get("tail_trim")
    trim_review = video.get("trim_review") or {}
    if type(pad) is not int or pad < 0 or not audio.get("capture"):
        raise WorkflowAppError("cadence evidence requires verified capture and measured audio.pad_frames")
    selected = str(audio.get("capture"))
    provenance = audio.get("efm_provenance") or {}
    selected_provenance = provenance.get(selected) if isinstance(provenance, dict) else None
    provenance_entry = (selected_provenance[-1] if isinstance(selected_provenance, list) and
                        selected_provenance and isinstance(selected_provenance[-1], dict) else {})
    selection = audio.get("capture_selection") or {}
    if (audio.get("efm_provenance_required") is not True or
            not isinstance(selection, dict) or
            not isinstance(selected_provenance, list) or not selected_provenance or
            selection.get("capture") != selected or
            selection.get("verified_provenance_sha256") != provenance_entry.get("report_sha256") or
            selection.get("alignment_map") != data.get("alignment_map") or
            len(str(selection.get("selection_note") or "").strip()) < 8 or
            review.get("capture") != selected or
            review.get("efm_provenance_sha256") != provenance_entry.get("report_sha256")):
        raise WorkflowAppError("cadence evidence requires the current explicitly selected Orc provenance")
    selected_capture = str(audio.get("capture"))
    if (audio.get("efm_provenance_required") is not True or
            not isinstance((audio.get("efm_provenance") or {}).get(selected_capture), list) or
            not (audio.get("efm_provenance") or {}).get(selected_capture)):
        raise WorkflowAppError("cadence evidence requires the selected capture's verified Orc provenance")
    if type(stack.get("frames")) is not int or stack["frames"] <= pad:
        raise WorkflowAppError("cadence evidence requires measured stack.frames greater than audio.pad_frames")
    if (type(head) is not int or head < 0 or type(tail) is not int or tail < 0 or
            trim_review.get("status") != "APPROVED" or
            trim_review.get("head_trim") != head or trim_review.get("tail_trim") != tail):
        raise WorkflowAppError(
            "cadence evidence requires head_trim/tail_trim values matching an explicit visual trim-review marker")
    manifest = Path(context["manifest"]).resolve(strict=True)
    source = _path(context, "stack_master")
    native = _path(context, "ld_native")
    probe = Path(context["probe"]).resolve(strict=True)
    if sha256_file(source) != trim_review.get("stack_master_sha256"):
        raise WorkflowAppError("stack master changed after visual trim review; review the boundaries again")
    if sha256_file(native) != trim_review.get("native_audio_sha256"):
        raise WorkflowAppError("selected native audio changed after trim review; reconcile the audio evidence")
    if trim_review.get("audio_pad_frames") != pad:
        raise WorkflowAppError("audio.pad_frames changed after visual trim review")
    additional_inputs: list[str] = []
    if data.get("dvd_reference_available") is True:
        alignment_review = video.get("dvd_alignment_review") or {}
        alignment = _path(context, "align_npy")
        if (alignment_review.get("status") != "APPROVED" or
                sha256_file(alignment) != alignment_review.get("sha256") or
                trim_review.get("alignment_sha256") != alignment_review.get("sha256")):
            raise WorkflowAppError("DVD-backed cadence requires the same currently approved alignment used for trim review")
        assets_raw = str(alignment_review.get("review_assets_path") or "")
        assets_path = _alignment_review_path(context, assets_raw)
        if (assets_path is None or not assets_path.is_file() or
                sha256_file(assets_path) != alignment_review.get("review_assets_sha256") or
                trim_review.get("review_assets_sha256") != alignment_review.get("review_assets_sha256")):
            raise WorkflowAppError("DVD-backed cadence requires current paired boundary review assets")
        additional_inputs.extend((str(alignment), str(assets_path)))
    token = _token()
    avs_root = _path(context, "avs_encode", must_exist=False)
    assert avs_root is not None
    avs_dir = avs_root.parent
    avs_out = avs_dir / f"{episode}-full-side-tfm-{token}.avs"
    review_avs = avs_dir / f"{episode}-tfm-review-{token}.avs"
    tfm_log = probe / f"{episode}-full-side-tfm-{token}.log"
    decode_log = probe / f"{episode}-full-side-decode-{token}.log"
    verification = probe / f"{episode}-cadence-coverage-{token}.json"
    motion = probe / f"{episode}-motion-trace-{token}.txt"
    map_out = avs_dir / f"{episode}-cadence-map-{token}.avs"
    ps_summary = probe / f"{episode}-cadence-summary-{token}.txt"
    di_map = probe / f"{episode}-di-map-{token}.txt"
    cadence_tsv = probe / f"{episode}-full-cadence-{token}.tsv"
    metrics = probe / f"{episode}-cadence-metrics-{token}.json"
    review_dir = probe / f"{episode}-cadence-visual-review-{token}"
    outputs = [avs_out, review_avs, tfm_log, decode_log, verification,
               motion, map_out, ps_summary, di_map, cadence_tsv, metrics, review_dir]
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise WorkflowAppError("refusing to reuse any cadence evidence or review output")
    plan = _base_plan(
        context, fingerprint_policy,
        f"cadence-evidence-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "cadence-measurement-source", "value": "full-side TFM log, contiguous and frame-count verified"},
        {"id": "cadence-review-status", "value": "candidate only until visual intervals are adjudicated"},
        {"id": "cadence-audio-pad-frames", "value": pad},
        {"id": "cadence-head-trim-output-frames", "value": head},
        {"id": "cadence-tail-trim-output-frames", "value": tail},
        {"id": "cadence-map-output", "value": str(map_out)},
    ]
    steps: list[dict[str, Any]] = [
        {
            "id": "generate-and-verify-full-side-tfm",
            "stage": "cadence-evidence",
            "args": ["--series", series, "--avs-out", str(avs_out),
                     "--review-avs-out", str(review_avs), "--tfm-log-out", str(tfm_log),
                     "--decode-log-out", str(decode_log), "--report-out", str(verification)],
            "inputs": [str(manifest), str(source), str(native), *additional_inputs],
            "outputs": [str(avs_out), str(review_avs), str(tfm_log), str(decode_log), str(verification)],
        },
        {
            "id": "measure-pad-aligned-source-motion",
            "stage": "motion-trace",
            "args": ["--out", str(motion), "--trim-head", str(pad)],
            "inputs": [str(manifest), str(source)],
            "outputs": [str(motion)],
        },
        {
            "id": "build-deterministic-cadence-map",
            "stage": "build-map",
            "args": ["-Log", str(tfm_log), "-Source", str(source), "-Motion", str(motion),
                     "-MapOut", str(map_out), "-SumOut", str(ps_summary),
                     "-DiOut", str(di_map), "-CadOut", str(cadence_tsv),
                     "-SourcePad", str(pad), "-TrimStart", str(head), "-TrimEnd", str(tail)],
            "inputs": [str(manifest), str(source), str(tfm_log), str(verification), str(motion)],
            "outputs": [str(map_out), str(ps_summary), str(di_map), str(cadence_tsv)],
        },
        {
            "id": "analyze-verified-cadence-log",
            "stage": "cadence-analysis",
            "args": ["--series", series, "--tfm-log", str(tfm_log),
                     "--motion", str(motion), "--verification-report", str(verification),
                     "--map", str(map_out), "--di-map", str(di_map),
                     "--out", str(metrics)],
            "inputs": [str(manifest), str(tfm_log), str(verification), str(motion),
                       str(map_out), str(di_map), str(cadence_tsv)],
            "outputs": [str(metrics)],
        },
        {
            "id": "build-visual-cadence-review-assets",
            "stage": "cadence-review-assets",
            "args": ["--series", series, "--metrics-report", str(metrics),
                     "--verification-report", str(verification),
                     "--review-avs", str(review_avs), "--out-dir", str(review_dir)],
            "inputs": [str(manifest), str(metrics), str(verification), str(review_avs),
                       str(map_out), str(source)],
            "outputs": [str(review_dir)],
        },
    ]
    plan["steps"] = steps
    return plan


def build_efm_decode_plan(context: dict[str, Any], captures: list[dict[str, Any]], *,
                          fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Build one serialized Orc/EFMSink decode-and-provenance stage for eligible captures."""
    episode = str(context["episode"]).upper()
    series = str(context["series"]).upper()
    if not captures:
        raise WorkflowAppError("there are no unverified captures with raw EFM sources to decode")
    manifest = Path(context["manifest"]).resolve(strict=True)
    project = _path(context, "stack_project")
    downloads = Path(context["downloads"]).resolve(strict=True)
    audio_root = Path(context["audio_root"]).resolve(strict=True)
    work_root = Path(context["work_root"]).resolve(strict=False)
    instance = Path(context["instance"]).resolve(strict=True)
    token = _token()
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    labels: set[str] = set()
    inputs: list[Path] = [project]
    outputs: list[Path] = []
    args = ["--run-token", token, "--confirm-manifest-sha256", manifest_sha256]
    for capture in captures:
        label = str(capture.get("label") or "")
        efm_name = str(capture.get("efm_file") or "")
        if not label or label in labels:
            raise WorkflowAppError("EFM decode captures must have unique manifest labels")
        if not efm_name:
            raise WorkflowAppError(f"capture {label} has no declared raw EFM source")
        labels.add(label)
        names = (("tbc_file", "tbc"), ("json_file", "TBC metadata"), ("efm_file", "EFM source"))
        for key, role in names:
            name = str(capture.get(key) or "")
            relative = Path(name)
            if (not name or relative.is_absolute() or len(relative.parts) != 1 or
                    any(part in ("", ".", "..") for part in relative.parts)):
                raise WorkflowAppError(f"capture {label} has an unsafe {role} filename")
            path = downloads / relative
            if path.is_symlink() or not path.is_file():
                raise WorkflowAppError(f"capture {label} {role} is missing or unsafe: {path}")
            inputs.append(path.resolve(strict=True))
        artifacts = decode_artifact_paths(audio_root, instance, episode, label, token)
        for path in artifacts.values():
            if path.exists() or path.is_symlink():
                raise WorkflowAppError(
                    f"refusing to plan an EFM overwrite; preserve/reconcile this output first: {path}")
            outputs.append(path)
        if efm_name.casefold().endswith(".efm.7z"):
            retained_efm = work_root / "efm-decodes" / episode / f"{label}-{token}.efm"
            if retained_efm.exists() or retained_efm.is_symlink():
                raise WorkflowAppError(
                    f"refusing to overwrite preserved raw EFM extraction: {retained_efm}")
            outputs.append(retained_efm)
        args.extend(("--capture", label))
    outputs.append(manifest)
    step_inputs = list(dict.fromkeys(str(path.resolve(strict=True)) for path in inputs))
    step_outputs = list(dict.fromkeys(str(path.resolve(strict=False)) for path in outputs))
    plan = _base_plan(
        context, fingerprint_policy,
        f"decode-efm-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "efm-decode-captures", "value": sorted(labels)},
        {"id": "efm-decode-mode", "value": "Orc EFMSink audio; standard decoder defaults; report enabled"},
        {"id": "efm-decode-manifest-sha256", "value": manifest_sha256},
        {"id": "efm-decode-output-policy", "value": "new per-capture WAV/report; no existing output overwritten"},
    ]
    plan["steps"] = [{
        "id": "decode-and-verify-efm-captures", "stage": "decode-efm-captures",
        "args": args, "inputs": step_inputs, "outputs": step_outputs,
    }]
    return plan


def build_video_trim_review_plan(
        context: dict[str, Any], head_trim: int, tail_trim: int, review_note: str, *,
        fingerprint_policy: str = "balanced", replace_existing: bool = False,
        review_assets_path: str | Path | None = None,
        review_assets_sha256: str = "") -> dict[str, Any]:
    """Record human-measured output-frame trims after a visual boundary review."""
    data = context.get("manifest_data") or {}
    audio = data.get("audio") or {}
    if type(audio.get("pad_frames")) is not int or audio["pad_frames"] < 0 or not audio.get("capture"):
        raise WorkflowAppError("visual trim review requires verified capture and measured audio.pad_frames")
    selected = str(audio.get("capture"))
    provenance = audio.get("efm_provenance") or {}
    if (audio.get("efm_provenance_required") is not True or
            not isinstance(provenance.get(selected), list) or not provenance.get(selected) or
            not isinstance(audio.get("capture_selection"), dict)):
        raise WorkflowAppError("visual trim review requires the explicitly selected, provenance-verified EFM capture")
    if type(head_trim) is not int or head_trim < 0 or type(tail_trim) is not int or tail_trim < 0:
        raise WorkflowAppError("head/tail trims must be nonnegative output-frame counts")
    if len(review_note.strip()) < 8:
        raise WorkflowAppError("visual trim review requires a human note of at least 8 characters")
    video = data.get("video") or {}
    existing = video.get("trim_review") or {}
    if (video.get("head_trim") is not None or video.get("tail_trim") is not None) and not replace_existing:
        raise WorkflowAppError("replacing existing trims requires explicit confirmation")
    manifest = Path(context["manifest"]).resolve(strict=True)
    source = _path(context, "stack_master")
    native = _path(context, "ld_native")
    probe = Path(context["probe"]).resolve(strict=True)
    dvd = data.get("dvd_reference_available") is True
    if dvd:
        alignment = video.get("dvd_alignment_review") or {}
        current_alignment = _path(context, "align_npy")
        if not (alignment.get("status") == "APPROVED" and
                _alignment_review_path(context, alignment.get("path")) == current_alignment.resolve(strict=False) and
                sha256_file(current_alignment) == alignment.get("sha256")):
            raise WorkflowAppError("DVD trim review requires the current visually approved alignment")
        if not review_assets_path:
            raise WorkflowAppError("paired head/tail visual samples are required for DVD trim review")
        raw_assets = Path(review_assets_path).expanduser()
        if raw_assets.is_symlink() or not raw_assets.is_file():
            raise WorkflowAppError("paired visual review index is missing or unsafe")
        assets = raw_assets.resolve(strict=True)
        try:
            assets.relative_to(probe)
        except ValueError as exc:
            raise WorkflowAppError("paired visual review index must remain in the episode probe folder") from exc
        if sha256_file(assets) != review_assets_sha256 or review_assets_sha256 != alignment.get("review_assets_sha256"):
            raise WorkflowAppError("paired visual review index changed after approval")
    else:
        if data.get("no_dvd_proceed_approved") is not True:
            raise WorkflowAppError("no-DVD trim review requires explicit no-DVD route approval")
        assets = None
    token = _token()
    report = probe / f"{context['episode']}-trim-review-{token}.json"
    if report.exists() or report.is_symlink():
        raise WorkflowAppError(f"refusing to reuse trim-review report: {report}")
    manifest_sha = sha256_file(manifest)
    args = [
        "--series", str(context["series"]), "--head-trim", str(head_trim),
        "--tail-trim", str(tail_trim), "--review-note", review_note.strip(),
        "--confirm-manifest-sha256", manifest_sha,
        "--confirm-source-sha256", sha256_file(source),
        "--confirm-audio-sha256", sha256_file(native),
        "--report-out", str(report), "--confirm-visual-review",
    ]
    if assets:
        args.extend(("--review-assets", str(assets),
                     "--confirm-review-assets-sha256", review_assets_sha256))
    if replace_existing:
        args.append("--replace-existing-trims")
    plan = _base_plan(
        context, fingerprint_policy,
        f"record-trim-review-{context['series'].lower()}-{context['episode'].lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "visual-trim-review",
         "value": "explicit operator measurement in output-frame domain; no conversion inferred"},
        {"id": "video-head-trim-output-frames", "value": head_trim},
        {"id": "video-tail-trim-output-frames", "value": tail_trim},
        {"id": "visual-trim-review-note", "value": review_note.strip()},
        {"id": "visual-trim-review-manifest-sha256", "value": manifest_sha},
    ]
    inputs = [str(manifest), str(source), str(native)]
    if assets:
        inputs.append(str(assets))
        inputs.append(str(Path(review_assets_path).parent))
        if data.get("dvd_reference_available") is True:
            inputs.append(str(_path(context, "align_npy")))
    plan["steps"] = [{
        "id": "record-human-reviewed-video-trims",
        "stage": "record-video-trim-review",
        "args": args,
        "inputs": list(dict.fromkeys(inputs)),
        "outputs": [str(manifest), str(report)],
    }]
    return plan


def build_cadence_evidence_plan(context: dict[str, Any], *,
                                fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Plan verified full-side TFM coverage, motion/map analysis, and visual samples."""
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    data = context.get("manifest_data") or {}
    video, audio, stack = data.get("video") or {}, data.get("audio") or {}, data.get("stack") or {}
    if str(context.get("platform") or "").casefold() not in ("windows", "win32"):
        raise WorkflowAppError("automated full-side AviSynth/TFM cadence evidence is Windows-only")
    pad = audio.get("pad_frames")
    head, tail = video.get("head_trim"), video.get("tail_trim")
    review = video.get("trim_review") or {}
    if type(pad) is not int or pad < 0 or not audio.get("capture"):
        raise WorkflowAppError("cadence evidence requires verified capture and measured audio.pad_frames")
    if type(stack.get("frames")) is not int or stack["frames"] <= pad:
        raise WorkflowAppError("cadence evidence requires measured stack.frames greater than pad_frames")
    if (type(head) is not int or head < 0 or type(tail) is not int or tail < 0 or
            review.get("status") != "APPROVED" or
            review.get("head_trim") != head or review.get("tail_trim") != tail or
            review.get("audio_pad_frames") != pad):
        raise WorkflowAppError(
            "cadence evidence requires head/tail output-frame trims matching a current visual trim-review marker")
    if data.get("dvd_reference_available") is False and data.get("no_dvd_proceed_approved") is not True:
        raise WorkflowAppError("no-DVD cadence evidence requires the explicit no-DVD route choice")
    manifest = Path(context["manifest"]).resolve(strict=True)
    source, native = _path(context, "stack_master"), _path(context, "ld_native")
    if (sha256_file(source) != review.get("stack_master_sha256") or
            sha256_file(native) != review.get("native_audio_sha256")):
        raise WorkflowAppError("stack master or native audio changed after trim adjudication")
    inputs: list[str] = [str(manifest), str(source), str(native)]
    if data.get("dvd_reference_available") is True:
        alignment_review = video.get("dvd_alignment_review") or {}
        alignment = _path(context, "align_npy")
        marker_path = _alignment_review_path(context, alignment_review.get("path"))
        if (alignment_review.get("status") != "APPROVED" or marker_path is None or
                marker_path.resolve(strict=True) != alignment.resolve(strict=True) or
                sha256_file(alignment) != alignment_review.get("sha256") or
                review.get("alignment_sha256") != alignment_review.get("sha256")):
            raise WorkflowAppError("DVD-backed cadence requires the same current approved alignment used in trim review")
        assets_path = _alignment_review_path(context, alignment_review.get("review_assets_path"))
        if (assets_path is None or not assets_path.is_file() or
                sha256_file(assets_path) != alignment_review.get("review_assets_sha256") or
                review.get("review_assets_sha256") != alignment_review.get("review_assets_sha256")):
            raise WorkflowAppError("DVD-backed cadence requires current paired boundary visual assets")
        inputs.extend((str(alignment), str(assets_path)))
    probe = Path(context["probe"]).resolve(strict=True)
    avs_reference = _path(context, "avs_encode", must_exist=False)
    assert avs_reference is not None
    avs_root = avs_reference.parent
    token = _token()
    outputs = {
        "tfm_avs": probe / f"{episode}-cadence-tfm-{token}.avs",
        "review_avs": probe / f"{episode}-cadence-review-{token}.avs",
        "tfm_log": probe / f"{episode}-cadence-tfm-{token}.log",
        "decode_log": probe / f"{episode}-cadence-decode-{token}.log",
        "coverage": probe / f"{episode}-cadence-coverage-{token}.json",
        "motion": probe / f"{episode}-cadence-motion-{token}.txt",
        "motion_log": probe / f"{episode}-cadence-motion-decode-{token}.log",
        "motion_report": probe / f"{episode}-cadence-motion-{token}.json",
        "map": avs_root / f"{episode}-cadence-map-{token}.avs",
        "summary": probe / f"{episode}-cadence-summary-{token}.txt",
        "di_map": probe / f"{episode}-cadence-di-map-{token}.txt",
        "cadence": probe / f"{episode}-cadence-full-{token}.tsv",
        "metrics": probe / f"{episode}-cadence-metrics-{token}.json",
        "review_dir": probe / f"{episode}-cadence-review-{token}",
    }
    if any(path.exists() or path.is_symlink() for path in outputs.values()):
        raise WorkflowAppError("refusing to reuse cadence evidence outputs")
    steps: list[dict[str, Any]] = [
        {
            "id": "generate-verify-full-side-tfm",
            "stage": "cadence-evidence",
            "args": ["--series", series, "--avs-out", str(outputs["tfm_avs"]),
                     "--review-avs-out", str(outputs["review_avs"]),
                     "--tfm-log-out", str(outputs["tfm_log"]),
                     "--decode-log-out", str(outputs["decode_log"]),
                     "--report-out", str(outputs["coverage"])],
            "inputs": inputs,
            "outputs": [str(outputs[key]) for key in
                        ("tfm_avs", "review_avs", "tfm_log", "decode_log", "coverage")],
        },
        {
            "id": "measure-pad-aligned-source-motion",
            "stage": "motion-trace",
            "args": ["--out", str(outputs["motion"]), "--trim-head", str(pad),
                     "--log-out", str(outputs["motion_log"]),
                     "--report-out", str(outputs["motion_report"])],
            "inputs": [str(manifest), str(source)],
            "outputs": [str(outputs["motion"]), str(outputs["motion_log"]),
                        str(outputs["motion_report"])],
        },
        {
            "id": "build-deterministic-cadence-map",
            "stage": "build-map",
            "args": ["-Log", str(outputs["tfm_log"]), "-Source", str(source),
                     "-Motion", str(outputs["motion"]), "-MapOut", str(outputs["map"]),
                     "-SumOut", str(outputs["summary"]), "-DiOut", str(outputs["di_map"]),
                     "-CadOut", str(outputs["cadence"]), "-SourcePad", str(pad),
                     "-TrimStart", str(head), "-TrimEnd", str(tail)],
            "inputs": [str(manifest), str(source), str(outputs["tfm_log"]),
                       str(outputs["coverage"]), str(outputs["motion"]),
                       str(outputs["motion_report"])],
            "outputs": [str(outputs[key]) for key in ("map", "summary", "di_map", "cadence")],
        },
        {
            "id": "analyze-verified-cadence-log",
            "stage": "cadence-analysis",
            "args": ["--series", series, "--tfm-log", str(outputs["tfm_log"]),
                     "--motion", str(outputs["motion"]),
                     "--motion-report", str(outputs["motion_report"]),
                     "--verification-report", str(outputs["coverage"]),
                     "--map", str(outputs["map"]), "--di-map", str(outputs["di_map"]),
                     "--out", str(outputs["metrics"])],
            "inputs": [str(manifest), str(outputs["tfm_log"]), str(outputs["motion"]),
                       str(outputs["motion_report"]),
                       str(outputs["coverage"]), str(outputs["map"]), str(outputs["di_map"]),
                       str(outputs["cadence"])],
            "outputs": [str(outputs["metrics"])],
        },
        {
            "id": "build-cadence-visual-review-assets",
            "stage": "cadence-review-assets",
            "args": ["--series", series, "--metrics-report", str(outputs["metrics"]),
                     "--verification-report", str(outputs["coverage"]),
                     "--review-avs", str(outputs["review_avs"]), "--map", str(outputs["map"]),
                     "--summary", str(outputs["summary"]),
                     "--out-dir", str(outputs["review_dir"])],
            "inputs": [str(manifest), str(source), str(outputs["metrics"]),
                       str(outputs["coverage"]), str(outputs["review_avs"]), str(outputs["map"]),
                       str(outputs["summary"]), str(outputs["tfm_log"]),
                       str(outputs["motion"]), str(outputs["motion_report"]),
                       str(outputs["di_map"])],
            "outputs": [str(outputs["review_dir"])],
        },
    ]
    plan = _base_plan(
        context, fingerprint_policy,
        f"cadence-evidence-{series.lower()}-{episode.lower()}-{token[-8:]}")
    plan["decisions"] = [
        {"id": "cadence-trim-review", "value": "APPROVED; values are measured output frames"},
        {"id": "cadence-pad-frames", "value": pad},
        {"id": "cadence-head-trim", "value": head},
        {"id": "cadence-tail-trim", "value": tail},
        {"id": "cadence-final-encode", "value": "blocked until visual cadence approval is recorded"},
    ]
    plan["steps"] = steps
    return plan


def build_cadence_review_decision_plan(
        context: dict[str, Any], review_index: str | Path, decision: str,
        interval_decisions: dict[str, str], review_note: str,
        expected_index_sha256: str, source_rate_acknowledged: bool, *,
        fingerprint_policy: str = "balanced") -> dict[str, Any]:
    """Plan a hash-confirmed visual cadence approval/rejection and metric recorder."""
    from ldcommon import episode_manifest_path

    decision = str(decision).upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise WorkflowAppError("cadence decision must be APPROVED or REJECTED")
    if len(review_note.strip()) < 8:
        raise WorkflowAppError("cadence review note must contain at least 8 characters")
    episode, series = str(context["episode"]).upper(), str(context["series"]).upper()
    probe = Path(context["probe"]).resolve(strict=True)
    manifest = Path(context["manifest"]).resolve(strict=True)
    raw_index = Path(review_index).expanduser()
    if raw_index.is_symlink() or not raw_index.is_file():
        raise WorkflowAppError("cadence review index must be a regular non-symlink file")
    index = raw_index.resolve(strict=True)
    try:
        index.relative_to(probe)
        if sha256_file(index) != expected_index_sha256:
            raise WorkflowAppError("cadence visual evidence changed after adjudication")
        payload = json.loads(index.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowAppError(f"cadence review index is invalid or outside the probe root: {exc}") from exc
    if (payload.get("schema") != "cadence-review-assets-v1" or
            payload.get("identity") != context["identity"] or
            payload.get("candidate_is_approval") is not False):
        raise WorkflowAppError("cadence review index is not candidate evidence for this episode")
    if payload.get("manifest_sha256") != sha256_file(manifest):
        raise WorkflowAppError("manifest changed since cadence visuals were generated")
    rate_info = payload.get("source_rate_info") or {}
    rate_mismatch = False
    for role in ("tfm", "motion"):
        row = rate_info.get(role) or {}
        if (row.get("r_frame_rate") and row.get("avg_frame_rate") and
                row.get("r_frame_rate") != row.get("avg_frame_rate")):
            rate_mismatch = True
    tfm_rate = (rate_info.get("tfm") or {}).get("r_frame_rate")
    motion_rate = (rate_info.get("motion") or {}).get("r_frame_rate")
    if tfm_rate and motion_rate and tfm_rate != motion_rate:
        rate_mismatch = True
    if decision == "APPROVED" and rate_mismatch and source_rate_acknowledged is not True:
        raise WorkflowAppError("source frame-rate mismatch must be explicitly acknowledged")
    metric_path = Path(str(payload.get("metrics_path") or "")).resolve(strict=True)
    verification = Path(str(payload.get("verification_report_path") or "")).resolve(strict=True)
    for label, path in (("metrics", metric_path), ("coverage report", verification)):
        try:
            path.relative_to(probe)
        except ValueError as exc:
            raise WorkflowAppError(f"cadence {label} must remain in the episode probe folder") from exc
    expected_ids = {str(row.get("id")) for row in payload.get("review_intervals", [])}
    if decision == "APPROVED" and set(interval_decisions) != expected_ids:
        raise WorkflowAppError("visual approval must cover every generated cadence interval")
    if not set(interval_decisions).issubset(expected_ids):
        raise WorkflowAppError("visual decisions include an unknown cadence interval")
    if any(value not in ("accepted", "unresolved") for value in interval_decisions.values()):
        raise WorkflowAppError("cadence interval decisions must be accepted or unresolved")
    if decision == "APPROVED" and (
            not expected_ids or any(value != "accepted" for value in interval_decisions.values())):
        raise WorkflowAppError("cadence approval requires every visual interval to be explicitly acceptable")
    video = (context.get("manifest_data") or {}).get("video") or {}
    trim_review = video.get("trim_review") or {}
    if trim_review.get("status") != "APPROVED":
        raise WorkflowAppError("cadence review cannot be recorded without an approved head/tail trim review")
    output = probe / f"{episode}-cadence-visual-decision-{decision.lower()}-{_token()}.json"
    if output.exists() or output.is_symlink():
        raise WorkflowAppError("refusing to reuse cadence visual decision output")
    manifest_sha = sha256_file(manifest)
    args = [
        "--series", series, "--review-index", str(index), "--decision", decision,
        "--review-note", review_note.strip(), "--confirm-manifest-sha256", manifest_sha,
        "--confirm-index-sha256", sha256_file(index), "--report-out", str(output),
    ]
    for key, value in interval_decisions.items():
        args.extend(("--reviewed-interval" if value == "accepted" else "--unresolved-interval", key))
    if decision == "APPROVED":
        args.append("--confirm-visual-review")
        if source_rate_acknowledged:
            args.append("--ack-source-rate-mismatch")
    plan = _base_plan(
        context, fingerprint_policy,
        f"cadence-review-{decision.lower()}-{series.lower()}-{episode.lower()}-{_token()[-8:]}")
    plan["decisions"] = [
        {"id": "cadence-visual-decision", "value": decision},
        {"id": "cadence-visual-review-note", "value": review_note.strip()},
        {"id": "cadence-visual-interval-decisions", "value": interval_decisions},
        {"id": "cadence-review-manifest-sha256", "value": manifest_sha},
    ]
    outputs = [str(output)]
    if decision == "APPROVED":
        outputs.append(str(episode_manifest_path(episode, context["ld"], series=series)))
    plan["steps"] = [{
        "id": f"record-cadence-visual-{decision.lower()}",
        "stage": "record-cadence-review",
        "args": args,
        "inputs": [str(manifest), str(index.parent), str(index), str(metric_path), str(verification)],
        "outputs": outputs,
    }]
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
    """Plan the full report-only audio evidence suite or individual diagnostics."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any],
                 on_plan: Any, fingerprint_policy: str = "balanced"):
        self.parent = parent
        self.context = context
        self.on_plan = on_plan
        self.fingerprint_policy = fingerprint_policy
        self.window: tk.Toplevel | None = None
        self.kind = tk.StringVar(value="audio-evidence-suite")
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
        ttk.Radiobutton(mode, text="All currently available timing evidence",
                        variable=self.kind, value="audio-evidence-suite").pack(anchor="w")

        form = ttk.LabelFrame(body, text="Explicit parameters", padding=8)
        form.pack(fill="both", expand=True)
        fields = [
            ("skip_s", "Provisional window-center skip (s)", "",
             "Blank uses measured DELTA - pad/FPS; only centers correlation windows."),
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
            elif self.kind.get() == "pad-occupancy":
                offsets = [float(value) for value in re.split(
                    r"[\s,]+", self.values["control_offsets"].get().strip()) if value]
                plan = build_pad_audio_occupancy_plan(
                    self.context, window_ms=float(self.values["window_ms"].get()),
                    control_offsets_s=offsets,
                    minimum_controls=int(self.values["minimum_controls"].get()),
                    fingerprint_policy=self.fingerprint_policy)
                suffix = "pad-audio-occupancy-plan.json"
                scope = "report-only PAD audio occupancy diagnostic"
            else:
                plan = build_audio_evidence_plan(
                    self.context, fingerprint_policy=self.fingerprint_policy)
                suffix = "audio-evidence-plan.json"
                scope = "report-only audio evidence bundle"
        except (WorkflowAppError, TypeError, ValueError) as exc:
            messagebox.showerror("Diagnostic plan not built", str(exc), parent=self.window)
            return
        name = f"{plan['series']}-{plan['episode']}-{suffix}"
        if self.on_plan(plan, name, scope):
            self.window.destroy()


class AudioTimingEvidenceWindow:
    """In-app summary of immutable audio timing evidence; never edits the manifest."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any],
                 reports: list[str | Path], deferred: list[str] | None = None):
        self.parent = parent
        self.context = context
        self.reports = [Path(path) for path in reports]
        self.deferred = deferred or []
        self.window: tk.Toplevel | None = None
        self.rows: list[tuple[str, str, str, str, str, str]] = []
        self.load_error: str | None = None
        self._load()

    @staticmethod
    def _number(value: Any, digits: int = 2, suffix: str = "") -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "unavailable"
        return f"{value:.{digits}f}{suffix}"

    def _load(self) -> None:
        for path in self.reports:
            if path.suffix.casefold() != ".json" or not path.is_file():
                continue
            try:
                report = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.load_error = f"Could not read an audio measurement result: {exc}"
                continue
            schema = report.get("schema")
            if schema == "fade-midpoint-fit-v1":
                for key, label in (("fit_A_master_vs_wav", "Primary: master-video fades vs LD audio"),
                                   ("fit_B_dvd_vs_wav", "Corroboration: DVD-video fades vs LD audio")):
                    fit = report.get(key) or {}
                    if fit.get("error"):
                        self.rows.append((label, str(fit.get("n", 0)), "unavailable",
                                          "unavailable", "unavailable", str(fit["error"])))
                    else:
                        self.rows.append((
                            label, str(fit.get("n", 0)),
                            self._number(fit.get("asetrate_from_b"), 2, " Hz"),
                            self._number(fit.get("asetrate_from_theil_sen"), 2, " Hz"),
                            self._number(fit.get("asetrate_stderr_hz"), 2, " Hz"),
                            f"residual SD {self._number(fit.get('residual_sd_s'), 3, ' s')}; report-only"))
            elif schema == "dvd-picture-audio-rate-summary-v2":
                ols, robust = report.get("ols") or {}, report.get("theil_sen") or {}
                loo = robust.get("leave_one_window_out_rate_ppm_range") or []
                interval = (f"{self._number(loo[0], 1)}..{self._number(loo[1], 1)} ppm"
                            if len(loo) == 2 else "unavailable")
                self.rows.append((
                    "Corroboration: DVD audio vs picture map",
                    f"{report.get('accepted_window_count', 0)}/{report.get('window_count', 0)}",
                    self._number(ols.get("candidate_rate_hz"), 2, " Hz"),
                    self._number(robust.get("candidate_rate_hz"), 2, " Hz"),
                    self._number(ols.get("slope_stderr_ppm"), 1, " ppm"),
                    f"leave-one-window-out {interval}; report-only"))
            elif schema == "dvd-free-audio-timing-v1":
                self.rows.append((
                    "Duration/PAD model (not a DVD rate approval)",
                    str(report.get("map_pad_count_full", "unavailable")),
                    self._number(report.get("duration_candidate_effective_rate_hz"), 3, " Hz"),
                    self._number(report.get("duration_candidate_effective_rate_rounded_hz"), 2, " Hz"),
                    self._number(report.get("duration_delta_frames_stack_minus_audio"), 3, " frames"),
                    "DVD not opened; candidate only"))

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Audio timing evidence — {self.context['identity']}")
        self.window.geometry("1120x700")
        self.window.minsize(900, 600)
        self.window.transient(self.parent)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Audio timing evidence — {self.context['identity']}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        audio = (self.context.get("manifest_data") or {}).get("audio") or {}
        ttk.Label(
            body,
            text=(f"Verified capture: {audio.get('capture') or 'not selected'}  |  "
                  f"Orc alignment pad: {audio.get('pad_frames') if audio.get('pad_frames') is not None else 'not measured'} frames"),
            wraplength=980).pack(anchor="w", pady=(4, 8))
        no_dvd = (self.context.get("manifest_data") or {}).get("dvd_reference_available") is False
        policy_text = (
            "No-DVD duration/PAD evidence is report-only here. The previously recorded no-DVD choice "
            "permits promotion only after final frame count/head trim are measured and the one-frame "
            "endpoint gate passes."
            if no_dvd else
            "DVD-backed rate/origin evidence is report-only: the canonical policy defines no numeric "
            "fit-agreement threshold. The fade fits still use native-WAV coordinates and are not yet "
            "pad-corrected; no rate or AudioSkip was promoted by this evidence run.")
        ttk.Label(body, text=policy_text, wraplength=980,
                  justify="left").pack(anchor="w", pady=(0, 8))
        columns = ("method", "samples", "ols", "robust", "uncertainty", "status")
        table = ttk.Treeview(body, columns=columns, show="headings", height=8)
        headings = {
            "method": "Measurement",
            "samples": "Pairs / windows / PADs",
            "ols": "OLS candidate",
            "robust": "Robust candidate",
            "uncertainty": "Uncertainty",
            "status": "Interpretation",
        }
        widths = {"method": 300, "samples": 110, "ols": 130, "robust": 130,
                  "uncertainty": 130, "status": 260}
        for column in columns:
            table.heading(column, text=headings[column])
            table.column(column, width=widths[column], anchor="w")
        table.pack(fill="both", expand=True)
        for row in self.rows:
            table.insert("", "end", values=row)
        if not self.rows:
            ttk.Label(body, text=self.load_error or "No structured audio evidence was available.",
                      wraplength=980).pack(anchor="w", pady=6)
        elif self.load_error:
            ttk.Label(body, text=self.load_error, wraplength=980).pack(anchor="w", pady=4)
        if self.deferred:
            ttk.Label(body, text="Waiting for prerequisites: " + "; ".join(self.deferred),
                      wraplength=980).pack(anchor="w", pady=(6, 0))

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        native = (self.context.get("file_paths") or {}).get("ld_native")

        def play_native() -> None:
            if not native or not Path(native).is_file():
                messagebox.showwarning("Native audio unavailable", "The selected native LD WAV is missing.",
                                       parent=self.window)
                return
            try:
                if os.name == "nt":
                    os.startfile(str(native))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(native)])
                else:
                    subprocess.Popen(["xdg-open", str(native)])
            except OSError as exc:
                messagebox.showerror("Could not play native audio", str(exc), parent=self.window)

        ttk.Button(actions, text="Play native LD audio", command=play_native).pack(side="left")
        ttk.Button(actions, text="Close", command=self.window.destroy).pack(side="right")
        self.window.grab_set()
        self.window.focus_set()


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
                 fingerprint_policy: str = "balanced", on_trim_review: Any | None = None,
                 on_cadence: Any | None = None):
        self.parent = parent
        self.context = context
        self.on_plan = on_plan
        self.fingerprint_policy = fingerprint_policy
        self.on_trim_review = on_trim_review
        self.on_cadence = on_cadence
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
            video = (self.context.get("manifest_data") or {}).get("video") or {}
            audio = (self.context.get("manifest_data") or {}).get("audio") or {}
            trim_review = video.get("trim_review") or {}
            status = (
                f"Verified audio.pad_frames: {audio.get('pad_frames', 'missing')}  |  "
                f"head/tail: {video.get('head_trim', 'missing')} / {video.get('tail_trim', 'missing')} "
                f"(trim review: {trim_review.get('status', 'not recorded')})")
            ttk.Label(fields, text=status, wraplength=780).grid(row=0, column=0, columnspan=3,
                                                                sticky="w", pady=5)
            ttk.Label(fields,
                      text=("Full TFM log and complete frame decode are generated automatically. "
                            "All review intervals must receive visual decisions before cadence values are recorded."),
                      wraplength=780).grid(row=1, column=0, columnspan=3, sticky="w", pady=5)
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
        if platform in ("windows", "win32"):
            ttk.Button(buttons, text="Build full cadence evidence…",
                       command=self._build_cadence).pack(side="right")
            ttk.Button(buttons, text="Review head/tail trims…",
                       command=self._review_trims).pack(side="left", padx=(0, 6))
        else:
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

    def _review_trims(self) -> None:
        if self.on_trim_review:
            self.on_trim_review()

    def _build_cadence(self) -> None:
        if self.on_cadence:
            self.on_cadence()


class AlignmentSampleReviewWindow:
    """Visual/audio gate for disposable, report-bound alignment samples."""

    def __init__(self, parent: tk.Misc, index_path: str | Path,
                 on_close: Any | None = None):
        self.parent = parent
        self.index_path = Path(index_path)
        self.on_close = on_close
        self.window: tk.Toplevel | None = None
        self.samples: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8-sig"))
            if data.get("schema") != "alignment-samples-v1" or data.get("candidate_is_approval") is not False:
                raise ValueError("sample index is not a candidate-only alignment sample set")
            integrity_errors: list[str] = []
            for label, path_key, hash_key in (
                    ("video source", "video_source", "video_sha256"),
                    ("audio source", "audio_source", "audio_sha256")):
                source = Path(str(data.get(path_key) or ""))
                if not source.is_file() or sha256_file(source) != data.get(hash_key):
                    integrity_errors.append(f"{label} changed or is unavailable")
            for evidence in data.get("reports", []):
                path = Path(str(evidence.get("path") or ""))
                if not path.is_file() or sha256_file(path) != evidence.get("sha256"):
                    integrity_errors.append("a source report changed or is unavailable")
                    break
            alignment_path = data.get("alignment_path")
            if alignment_path and (
                    not Path(str(alignment_path)).is_file() or
                    sha256_file(Path(str(alignment_path))) != data.get("alignment_sha256")):
                integrity_errors.append("the approved alignment artifact changed or is unavailable")
            if integrity_errors:
                raise ValueError("; ".join(integrity_errors))
            self.samples = {}
            for row in data.get("samples", []):
                if row.get("status") != "READY":
                    continue
                path = Path(str(row.get("path", "")))
                if not path.is_file() or sha256_file(path) != row.get("output_sha256"):
                    continue
                self.samples[str(row["id"])] = row
            self.warning = str(data.get("warning") or "Decision aids only; no measurements are approved.")
            self.deferred = [str(value) for value in data.get("deferred", [])]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            self.error = f"Could not load the preview samples: {exc}"
            self.warning = "No candidate measurement was approved."
            self.deferred = []

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title("Alignment samples — visual/audio review")
        self.window.geometry("1040x640")
        self.window.minsize(820, 500)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Candidate A/V alignment samples",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=self.warning, wraplength=980,
                  justify="left").pack(anchor="w", pady=(4, 8))
        ttk.Label(body, text=f"Episode samples: {self.index_path.parent}",
                  wraplength=980).pack(anchor="w", pady=(0, 8))
        columns = ("candidate", "anchor", "rate", "residual", "status")
        table = ttk.Treeview(body, columns=columns, show="headings", height=12)
        for name, title, width in (
                ("candidate", "Candidate / source evidence", 340),
                ("anchor", "WAV → master (s)", 150),
                ("rate", "Preview-only slope", 120),
                ("residual", "Residual / correlation", 150),
                ("status", "Playback status", 180)):
            table.heading(name, text=title)
            table.column(name, width=width, anchor="w")
        table.pack(fill="both", expand=True)
        ids: dict[str, str] = {}
        for key, row in self.samples.items():
            anchor = f"{row.get('wav_time_s', 0):.6f} → {row.get('master_time_s', 0):.6f}"
            if isinstance(row.get("dvd_reference_time_s"), (int, float)):
                anchor += f" (DVD {row['dvd_reference_time_s']:.6f})"
            slope = row.get("candidate_slope")
            slope_text = f"{slope:.7f}× (preview only)" if isinstance(slope, (int, float)) else "unavailable"
            quality = row.get("residual_s", row.get("correlation"))
            quality_text = (f"resid {quality:+.3f} s" if "residual_s" in row else
                            f"corr {quality:.3f}" if isinstance(quality, (int, float)) else "candidate pair")
            item = table.insert("", "end", values=(
                row.get("label", key), anchor, slope_text, quality_text, "Ready to audition"))
            ids[item] = key
        if self.error:
            ttk.Label(body, text=self.error, foreground="#8b1a1a",
                      wraplength=980).pack(anchor="w", pady=6)
        elif not self.samples:
            ttk.Label(body, text="No preview candidate passed the available timestamp/source checks.",
                      wraplength=980).pack(anchor="w", pady=6)
        if self.deferred:
            ttk.Label(body, text="Not generated: " + "; ".join(self.deferred),
                      wraplength=980).pack(anchor="w", pady=(6, 0))

        def play_selected() -> None:
            selected = table.selection()
            if not selected:
                messagebox.showinfo("Select a sample", "Choose a candidate row to play.", parent=self.window)
                return
            row = self.samples[ids[selected[0]]]
            path = Path(str(row["path"]))
            try:
                if not path.is_file() or sha256_file(path) != row.get("output_sha256"):
                    raise OSError("The preview changed after it was verified; rebuild the sample set.")
                if os.name == "nt":
                    os.startfile(str(path))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(path)])
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except OSError as exc:
                messagebox.showerror("Could not play alignment sample", str(exc), parent=self.window)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Play selected sample", command=play_selected).pack(side="left")
        ttk.Button(actions, text="Close", command=self.close).pack(side="right")
        self.window.grab_set()
        self.window.focus_set()

    def close(self) -> None:
        window, self.window = self.window, None
        if window:
            try:
                window.grab_release()
                window.destroy()
            except tk.TclError:
                pass
        callback, self.on_close = self.on_close, None
        if callback:
            callback()
