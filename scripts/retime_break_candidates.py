#!/usr/bin/env python3
"""Pure helpers for report-first retime-break candidate preparation and recording."""
from __future__ import annotations

import math
from pathlib import Path

from ldcommon import PipelineError
from fileutil import sha256_file

FPS = 30000.0 / 1001.0
CANDIDATE_SCHEMA = "ld-pipeline-retime-break-candidates-v1"


def validate_alignment(alignment) -> tuple[list[float], list[float]]:
    """Validate an Nx2+ frame/offset path and return its first two columns."""
    if getattr(alignment, "ndim", None) != 2 or alignment.shape[1] < 2 or not len(alignment):
        raise PipelineError(f"alignment path must be a non-empty Nx2 array; got {getattr(alignment, 'shape', None)}")
    master_frames = [float(row[0]) for row in alignment]
    offsets = [float(row[1]) for row in alignment]
    if any(not math.isfinite(value) for value in master_frames + offsets):
        raise PipelineError("alignment path contains non-finite frame/offset values")
    if any(right <= left for left, right in zip(master_frames, master_frames[1:])):
        raise PipelineError("alignment master-frame samples must be strictly increasing")
    return master_frames, offsets


def validate_visual_alignment_approval(ep, alignment_path: Path, dvd_path: Path,
                                       master_path: Path) -> dict:
    """Require a manifest marker bound to the exact candidate path and source media."""
    review = ((getattr(ep, "data", {}) or {}).get("video") or {}).get("dvd_alignment_review") or {}
    if review.get("status") != "APPROVED":
        raise PipelineError("DVD/LD alignment has no explicit visual approval marker")
    raw = str(review.get("path") or "")
    marker = Path(raw).expanduser()
    if not marker.is_absolute():
        parts = marker.parts
        if not parts or parts[0].casefold() != "probe" or ".." in parts:
            raise PipelineError("visual alignment marker has an unsafe path")
        marker = ep.ld.probe() / Path(*parts[1:])
    try:
        if marker.resolve(strict=True) != alignment_path.resolve(strict=True):
            raise PipelineError("visual approval marker names a different alignment artifact")
        if sha256_file(alignment_path) != review.get("sha256"):
            raise PipelineError("approved alignment artifact changed after visual review")
        if sha256_file(master_path) != review.get("master_source_sha256"):
            raise PipelineError("master source changed after visual alignment review")
        if sha256_file(dvd_path) != review.get("dvd_source_sha256"):
            raise PipelineError("DVD source changed after visual alignment review")
        raw_assets = Path(str(review.get("review_assets_path") or "")).expanduser()
        if not raw_assets.is_absolute():
            parts = raw_assets.parts
            if not parts or parts[0].casefold() != "probe" or ".." in parts:
                raise PipelineError("visual alignment approval has an unsafe review-assets path")
            raw_assets = ep.ld.probe() / Path(*parts[1:])
        assets_path = raw_assets.resolve(strict=True)
        probe = ep.ld.probe().resolve(strict=True)
        assets_path.relative_to(probe)
        if sha256_file(assets_path) != review.get("review_assets_sha256"):
            raise PipelineError("paired visual assets changed after alignment review")
        with assets_path.open("r", encoding="utf-8-sig") as stream:
            import json
            assets = json.load(stream)
        if (assets.get("schema") != "dvd-alignment-review-assets-v1" or
                assets.get("identity") != ep.identity or
                assets.get("alignment_sha256") != review.get("sha256") or
                assets.get("candidate_is_approval") is not False):
            raise PipelineError("paired visual assets do not match the approved alignment")
    except OSError as exc:
        raise PipelineError(f"cannot verify approved alignment/source evidence: {exc}") from exc
    return review


def _interval(row: dict, label: str) -> tuple[float, float]:
    try:
        start = float(row.get("start_s"))
        end = float(row.get("end_s"))
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"{label} has missing/non-numeric interval bounds") from exc
    if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
        raise PipelineError(f"{label} has an invalid interval: {start!r}..{end!r}")
    return start, end


def _interval_distance(a0: float, a1: float, b0: float, b1: float) -> float:
    if a1 < b0:
        return b0 - a1
    if b1 < a0:
        return a0 - b1
    return 0.0


