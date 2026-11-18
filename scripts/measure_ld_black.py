#!/usr/bin/env python3
"""Report LD dark/fade intervals from descriptor-grid luma at several thresholds.

This report-only helper is evidence for human ``retime.breaks[].ld_black`` adjudication.
It never chooses a universal threshold, changes the manifest, or moves a cut. Compare the
threshold sensitivity and the reported run to independent picture/content evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from dependency_lock import LockError  # noqa: E402
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode, redact_shared_text  # noqa: E402
from fileutil import sha256_file  # noqa: E402
from measure_dvd_alignment import load_desc  # noqa: E402

FPS = 30000.0 / 1001.0
DEFAULT_THRESHOLDS = (5.0, 10.0, 15.0)


def luma_runs(means, threshold: float, fps: float = FPS,
              min_duration_s: float = 0.3) -> list[dict[str, Any]]:
    """Return contiguous [start_frame,end_frame) runs below a descriptor luma threshold."""
    import numpy as np

    values = np.asarray(means, dtype=np.float64)
    if values.ndim != 1 or not len(values):
        raise PipelineError("descriptor luma means must be a non-empty one-dimensional array")
    if threshold <= 0 or fps <= 0 or min_duration_s < 0:
        raise PipelineError("threshold/FPS must be positive and minimum duration nonnegative")
    mask = values < threshold
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    result = []
    for start, end in zip(starts, ends):
        duration = (int(end) - int(start)) / fps
        if duration + 1e-12 < min_duration_s:
            continue
        result.append({
            "start_frame": int(start),
            "end_frame_exclusive": int(end),
            "start_s": float(start / fps),
            "end_s": float(end / fps),
            "duration_s": float(duration),
            "mean_luma": float(values[start:end].mean()),
        })
    return result


def best_overlap(run_rows: list[dict], start_s: float, end_s: float) -> dict | None:
    """Find the threshold run with greatest overlap against a measured candidate interval."""
    best = None
    best_seconds = 0.0
    for row in run_rows:
        overlap = max(0.0, min(end_s, row["end_s"]) - max(start_s, row["start_s"]))
        if overlap > best_seconds:
            best, best_seconds = row, overlap
    if best is None:
        return None
    return {**best, "overlap_seconds": best_seconds}


def descriptor_luma_means(descriptors) -> Any:
    """Compute float64 row means without making a full float64 descriptor copy.

    Descriptor arrays are memory-mapped by ``load_desc`` and can be large. NumPy's
    float64 accumulator preserves the existing mean precision while limiting the
    additional allocation to the one-dimensional means vector.
    """
    import numpy as np

    values = np.asanyarray(descriptors)
    if values.ndim != 2 or not values.shape[1]:
        raise PipelineError(f"descriptor array must be a non-empty 2D matrix; got {values.shape}")
    return np.mean(values, axis=1, dtype=np.float64)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--thresholds", type=float, nargs="+", default=list(DEFAULT_THRESHOLDS),
                        help="8-bit descriptor-grid luma thresholds to compare; none is a global default")
    parser.add_argument("--min-duration", type=float, default=0.3)
    parser.add_argument("--report-out")
    args = parser.parse_args()
    try:
        if not args.thresholds or len(set(args.thresholds)) != len(args.thresholds):
            raise PipelineError("thresholds must be a non-empty list of distinct values")
        ld = Ld()
        ep = load_episode(args.episode, ld)
        master_path = ep.path("master_descriptors")
        master = load_desc(master_path, "master", ep.code, ep.path("stack_master"))
        import numpy as np
        means = descriptor_luma_means(master)
        thresholds = {}
        for threshold in args.thresholds:
            thresholds[str(threshold)] = luma_runs(means, threshold, FPS, args.min_duration)

        break_rows = []
        for index, br in enumerate((ep.data.get("retime") or {}).get("breaks") or [], 1):
            interval = br.get("ld_black")
            if not isinstance(interval, list) or len(interval) != 2:
                continue
            start_s, end_s = map(float, interval)
            candidates = {key: best_overlap(rows, start_s, end_s)
                          for key, rows in thresholds.items()}
            break_rows.append({"label": br.get("label", f"B{index}"),
                               "candidate_ld_black_s": [start_s, end_s],
                               "threshold_matches": candidates})

        report = {
            "schema": "ld-pipeline-ld-black-intervals-v2",
            "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "episode": ep.code,
            "series": ep.series,
            "identity": ep.identity,
            "status": "CANDIDATE_ONLY",
            "fps": FPS,
            "minimum_run_duration_s": args.min_duration,
            "thresholds_luma_8bit": args.thresholds,
            "descriptor_shape": list(master.shape),
            "source_descriptors": str(master_path),
            "source_descriptors_sha256": sha256_file(master_path),
            "thresholds_luma": thresholds,
            "candidate_break_matches": break_rows,
            "manifest_updated": False,
            "caveat": "Threshold runs are sensitivity evidence, not an automatic black boundary or trim decision.",
        }
        out = Path(args.report_out) if args.report_out else ld.probe() / f"{ep.code}-ld-black-intervals.json"
        assert_writable_path(out, ld)
        if out.exists():
            raise PipelineError(f"refusing to overwrite LD-black interval report: {out}")
        out.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if (ld.profile.get("shared") or {}).get("enabled") is True:
            encoded = redact_shared_text(encoded, ld)
        with out.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        for threshold, rows in thresholds.items():
            print(f"luma<{threshold}: {len(rows)} intervals >= {args.min_duration:.3f}s")
        print(f"CANDIDATE ONLY: no threshold chosen and manifest unchanged; report={out}")
        return 0
    except (LockError, PipelineError, OSError, ValueError) as exc:
        print(f"LD BLACK MEASUREMENT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
