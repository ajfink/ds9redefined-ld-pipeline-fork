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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

FPS = 30000.0 / 1001.0
FEATURES = 48 * 36


def normalize(a):
    import numpy as np
    x=a.astype(np.float32,copy=True);x-=x.mean(axis=1,keepdims=True)
    n=np.linalg.norm(x,axis=1,keepdims=True);n[n==0]=1
    return x/n


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
        dpb=dp_median(align,b0-45,b0-args.fade_clear-1/FPS)
        dpa=dp_median(align,b1+args.fade_clear+1/FPS,b1+45)
        if dpb is None or dpa is None:
            rows.append({"label":b.get('label',f"B{i}"),"status":"no-DP-samples","ld_black_start":b0,"ld_black_end":b1})
            continue
        before=match_window(master,dvd,b0-args.window-args.fade_clear,b0-args.fade_clear,dpb,args.search_frames,args.texture_std,args.min_frames)
        after=match_window(master,dvd,b1+args.fade_clear,b1+args.window+args.fade_clear,dpa,args.search_frames,args.texture_std,args.min_frames)
        if before["offset"] is None or after["offset"] is None:
            rows.append({"label":b.get('label',f"B{i}"),"status":"insufficient-texture","ld_black_start":b0,"ld_black_end":b1,"dp_before":dpb,"dp_after":dpa,"n_before":before['n'],"n_after":after['n']})
            continue
        step=after["offset"]-before["offset"]
        row={"label":b.get('label',f"B{i}"),"status":"measured","ld_black_start":b0,"ld_black_end":b1,
             "dvd_silence_start":(b.get('dvd_silence') or [None,None])[0],"dvd_silence_end":(b.get('dvd_silence') or [None,None])[1],
             "dp_before":dpb,"content_before":before['offset'],"ncc_before":before['score'],"runner_before":before['runner'],"margin_before":before['margin'],"n_before":before['n'],
             "dp_after":dpa,"content_after":after['offset'],"ncc_after":after['score'],"runner_after":after['runner'],"margin_after":after['margin'],"n_after":after['n'],"measured_step":step,
             "manifest_off_before":b.get('off_before'),"manifest_off_after":b.get('off_after'),
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
    return 0 if all(r.get('status')=='measured' for r in rows) else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}",file=sys.stderr);raise SystemExit(2)