def pair_ld_intervals(ld_intervals: list[dict], dvd_silences: list[tuple[float, float]],
                      alignment, *, pair_tolerance_s: float,
                      max_offset_spread_frames: float) -> list[dict]:
    """Return all plausible pairings; never resolve ambiguous or colliding matches.

    Alignment samples outside an LD black are not extrapolated. A spread in the sampled
    offset path larger than the caller's explicit limit is reported as an offset transition,
    not silently reduced to its midpoint.
    """
    if not math.isfinite(pair_tolerance_s) or pair_tolerance_s <= 0:
        raise PipelineError("pair_tolerance_s must be a finite positive value")
    if not math.isfinite(max_offset_spread_frames) or max_offset_spread_frames < 0:
        raise PipelineError("max_offset_spread_frames must be finite and nonnegative")
    master_frames, offsets = validate_alignment(alignment)
    dvd_rows = []
    for index, interval in enumerate(dvd_silences, 1):
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise PipelineError(f"DVD silence D{index} must contain exactly two bounds")
        try:
            start, end = map(float, interval)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"DVD silence D{index} has non-numeric bounds") from exc
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
            raise PipelineError(f"DVD silence D{index} has an invalid interval: {start!r}..{end!r}")
        dvd_rows.append({"id": f"D{index}", "start_s": start, "end_s": end})
    dvd_rows.sort(key=lambda row: (row["start_s"], row["end_s"], row["id"]))
    for index, row in enumerate(dvd_rows, 1):
        row["id"] = f"D{index}"
    if any(right["start_s"] < left["end_s"] - 1e-9
           for left, right in zip(dvd_rows, dvd_rows[1:])):
        raise PipelineError("DVD silence intervals overlap; coalesce/inspect the detector output before pairing")

    if any(not isinstance(row, dict) for row in ld_intervals):
        raise PipelineError("LD black candidates must be interval objects")
    labels = [str(row.get("label", "")) for row in ld_intervals]
    if any(not label for label in labels) or len(labels) != len(set(labels)):
        raise PipelineError("LD black candidate labels must be present and unique")
    ld_bounds = []
    for candidate in ld_intervals:
        start, end = _interval(candidate, f"LD candidate {candidate.get('label', '')}")
        ld_bounds.append((start, end))
    ld_bounds.sort()
    if any(right[0] < left[1] - 1e-9 for left, right in zip(ld_bounds, ld_bounds[1:])):
        raise PipelineError("LD black candidates overlap; resolve threshold splits/merges before pairing")
    results = []
    for candidate in ld_intervals:
        label = str(candidate["label"])
        start_s, end_s = _interval(candidate, f"LD candidate {label}")
        try:
            start_frame = float(candidate.get("start_frame", start_s * FPS))
            end_frame = float(candidate.get("end_frame_exclusive", end_s * FPS))
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"LD candidate {label} has non-numeric frame bounds") from exc
        if (not math.isfinite(start_frame) or not math.isfinite(end_frame) or
                end_frame <= start_frame):
            raise PipelineError(f"LD candidate {label} has invalid frame bounds")
        sample_indexes = [i for i, frame in enumerate(master_frames)
                          if start_frame <= frame < end_frame]
        row = {
            "label": label,
            "ld_black": [start_s, end_s],
            "master_frame_range": [start_frame, end_frame],
            "alignment_sample_count": len(sample_indexes),
            "projected_dvd_interval_s": None,
            "offset_min_frames": None,
            "offset_max_frames": None,
            "offset_spread_frames": None,
            "pair_candidates": [],
            "pair_status": "alignment-uncovered",
        }
        if not sample_indexes:
            results.append(row)
            continue

        projected = [((master_frames[i] + offsets[i]) / FPS) for i in sample_indexes]
        offset_values = [offsets[i] for i in sample_indexes]
        if not projected or any(not math.isfinite(value) for value in projected):
            results.append(row)
            continue
        offset_min, offset_max = min(offset_values), max(offset_values)
        spread = offset_max - offset_min
        projected_start, projected_end = min(projected), max(projected)
        row.update({
            "projected_dvd_interval_s": [projected_start, projected_end],
            "offset_min_frames": offset_min,
            "offset_max_frames": offset_max,
            "offset_spread_frames": spread,
        })
        matches = []
        for dvd_row in dvd_rows:
            distance = _interval_distance(projected_start, projected_end,
                                          dvd_row["start_s"], dvd_row["end_s"])
            if distance <= pair_tolerance_s:
                matches.append({**dvd_row, "distance_s": distance})
        row["pair_candidates"] = matches
        if spread > max_offset_spread_frames:
            row["pair_status"] = "offset-transition"
        elif len(matches) == 0:
            row["pair_status"] = "no-dvd-silence"
        elif len(matches) > 1:
            row["pair_status"] = "ambiguous-pair"
        else:
            row["pair_status"] = "unique-pair"
        results.append(row)

    # A unique local match is not unique globally if another LD candidate can claim the
    # same DVD silence. Mark every claimant; do not keep the candidate with the closest gap.
    claims: dict[str, list[dict]] = {}
    for row in results:
        for match in row["pair_candidates"]:
            claims.setdefault(match["id"], []).append(row)
    collided_ids = {dvd_id for dvd_id, rows in claims.items() if len(rows) > 1}
    for row in results:
        if any(match["id"] in collided_ids for match in row["pair_candidates"]):
            row["pair_status"] = "many-to-one-collision"
    return results


