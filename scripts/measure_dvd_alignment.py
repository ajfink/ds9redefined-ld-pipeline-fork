#!/usr/bin/env python3
"""Continuity-constrained master-to-DVD picture alignment (diagnostic only).

Inputs are 48x36 grayscale descriptor arrays on a shared 29.97 frame grid. Create
them with `extract_video_descriptors.py`; DVD descriptors are decoded sequentially
and regridded from a measured source FPS. The returned state is `DVD index - master
index` in frames. A Viterbi path is a proposal, not an adjudicated act-break map.

  python scripts\\measure_dvd_alignment.py --episode S01E06
  python scripts\\measure_dvd_alignment.py --episode S01E06 --min-offset -3600 --max-offset 600

Review range-edge hits, score/margin, transitions, and the reverse/scene-cut checks
in the runbook before using any edge in a retime schedule.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402

FPS = 30000.0 / 1001.0
FEATURES = 48 * 36


def normalize(a):
    import numpy as np
    y = a.astype(np.float32, copy=True)
    y -= y.mean(axis=1, keepdims=True)
    n = np.linalg.norm(y, axis=1, keepdims=True)
    n[n == 0] = 1
    return y / n


def load_desc(path: Path, role: str, code: str, source: Path):
    import numpy as np
    if not path.is_file():
        raise PipelineError(f"{role} descriptors missing: {path}; run extract_video_descriptors.py")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("episode") != code or meta.get("role") != role:
            raise PipelineError(f"descriptor metadata mismatch for {path}: episode/role is not {code}/{role}")
        if meta.get("descriptor_size") != [48,36]:
            raise PipelineError(f"unexpected descriptor geometry in {meta_path}")
        if meta.get("output_grid_fps") != "30000/1001":
            raise PipelineError(f"{role} descriptor grid is {meta.get('output_grid_fps')}; direct alignment requires 30000/1001 fps")
        if Path(meta.get("input_path", "")).resolve() != source.resolve():
            raise PipelineError(f"{role} descriptor input is {meta.get('input_path')}, expected {source.resolve()}")
        stat = source.stat()
        if meta.get("input_size") != stat.st_size or meta.get("input_mtime_ns") != stat.st_mtime_ns:
            raise PipelineError(f"{role} source changed since descriptors were extracted; regenerate {path}")
    a = np.load(path, mmap_mode="r")
    if a.ndim != 2 or a.shape[1] != FEATURES or a.shape[0] < 1000:
        raise PipelineError(f"invalid {role} descriptor shape {a.shape}; expected N x {FEATURES}")
    return a


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--master-desc", help="override manifest files.master_descriptors")
    ap.add_argument("--dvd-desc", help="override manifest files.dvd_descriptors")
    ap.add_argument("--min-offset", type=int, default=-3000, help="inclusive DVD-master frame offset search minimum")
    ap.add_argument("--max-offset", type=int, default=500, help="inclusive DVD-master frame offset search maximum")
    ap.add_argument("--lambda", dest="penalty", type=float, default=0.006, help="Viterbi penalty per frame of offset change")
    ap.add_argument("--step", type=int, default=15, help="master-frame sample interval")
    ap.add_argument("--warn-ncc", type=float, default=0.50, help="diagnostic low-correlation marker; does not adjudicate/fail the path")
    ap.add_argument("--transition-threshold", type=int, default=20, help="print transitions at least this many frames; TSV keeps every sample")
    ap.add_argument("--first-master-frame", type=int, help="override initial index; default=max(3000,-min-offset)")
    ap.add_argument("--out", help="override alignment NPY output path")
    ap.add_argument("--report", help="override TSV diagnostics output path")
    ap.add_argument("--force", action="store_true", help="replace existing alignment outputs after preserving them")
    args = ap.parse_args()

    import numpy as np
    if (args.max_offset <= args.min_offset or args.step <= 0 or args.penalty < 0
            or args.transition_threshold < 1 or not -1.0 <= args.warn_ncc <= 1.0):
        raise PipelineError("offset range, sample step, transition threshold, NCC marker, or transition penalty is invalid")
    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("two-part E01 needs per-part descriptor/alignment paths")
    master_path = Path(args.master_desc) if args.master_desc else ep.path("master_descriptors")
    dvd_path = Path(args.dvd_desc) if args.dvd_desc else ep.path("dvd_descriptors")
    out = Path(args.out) if args.out else ep.path("align_npy")
    assert_writable_path(out, ld)
    report = Path(args.report) if args.report else ep.path("alignment_report")
    assert_writable_path(report, ld)
    existing = [p for p in (out, report) if p.exists()]
    if existing and not args.force:
        raise PipelineError(f"refusing to overwrite alignment artifacts: {out}, {report}")
    if existing:
        backups = [p.with_name(p.name + ".bak") for p in existing]
        collision = [p for p in backups if p.exists()]
        if collision:
            raise PipelineError(f"refusing to overwrite existing alignment backups: {collision}")
        for src, backup in zip(existing, backups):
            shutil.copy2(src, backup)
            print(f"preserved prior artifact: {backup}")
    master = load_desc(master_path, "master", ep.code, ep.path("stack_master"))
    dvd = normalize(load_desc(dvd_path, "dvd", ep.code, ep.path("dvd")))

    offs = np.arange(args.min_offset, args.max_offset + 1, dtype=np.int32)
    first = args.first_master_frame if args.first_master_frame is not None else max(3000, -args.min_offset)
    last = len(master) - 300
    idxs = np.arange(first, last, args.step, dtype=np.int32)
    if len(idxs) < 100:
        raise PipelineError(f"alignment range has too few samples: {len(idxs)}")
    states = len(offs)
    emissions = np.full((len(idxs), states), -1.0, np.float32)
    for row, i in enumerate(idxs):
        js = i + offs
        valid = (js >= 0) & (js < len(dvd))
        v = np.array(master[i], dtype=np.float32, copy=True)
        v -= v.mean()
        n = np.linalg.norm(v)
        if n:
            v /= n
        emissions[row, valid] = dvd[js[valid]] @ v
        if row and row % 1000 == 0:
            print(f"emissions {row}/{len(idxs)}", flush=True)

    penalty = args.penalty * np.abs(offs[:,None]-offs[None,:]).astype(np.float32)
    scores = np.empty_like(emissions)
    back = np.zeros(emissions.shape, np.int16)
    scores[0] = emissions[0]
    for row in range(1,len(idxs)):
        prev=scores[row-1][None,:]-penalty
        back[row]=np.argmax(prev,axis=1)
        scores[row]=emissions[row]+prev[np.arange(states),back[row]]
        if row % 1000 == 0:
            print(f"Viterbi {row}/{len(idxs)}",flush=True)
    path=np.empty(len(idxs),np.int32)
    path[-1]=int(np.argmax(scores[-1]))
    for row in range(len(idxs)-2,-1,-1):
        path[row]=back[row+1,path[row+1]]
    offset_path=offs[path]
    selected=emissions[np.arange(len(idxs)),path]
    margin=[]
    for row,state in enumerate(path):
        lo=max(0,int(state)-2);hi=min(states,int(state)+3)
        alternatives=np.concatenate((emissions[row,:lo],emissions[row,hi:]))
        margin.append(float(selected[row]-alternatives.max()) if alternatives.size else 0.0)
    edge=(offset_path==args.min_offset)|(offset_path==args.max_offset)
    print(f"master descriptors={len(master)} DVD descriptors={len(dvd)} samples={len(idxs)} states={states}")
    print(f"state=DVD-master; path range={int(offset_path.min())}..{int(offset_path.max())}; range-edge samples={int(edge.sum())}/{len(edge)}")
    changes=np.flatnonzero(np.diff(offset_path)!=0)+1
    print(f"offset transitions >={args.transition_threshold} frames (full path is in TSV):")
    for i in changes:
        if abs(int(offset_path[i])-int(offset_path[i-1]))>=args.transition_threshold:
            print(f"  master t={idxs[i]/FPS:.2f}s f{idxs[i]}: {int(offset_path[i-1]):+d}->{int(offset_path[i]):+d}, NCC={selected[i]:.3f}, margin={margin[i]:.3f}")
    weak=np.flatnonzero(selected < args.warn_ncc)
    if len(weak):
        groups=[]
        start=prev=int(weak[0])
        for i in weak[1:]:
            i=int(i)
            if i!=prev+1:
                groups.append((start,prev));start=i
            prev=i
        groups.append((start,prev))
        spans=[(a,b) for a,b in groups if b-a+1>=2]
        isolated=sum(b==a for a,b in groups)
        print(f"low-NCC samples (<{args.warn_ncc:.2f}): {len(weak)}/{len(selected)}; "
              f"{isolated} isolated, {len(spans)} multi-sample spans (all samples are in TSV)")
        for a,b in spans:
            print(f"  {idxs[a]/FPS:.2f}..{idxs[b]/FPS:.2f}s ({b-a+1} samples), NCC {selected[a:b+1].min():.3f}..{selected[a:b+1].max():.3f}")

    out.parent.mkdir(parents=True,exist_ok=True);report.parent.mkdir(parents=True,exist_ok=True)
    np.save(out,np.column_stack((idxs,offset_path)))
    with report.open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f,delimiter="\t")
        w.writerow(("master_frame","master_time_s","dvd_minus_master_frames","ncc","local_margin","search_edge"))
        for i,o,sc,mg,ed in zip(idxs,offset_path,selected,margin,edge):
            w.writerow((int(i),f"{i/FPS:.6f}",int(o),f"{sc:.6f}",f"{mg:.6f}",int(ed)))
    print(f"alignment path: {out}")
    print(f"diagnostics: {report}")
    if edge.any():
        print("WARNING: path touches an offset search boundary; widen/review the search before using it.")
    print("This is a candidate path, not an adjudicated break map. Verify reverse matches, scene cuts, and content-edge windows.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
