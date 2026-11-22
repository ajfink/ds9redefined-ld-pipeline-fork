#!/usr/bin/env python3
"""Compare fstk's DVD alignment with an episode's approved v38 alignment (read-only).

Usage:
  python tools/validate_v38_alignment.py <v38 device episode dir> <v38 probe root> [--json out.json]

Reads the episode manifest's ``files.master_descriptors`` / ``dvd_descriptors`` / ``align_npy``
from the probe root, runs ``fstk.media.alignment.measure`` on the same descriptors, then
reports:
* where the two paths agree, over the master frames that both sampled;
* fstk's segments, origin and automatic decision;
* every failed acceptance check, so the provisional thresholds can be calibrated.
Nothing is written next to the v38 data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fstk.media import alignment as al


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_dir", type=Path)
    ap.add_argument("probe_root", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    (manifest_path,) = sorted((args.episode_dir / "manifest").glob("*.json"))
    files = json.loads(manifest_path.read_text(encoding="utf-8-sig"))["files"]
    load = {k: np.load(args.probe_root / files[k]["name"], mmap_mode="r")
            for k in ("master_descriptors", "dvd_descriptors", "align_npy")}
    v38 = np.asarray(load["align_npy"])
    res = al.measure(load["master_descriptors"], load["dvd_descriptors"],
                     progress=lambda f, m: print(f"  {f:4.0%} {m}", flush=True))
    ours = al.path_array(res)
    common = np.intersect1d(v38[:, 0], ours[:, 0])
    a = dict(zip(v38[:, 0].tolist(), v38[:, 1].tolist(), strict=True))
    b = dict(zip(ours[:, 0].tolist(), ours[:, 1].tolist(), strict=True))
    diff = np.array([b[i] - a[i] for i in common.tolist()])
    print(f"\nv38 path: {len(v38)} samples, offsets {v38[:, 1].min()}..{v38[:, 1].max()}, "
          f"from master frame {v38[0, 0]}")
    print(f"fstk path: {len(ours)} samples in matched segments, origin master frame {res.origin_master_frame}")
    if len(common):
        print(f"common samples: {len(common)}; exact {np.mean(diff == 0):.2%}, within ±1 {np.mean(abs(diff) <= 1):.2%}; "
              f"max |Δ| {int(abs(diff).max())}")
        bad = common[abs(diff) > 1]
        if len(bad):
            print("  disagreements (master frame: v38 → fstk):",
                  ", ".join(f"{i}: {a[i]}→{b[i]}" for i in bad[:20].tolist()))
    print("\nsegments:")
    for s in res.segments:
        extra = (f" NCC~{s.median_ncc:.3f} dense±1 {s.dense_within1:.1%}" if s.offset is not None
                 and s.dense_within1 is not None else "")
        print(f"  {s.kind:9s} {s.start_frame / al.FPS:8.2f}–{s.end_frame / al.FPS:8.2f}s "
              f"offset {s.offset if s.offset is not None else '—':>6}{extra}")
    print(f"\nreverse agreement: {res.reverse_agreement}")
    print(f"decision: {res.decision}")
    for r in res.reasons:
        print(f"  - {r}")
    if args.json:
        args.json.write_text(json.dumps(res.summary(), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