def candidate_break_entries(report: dict, selected_labels: list[str],
                            merged_labels: list[str], *, reviewed_by: str,
                            review_note: str, report_sha256: str) -> tuple[list[dict], list[dict]]:
    """Build manifest candidate entries only from explicitly selected report rows.

    This records interval candidates, not retime offsets. All offsets/steps/gaps remain null
    for the existing measurement/adjudication stage.
    """
    if report.get("schema") != CANDIDATE_SCHEMA:
        raise PipelineError("unsupported retime-break candidate report schema")
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise PipelineError("candidate report has no candidates array")
    if any(not isinstance(row, dict) for row in candidates):
        raise PipelineError("candidate report rows must be objects")
    by_label = {str(row.get("label")): row for row in candidates}
    if len(by_label) != len(candidates) or "" in by_label:
        raise PipelineError("candidate report labels are missing or duplicated")
    if not selected_labels and not merged_labels:
        raise PipelineError("select at least one unique candidate or explicitly reviewed merged label")
    if len(selected_labels) != len(set(selected_labels)) or len(merged_labels) != len(set(merged_labels)):
        raise PipelineError("candidate selections contain duplicate labels")
    if set(selected_labels) & set(merged_labels):
        raise PipelineError("a candidate cannot be selected both as a cut and merged music-through")
    unknown = (set(selected_labels) | set(merged_labels)) - set(by_label)
    if unknown:
        raise PipelineError("unknown candidate label(s): " + ", ".join(sorted(unknown)))
    global_claims: dict[str, set[str]] = {}
    for candidate in candidates:
        candidate_matches = candidate.get("pair_candidates", []) or []
        if not isinstance(candidate_matches, list) or any(not isinstance(match, dict) for match in candidate_matches):
            raise PipelineError(f"{candidate.get('label')} has malformed pair_candidates")
        for match in candidate_matches:
            dvd_id = str(match.get("id", ""))
            if dvd_id:
                global_claims.setdefault(dvd_id, set()).add(str(candidate.get("label", "")))
    pair_ids = []
    entries = []
    common = {"off_before": None, "off_after": None, "step": None, "gap_s": None}
    provenance = {
        "candidate_report_sha256": report_sha256,
        "candidate_report_schema": CANDIDATE_SCHEMA,
        "reviewed_by": reviewed_by,
        "review_note": review_note.strip(),
        "pair_parameters": report.get("parameters"),
        "ld_black_report_sha256": (report.get("inputs") or {}).get("ld_black_report_sha256"),
        "alignment_sha256": (report.get("inputs") or {}).get("alignment_sha256"),
    }
    inputs = report.get("inputs") or {}
    try:
        dvd_duration_s = float(inputs["dvd_duration_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("candidate report is missing a valid DVD duration") from exc
    if not math.isfinite(dvd_duration_s) or dvd_duration_s <= 0:
        raise PipelineError("candidate report DVD duration must be finite and positive")
    for label in selected_labels:
        row = by_label[label]
        if row.get("pair_status") != "unique-pair" or len(row.get("pair_candidates", [])) != 1:
            raise PipelineError(f"{label} is not a unique pair ({row.get('pair_status')}); review/resolve it first")
        ld_start, ld_end = _interval({"start_s": row["ld_black"][0],
                                      "end_s": row["ld_black"][1]}, f"LD candidate {label}")
        match = row["pair_candidates"][0]
        claimants = global_claims.get(str(match.get("id", "")), set())
        if claimants != {label}:
            raise PipelineError(f"{label} shares DVD silence {match.get('id')} with other candidate(s): "
                                + ", ".join(sorted(claimants - {label})))
        dvd_start, dvd_end = _interval(match, f"DVD silence for {label}")
        if dvd_end > dvd_duration_s + 0.050:
            raise PipelineError(f"DVD silence for {label} exceeds the measured DVD duration")
        pair_ids.append(match["id"])
        entries.append({
            "label": label,
            "ld_black": [ld_start, ld_end],
            "dvd_silence": [dvd_start, dvd_end],
            **common,
            "_candidate_provenance": {**provenance, "pair_status": "unique-pair",
                                      "dvd_silence_id": match["id"]},
        })
    if len(pair_ids) != len(set(pair_ids)):
        raise PipelineError("selected candidates claim the same DVD silence")
    merged = []
    for label in merged_labels:
        row = by_label[label]
        if row.get("pair_status") != "no-dvd-silence" or row.get("pair_candidates"):
            raise PipelineError(f"{label} is not an unmatched candidate; cannot record as merged")
        ld_start, ld_end = _interval({"start_s": row["ld_black"][0],
                                      "end_s": row["ld_black"][1]}, f"LD merged candidate {label}")
        merged.append({
            "label": label,
            "ld_black": [ld_start, ld_end],
            "step": None,
            "_candidate_provenance": {**provenance, "pair_status": "no-dvd-silence",
                                      "decision": "merged_music_through"},
        })
    return entries, merged
