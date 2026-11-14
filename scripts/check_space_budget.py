#!/usr/bin/env python3
"""Read-only projected free-space gate before a write-heavy pipeline stage.

The caller supplies a defensible estimate for the output and temporary space
needed before the next gate. The check subtracts that estimate from current
free space and preserves machine.json's min_free_gb reserve.

Example:
  python scripts/check_space_budget.py --stage stack-render --planned-gib 26
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError  # noqa: E402

GIB = 1024 ** 3


def evaluate_budget(free_bytes: int, planned_bytes: int,
                    reserve_bytes: int) -> tuple[bool, int]:
    """Return whether the plan preserves reserve and its projected free bytes."""
    projected = free_bytes - planned_bytes
    return projected >= reserve_bytes, projected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, help="pipeline stage about to write outputs")
    parser.add_argument("--planned-gib", type=float, required=True,
                        help="estimated peak additional space through the next space check, in GiB")
    parser.add_argument("--root", default="work",
                        help="machine.json root whose volume will receive the writes (default: work)")
    parser.add_argument("--profile", help="machine.json override; otherwise use LDP_PROFILE/bundle profile")
    args = parser.parse_args()
    if not math.isfinite(args.planned_gib) or args.planned_gib <= 0:
        parser.error("--planned-gib must be a finite, positive number")

    try:
        ld = Ld(args.profile)
        root = ld.root(args.root)
        if not root.is_dir():
            raise PipelineError(f"root '{args.root}' is not an existing directory: {root}")
        limits = ld.profile.get("limits") or {}
        reserve_gib = float(limits.get("min_free_gb") or 80)
        free_bytes = shutil.disk_usage(root).free
    except (OSError, ValueError, PipelineError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    planned_bytes = math.ceil(args.planned_gib * GIB)
    reserve_bytes = math.ceil(reserve_gib * GIB)
    passed, projected_bytes = evaluate_budget(free_bytes, planned_bytes, reserve_bytes)
    status = "PASS" if passed else "FAIL"
    print(
        f"SPACE BUDGET {status} | stage={args.stage} | root={root} | "
        f"free={free_bytes / GIB:.1f} GiB | planned={planned_bytes / GIB:.1f} GiB | "
        f"projected={projected_bytes / GIB:.1f} GiB | reserve={reserve_gib:.1f} GiB"
    )
    if not passed:
        print("Stop before writing. Free space or move the work root; do not lower min_free_gb.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
