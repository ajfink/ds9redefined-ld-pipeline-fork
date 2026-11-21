# Ported verbatim from v38 scripts/cadence_analysis.py (tools/port_v38.py). Do not hand-edit ported logic
# without a characterization test that states the intended behaviour change.
"""Pure TFM-log parsing and machine-readable cadence metrics."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

FPS = 30000.0 / 1001.0
TFM_MI = 80
REPAIR_MI = 65
INSERT_WINDOW_FRAMES = 90
INSERT_WINDOW_STEP = 15
INSERT_COMBED_FRAMES = 40
WINDOW_30S_FRAMES = 899

_WITH_FLAG = re.compile(r"^\s*(\d+)\s+([a-z])\s+([-+])\s+\[(\d+)\]\s*$", re.I)
_NO_FLAG = re.compile(r"^\s*(\d+)\s+([a-z])\s+\[(\d+)\]\s*$", re.I)


def parse_tfm_log(text: str, expected_frames: int | None = None) -> list[dict[str, Any]]:
    """Parse every decision row and fail on duplicate, missing, or malformed frame rows."""
    rows: list[dict[str, Any]] = []
    malformed: list[int] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = _WITH_FLAG.fullmatch(stripped)
        if match:
            frame, code, flag, mic = match.groups()
            rows.append({
                "frame": int(frame), "match_code": code.lower(),
                "flag": flag, "mic": int(mic),
            })
            continue
        match = _NO_FLAG.fullmatch(stripped)
        if match:
            frame, code, mic = match.groups()
            rows.append({
                "frame": int(frame), "match_code": code.lower(),
                "flag": None, "mic": int(mic),
            })
            continue
        if re.match(r"^\d+\s+", stripped):
            malformed.append(line_number)
    if malformed:
        raise ValueError(f"malformed TFM frame row(s) at line(s) {malformed[:12]}")
    if not rows:
        raise ValueError("TFM log contains no frame decision rows")
    frames = [row["frame"] for row in rows]
    if frames != list(range(len(frames))):
        raise ValueError("TFM log frame indices are not complete, contiguous, and zero-based")
    if expected_frames is not None and len(rows) != expected_frames:
        raise ValueError(f"TFM log contains {len(rows)} rows; expected exactly {expected_frames}")
    return rows


def _spans(mask: list[bool]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, enabled in enumerate([*mask, False]):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            spans.append((start, index - 1))
            start = None
    return spans


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _insert_regions(mics: list[int]) -> list[tuple[int, int]]:
    windows = []
    for start in range(0, max(0, len(mics) - INSERT_WINDOW_FRAMES + 1), INSERT_WINDOW_STEP):
        end = start + INSERT_WINDOW_FRAMES
        combed = sum(value > TFM_MI for value in mics[start:end])
        if combed >= INSERT_COMBED_FRAMES:
            windows.append((start, end - 1))
    return _merge_spans(windows)


def _interval(start: int, end: int, reasons: list[str]) -> dict[str, Any]:
    return {
        "start_frame": start, "end_frame_inclusive": end,
        "start_s": start / FPS, "end_s": end / FPS,
        "frame_count": end - start + 1,
        "reasons": sorted(set(reasons)),
        "visual_status": "REVIEW_REQUIRED",
    }


def analyze_tfm_rows(rows: list[dict[str, Any]], *,
                     motion_values: list[float] | None = None) -> dict[str, Any]:
    """Summarize verified rows without scraping PowerShell or inferring a map."""
    if not rows or [row.get("frame") for row in rows] != list(range(len(rows))):
        raise ValueError("cadence analysis requires a verified contiguous zero-based TFM row set")
    count = len(rows)
    codes = [str(row["match_code"]).lower() for row in rows]
    mics = [int(row["mic"]) for row in rows]
    unmatched = [code == "u" for code in codes]
    combed = [(mic > TFM_MI) or is_unmatched for mic, is_unmatched in zip(mics, unmatched, strict=True)]
    near_repair = [(mic > REPAIR_MI) or is_unmatched for mic, is_unmatched in zip(mics, unmatched, strict=True)]
    p5_matches = sum(codes[index] == codes[index - 5] for index in range(5, count))
    p5_total = max(0, count - 5)
    p5_pct = 100.0 * p5_matches / p5_total if p5_total else 0.0
    code_counts: dict[str, int] = {}
    for code in codes:
        code_counts[code] = code_counts.get(code, 0) + 1
    combed_spans = [(start, end) for start, end in _spans(combed)
                    if end - start + 1 >= INSERT_WINDOW_FRAMES]
    unmatched_spans = _spans(unmatched)
    insert_regions = _insert_regions(mics)
    insert_mask = [False] * count
    for start, end in insert_regions:
        for index in range(start, end + 1):
            insert_mask[index] = True
    repair_mask = [near or inside for near, inside in zip(near_repair, insert_mask, strict=True)]

    interval_reasons: dict[tuple[int, int], list[str]] = {}
    for start, end in combed_spans:
        interval_reasons.setdefault((start, end), []).append("sustained-combing")
    for start, end in unmatched_spans:
        interval_reasons.setdefault((start, end), []).append("unmatched-frames")
    for start, end in insert_regions:
        interval_reasons.setdefault((start, end), []).append("interlaced-insert-density")
    # No numeric confidence threshold is supplied: windows are flagged when their
    # measured period-5 agreement is not perfect or they contain combed/unmatched rows.
    windows = []
    for start in range(0, count, WINDOW_30S_FRAMES):
        end = min(count, start + WINDOW_30S_FRAMES)
        local_codes = codes[start:end]
        local_matches = sum(local_codes[i] == local_codes[i - 5] for i in range(5, len(local_codes)))
        local_total = max(0, len(local_codes) - 5)
        local_p5 = 100.0 * local_matches / local_total if local_total else 0.0
        local_unmatched = sum(unmatched[start:end])
        local_combed = sum(combed[start:end])
        local_activity = sum(code != "c" for code in local_codes)
        local_reasons = []
        if local_p5 < 100.0:
            local_reasons.append("period-5 deviations")
        if local_unmatched:
            local_reasons.append("unmatched frames")
        if local_combed:
            local_reasons.append("combed frames")
        window = {
            "start_frame": start, "end_frame_exclusive": end,
            "start_s": start / FPS, "end_s": end / FPS,
            "period5_agreement_pct": local_p5,
            "unmatched_count": local_unmatched,
            "combed_count": local_combed,
            "telecine_activity_pct": 100.0 * local_activity / max(1, end - start),
            "mic_mean": sum(mics[start:end]) / max(1, end - start),
            "review_required": bool(local_reasons),
            "review_reasons": local_reasons,
        }
        if motion_values is not None:
            aligned_motion = motion_values if len(motion_values) == count else [0.0, *motion_values]
            if len(aligned_motion) != count:
                raise ValueError(
                    f"motion trace has {len(motion_values)} values; expected {count - 1} (or {count})")
            selected = aligned_motion[start:end]
            ordered = sorted(selected)
            p90 = ordered[int(0.9 * (len(ordered) - 1))] if ordered else 0.0
            motion_mean = sum(selected) / max(1, len(selected))
            window["motion_mean"] = motion_mean
            window["motion_p90"] = p90
            if motion_mean >= 15.0 and window["telecine_activity_pct"] <= 12.0:
                local_reasons.append("native-30p-or-hybrid-check")
                window["review_required"] = True
                window["review_reasons"] = local_reasons
        windows.append(window)
    # Compact adjacent low-confidence windows into review spans, while retaining every
    # measured 30-second window in the report/plot. This avoids hiding the full trend
    # behind a per-window sample cap.
    flagged_windows = [window for window in windows if window["review_required"]]
    groups: list[list[dict[str, Any]]] = []
    for window in flagged_windows:
        if groups and window["start_frame"] <= groups[-1][-1]["end_frame_exclusive"]:
            groups[-1].append(window)
        else:
            groups.append([window])
    for group in groups:
        start, end = group[0]["start_frame"], group[-1]["end_frame_exclusive"] - 1
        reasons = sorted({reason for window in group for reason in window["review_reasons"]})
        interval_reasons.setdefault((start, end), []).extend(reasons)

    merged_spans = _merge_spans(list(interval_reasons))
    review_intervals = [
        _interval(start, end, [reason for span, reasons in interval_reasons.items()
                                if start <= span[1] + 1 and end >= span[0] - 1
                                for reason in reasons])
        for start, end in merged_spans
    ]
    for interval in review_intervals:
        overlapping = [window for window in flagged_windows
                       if window["start_frame"] <= interval["end_frame_inclusive"] and
                       window["end_frame_exclusive"] - 1 >= interval["start_frame"]]
        if overlapping:
            worst = min(overlapping, key=lambda row: (
                row["period5_agreement_pct"], -row["combed_count"], -row["unmatched_count"]))
            interval["representative_frame"] = (
                worst["start_frame"] + worst["end_frame_exclusive"] - 1) // 2
    clean_windows = [
        window for window in windows
        if not window["review_required"] and window["end_frame_exclusive"] - window["start_frame"] >= 90
    ]
    clean_samples = []
    if clean_windows:
        for index in sorted({0, len(clean_windows) // 2, len(clean_windows) - 1}):
            window = clean_windows[index]
            center = (window["start_frame"] + window["end_frame_exclusive"] - 1) // 2
            clean_samples.append(_interval(center, center, ["representative clean interval"]))

    return {
        "frame_count": count,
        "fps": "30000/1001",
        "match_code_counts": code_counts,
        "period5_match_count": p5_matches,
        "period5_comparison_count": p5_total,
        "period5_agreement_pct": p5_pct,
        "unmatched_count": sum(unmatched),
        "unmatched_spans": [_interval(start, end, ["unmatched frames"])
                            for start, end in unmatched_spans],
        "combed_count": sum(combed),
        "repair_frame_count": sum(repair_mask),
        "repair_frame_pct": 100.0 * sum(repair_mask) / count,
        "combed_spans": [_interval(start, end, ["combed frames"])
                         for start, end in _spans(combed)],
        "near_repair_threshold_count": sum(near_repair),
        "insert_count": len(insert_regions),
        "insert_regions": [_interval(start, end, ["interlaced-insert-density"])
                           for start, end in insert_regions],
        "windows_30s": windows,
        "review_intervals": review_intervals,
        "clean_samples": clean_samples,
        "measurement_policy": (
            "All metrics are derived from the complete contiguous TFM log. "
            "No period-5 or confidence cutoff is inferred; any deviation from 100% is flagged."
        ),
        "visual_approval_required": True,
    }


def load_motion_trace(path: str) -> list[float]:
    values = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = float(stripped)
        except ValueError as exc:
            raise ValueError(f"motion trace contains a non-numeric value on line {line_number}") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"motion trace contains an invalid value on line {line_number}")
        values.append(value)
    if not values:
        raise ValueError("motion trace is empty")
    return values
