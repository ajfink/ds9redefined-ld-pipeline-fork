#!/usr/bin/env python3
"""Independent long-window picture measurements around manifest black candidates.

For each `retime.breaks[].ld_black`, this reports the Viterbi offset before/after
and re-matches high-texture master/DVD descriptor windows over +/-N frames. It does
not adjudicate the final cut, decide whether music-through black is merged, or edit
the manifest. Review the score, runner-up, margins, and agreement with the other
methods; if they disagree beyond tolerance, use the Qwen4B-Luna fallback/STOP policy.

Create master/DVD descriptor arrays first with `extract_video_descriptors.py`.

  python scripts\\measure_break_edges.py --episode S01E05
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402
from numeric import normalize_rows as normalize  # noqa: E402

FPS = 30000.0 / 1001.0
FEATURES = 48 * 36


def load_array(path: Path, label: str, code: str, role: str, source: Path):
    import numpy as np
    if not path.is_file(): raise PipelineError(f"{label} missing: {path}; run extract_video_descriptors.py")
    a=np.load(path,mmap_mode='r')
    if a.ndim!=2 or a.shape[1]!=FEATURES: raise PipelineError(f"{label} has invalid shape {a.shape}")
    meta_path=path.with_suffix(path.suffix+'.meta.json')
    if meta_path.is_file():
        import json
        meta=json.loads(meta_path.read_text(encoding='utf-8'))
        if meta.get('episode') != code or meta.get('role') != role:
            raise PipelineError(f"{label} metadata episode/role mismatch in {meta_path}")
        if meta.get('output_grid_fps') != '30000/1001':
            raise PipelineError(f"{label} grid is {meta.get('output_grid_fps')}, expected 30000/1001 fps")
        if Path(meta.get('input_path','')).resolve() != source.resolve():
            raise PipelineError(f"{label} input is {meta.get('input_path')}, expected {source.resolve()}")
        stat=source.stat()
        if meta.get('input_size') != stat.st_size or meta.get('input_mtime_ns') != stat.st_mtime_ns:
            raise PipelineError(f"{label} source changed since extraction; regenerate {path}")
    return a


def dp_median(align, lo_s: float, hi_s: float):
    import numpy as np
    t=align[:,0].astype(np.float64)/FPS
    vals=align[(t>=lo_s)&(t<=hi_s),1]
    if not len(vals): return None
    return float(np.median(vals))


def match_window(master,dvd_norm,lo_s:float,hi_s:float,guess:float,search:int,texture_std:float,min_frames:int):
    import numpy as np
    lo=max(int(lo_s*FPS),0);hi=min(int(hi_s*FPS),len(master))
    if hi<=lo:return None
    raw=np.asarray(master[lo:hi],dtype=np.uint8)
    keep=raw.std(axis=1)>texture_std
    idx=np.flatnonzero(keep)+lo
    if len(idx)<min_frames:return {"offset":None,"score":None,"runner":None,"n":len(idx)}
    a=normalize(raw[keep])
    offsets=np.arange(int(round(guess))-search,int(round(guess))+search+1,dtype=np.int32)
    scores=np.full(len(offsets),-1.0,np.float32)
    for j,o in enumerate(offsets):
        d=idx+o;valid=(d>=0)&(d<len(dvd_norm))
        if valid.sum()>=min_frames:
            scores[j]=float(np.mean(np.sum(a[valid]*dvd_norm[d[valid]],axis=1)))
    k=int(np.argmax(scores));mask=np.ones(len(scores),bool);mask[max(0,k-2):min(len(scores),k+3)]=False
    runner=float(np.max(scores[mask])) if mask.any() else -1.0
    return {"offset":int(offsets[k]),"score":float(scores[k]),"runner":runner,
            "margin":float(scores[k]-runner),"n":int(keep.sum())}


def content_window_bounds(breaks, index: int, window_s: float,
                          fade_clear_s: float) -> dict[str, object | None]:
    """Clip match windows away from neighboring blacks; empty windows are ``None``.

    A close pair can leave no interval that meets the requested fade clearance.
    Represent that explicitly instead of returning a backwards ``(lo, hi)`` pair.
    """
    if not 0 <= index < len(breaks):
        raise PipelineError(f"break index {index} is outside the adjudication list")
    own = breaks[index].get("ld_black")
    if not own or len(own) != 2:
        raise PipelineError(f"break {breaks[index].get('label', index + 1)} needs ld_black=[start,end]")
    b0, b1 = map(float, own)
    if window_s <= 0 or fade_clear_s < 0 or b1 <= b0:
        raise PipelineError("content window, fade clearance, or black interval is invalid")

    before = [max(0.0, b0 - window_s - fade_clear_s), b0 - fade_clear_s]
    after = [b1 + fade_clear_s, b1 + window_s + fade_clear_s]
    clipped_before = clipped_after = False
    previous = []
    following = []
    for j, row in enumerate(breaks):
        if j == index or not row.get("ld_black") or len(row["ld_black"]) != 2:
            continue
        start, end = map(float, row["ld_black"])
        if end <= b0:
            previous.append((start, end))
        elif start >= b1:
            following.append((start, end))
        elif start < b1 and end > b0:
            raise PipelineError("recorded LD black intervals overlap; adjudicate before measuring break edges")

    overlapping_previous = [row for row in previous if row[1] > before[0] and row[0] < before[1]]
    if overlapping_previous:
        before[0] = max(before[0], max(row[1] for row in overlapping_previous) + fade_clear_s)
        clipped_before = True
    overlapping_next = [row for row in following if row[0] < after[1] and row[1] > after[0]]
    if overlapping_next:
        after[1] = min(after[1], min(row[0] for row in overlapping_next) - fade_clear_s)
        clipped_after = True
    before_bounds = tuple(before) if before[1] > before[0] else None
    after_bounds = tuple(after) if after[1] > after[0] else None
    return {"before": before_bounds, "after": after_bounds,
            "before_clipped": clipped_before, "after_clipped": clipped_after}


def match_content_windows(master, dvd_norm, before_bounds, after_bounds,
                          dp_before: float, dp_after: float, search: int,
                          texture_std: float, min_frames: int) -> dict:
    """Match each side independently; preserve a valid side when its pair is unusable.

    The row remains non-measured unless both offsets pass the existing texture gate.
    This is evidence preservation only; it does not infer a missing edge or permit a
    partial result to be written to the manifest.
    """
    before = (match_window(master, dvd_norm, *before_bounds, dp_before, search,
                           texture_std, min_frames) if before_bounds is not None else None)
    after = (match_window(master, dvd_norm, *after_bounds, dp_after, search,
                          texture_std, min_frames) if after_bounds is not None else None)
    if before is None and after is None:
        return {"status": "window-too-small", "before": None, "after": None}
    if before is None or after is None:
        return {"status": "partial-window", "before": before, "after": after}
    before_ok = before is not None and before.get("offset") is not None
    after_ok = after is not None and after.get("offset") is not None
    if not before_ok or not after_ok:
        status = "partial-measurement" if before_ok != after_ok else "insufficient-texture"
        return {"status": status, "before": before, "after": after}
    return {"status": "measured", "before": before, "after": after}


def report_exit_code(rows: list[dict]) -> int:
    """An incomplete/empty report is not a successful set of break measurements."""
    return 0 if rows and all(row.get("status") == "measured" for row in rows) else 1


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--episode',required=True)
    ap.add_argument('--window',type=float,default=48.0,help='content-window duration in seconds')
    ap.add_argument('--fade-clear',type=float,default=2.0)
    ap.add_argument('--search-frames',type=int,default=40)
    ap.add_argument('--texture-std',type=float,default=10.0)
    ap.add_argument('--min-frames',type=int,default=300)
    ap.add_argument('--out')
    ap.add_argument('--force',action='store_true')
    args=ap.parse_args()
    import numpy as np
    ld=Ld();ep=load_episode(args.episode,ld)
    master=load_array(ep.path('master_descriptors'),'master descriptors',ep.code,'master',ep.path('stack_master'))
    dvd=normalize(load_array(ep.path('dvd_descriptors'),'DVD descriptors',ep.code,'dvd',ep.path('dvd')))
    align=np.load(ep.path('align_npy'))
    if align.ndim!=2 or align.shape[1]<2: raise PipelineError(f"bad alignment array shape {align.shape}")
    breaks=(ep.data.get('retime') or {}).get('breaks') or []
    if not breaks: raise PipelineError(f"{ep.code}: record candidate retime.breaks[].ld_black intervals first")
    out=Path(args.out) if args.out else (ep.path("break_edge_report") if ep.data.get("files",{}).get("break_edge_report") else ld.probe()/f"{ep.code}-break-edge-measurements.tsv")
    assert_writable_path(out, ld)
    if out.exists():
        if not args.force: raise PipelineError(f"refusing to overwrite edge report: {out}")
        backup=out.with_name(out.name+'.bak')
        if backup.exists(): raise PipelineError(f"refusing to overwrite existing edge-report backup: {backup}")
        shutil.copy2(out,backup)
        print(f"preserved prior artifact: {backup}")
    rows=[]
    for i,b in enumerate(breaks,1):
        black=b.get('ld_black')
        if not black or len(black)!=2: raise PipelineError(f"break {b.get('label',i)} needs measured ld_black=[start,end]")
        b0,b1=map(float,black)
        windows=content_window_bounds(breaks,i-1,args.window,args.fade_clear)
        before_bounds=windows['before']
        after_bounds=windows['after']
        before_lo,before_hi=before_bounds if before_bounds is not None else (None,None)
        after_lo,after_hi=after_bounds if after_bounds is not None else (None,None)
        dpb=dp_median(align,b0-45,b0-args.fade_clear-1/FPS)
        dpa=dp_median(align,b1+args.fade_clear+1/FPS,b1+45)
        if dpb is None or dpa is None:
            rows.append({"label":b.get('label',f"B{i}"),"status":"no-DP-samples","ld_black_start":b0,"ld_black_end":b1,
                         "window_before_start":before_lo,"window_before_end":before_hi,
                         "window_after_start":after_lo,"window_after_end":after_hi,
                         "before_window_clipped":windows['before_clipped'],
                         "after_window_clipped":windows['after_clipped']})
            continue
        matched=match_content_windows(master,dvd,before_bounds,after_bounds,dpb,dpa,
                                      args.search_frames,args.texture_std,args.min_frames)
        before,after=matched["before"],matched["after"]
        if matched["status"] != "measured":
            row={"label":b.get('label',f"B{i}"),"status":matched["status"],"ld_black_start":b0,"ld_black_end":b1,
                          "dp_before":dpb,"dp_after":dpa,
                          "content_before":before.get('offset') if before else None,
                          "ncc_before":before.get('score') if before else None,
                          "runner_before":before.get('runner') if before else None,
                          "margin_before":before.get('margin') if before else None,
                          "n_before":before.get('n') if before else None,
                          "content_after":after.get('offset') if after else None,
                          "ncc_after":after.get('score') if after else None,
                          "runner_after":after.get('runner') if after else None,
                          "margin_after":after.get('margin') if after else None,
                          "n_after":after.get('n') if after else None,
                         "window_before_start":before_lo,"window_before_end":before_hi,
                         "window_after_start":after_lo,"window_after_end":after_hi,
                         "before_window_clipped":windows['before_clipped'],
                         "after_window_clipped":windows['after_clipped']}
            rows.append(row)
            print(f"{row['label']}: status={row['status']} "
                  f"before={before_bounds} after={after_bounds}; offsets remain unmeasured",flush=True)
            continue
        step=after["offset"]-before["offset"]
        row={"label":b.get('label',f"B{i}"),"status":"measured","ld_black_start":b0,"ld_black_end":b1,
             "dvd_silence_start":(b.get('dvd_silence') or [None,None])[0],"dvd_silence_end":(b.get('dvd_silence') or [None,None])[1],
             "dp_before":dpb,"content_before":before['offset'],"ncc_before":before['score'],"runner_before":before['runner'],"margin_before":before['margin'],"n_before":before['n'],
             "dp_after":dpa,"content_after":after['offset'],"ncc_after":after['score'],"runner_after":after['runner'],"margin_after":after['margin'],"n_after":after['n'],"measured_step":step,
              "manifest_off_before":b.get('off_before'),"manifest_off_after":b.get('off_after'),
              "window_before_start":before_lo,"window_before_end":before_hi,
              "window_after_start":after_lo,"window_after_end":after_hi,
              "before_window_clipped":windows['before_clipped'],
              "after_window_clipped":windows['after_clipped'],
             "delta_before":(before['offset']-float(b['off_before'])) if b.get('off_before') is not None else None,
             "delta_after":(after['offset']-float(b['off_after'])) if b.get('off_after') is not None else None}
        rows.append(row)
        print(f"{row['label']}: content {row['content_before']:+d}->{row['content_after']:+d} step={step:+d}; "
              f"NCC={row['ncc_before']:.4f}/{row['ncc_after']:.4f}; margins={row['margin_before']:.4f}/{row['margin_after']:.4f}; "
              f"DP={dpb:+.1f}/{dpa:+.1f}",flush=True)
    out.parent.mkdir(parents=True,exist_ok=True)
    keys=[]
    for row in rows:
        for k in row:
            if k not in keys:keys.append(k)
    with out.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=keys,delimiter='\t');w.writeheader();w.writerows(rows)
    print(f"report: {out}")
    print("Adjudicate offsets with content/plateau/DP/black-start evidence; this report does not edit the manifest.")
    return report_exit_code(rows)


if __name__=='__main__':
    try:raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}",file=sys.stderr);raise SystemExit(2)
