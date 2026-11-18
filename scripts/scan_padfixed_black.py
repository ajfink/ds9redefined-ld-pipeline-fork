#!/usr/bin/env python3
"""Full-decode + black/PAD-alignment scan of the delivered pad-fixed encode.

  python scripts\\scan_padfixed_black.py --episode S01E05

Gate: no single-frame black may sit at a confirmed PAD position. Exit 1 if any
does. Writes <code>-padfixed-blackscan.txt with the raw blackdetect log.
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

F24 = 24000.0 / 1001.0
BLIP_MAX_S = 0.10          # 1 frame is 0.042 s; allow a little encoder slack
ALIGN_TOL_S = 1.25 / F24   # +/-1.25 output frames


def resolve_video_input(ep, override: str | None = None) -> Path:
    """Prefer the declared deliverable, then the intermediate padfixed encode.

    Some episode templates call the final mux ``pre_upscale`` rather than
    ``padfixed``. The explicit ``--encode`` override still wins, but the
    manifest's deliverable declaration is the correct no-argument default.
    """
    if override:
        return Path(override).expanduser()
    deliverable_key = ep.data.get("deliverable")
    if deliverable_key:
        deliverable = ep.optional_path(str(deliverable_key))
        if deliverable is not None and deliverable.is_file():
            return deliverable
    return ep.path("padfixed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--encode", help="override the delivered pad-fixed file")
    ap.add_argument("--rescan", action="store_true")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    probe = ld.probe()
    video = resolve_video_input(ep, args.encode)
    repair_file = probe / f"{ep.code}-pad-repair-frames.txt"
    if not video.exists():
        raise PipelineError(f"missing deliverable: {video} - run encode+remux first")
    if not repair_file.exists():
        raise PipelineError(f"missing confirmed PAD repair list: {repair_file} - run pad_audit.py first")
    pads = sorted({int(x) for x in repair_file.read_text(encoding="utf-8").split() if x.strip()})

    logfile = probe / f"{ep.code}-blackscan-{video.stem}.txt"
    st = video.stat()
    stamp = f"{video.resolve()}\n{st.st_size}\n{st.st_mtime_ns}"
    meta = logfile.with_suffix(logfile.suffix + ".source")
    cache_ok = logfile.exists() and meta.exists() and meta.read_text(encoding="utf-8") == stamp
    if cache_ok and not args.rescan:
        print(f"reusing {logfile} (input fingerprint matches)")
        log = logfile.read_text(encoding="utf-8", errors="replace")
    else:
        cmd = [ld.ffmpeg(), "-hide_banner", "-v", "info", "-i", str(video),
               "-vf", "blackdetect=d=0:pix_th=0.10:pic_th=0.98", "-an", "-f", "null", "-"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        log = p.stderr or ""
        logfile.write_text(log, encoding="utf-8")
        if p.returncode and "frame=" not in log:
            raise PipelineError(f"decode/blackscan failed ({p.returncode}): {log[-1200:]}")
        # A non-zero exit with a complete frame count is the known DTS false alarm.
        print(f"scanned {video} -> {logfile}")

    pat = re.compile(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)")
    events = [(float(a), float(b), float(d)) for a, b, d in pat.findall(log)]
    frames_line = re.findall(r"frame=(\d+)", log)
    if not frames_line:
        raise PipelineError(f"blackdetect log has no decoded frame count: {logfile}")
    # the log carries periodic progress lines; only the last is the total
    got = int(frames_line[-1])
    if got != ep.frame_count:
        raise PipelineError(f"decoded {got} frames, manifest says {ep.frame_count}")
    if not (meta.exists() and meta.read_text(encoding="utf-8") == stamp):
        meta.write_text(stamp, encoding="utf-8")
    print(f"decoded frames: {got} (matches manifest)")

    unresolved = []
    for frame in pads:
        t = frame / F24
        hits = [(a, d) for a, b, d in events if d <= BLIP_MAX_S and abs(a - t) <= ALIGN_TOL_S]
        if hits:
            unresolved.append((frame, round(t, 3), [(round(h[0], 3), h[1]) for h in hits]))
    print(f"blackdetect hits={len(events)}; PAD positions={len(pads)}")
    if unresolved:
        print(f"FAIL - PAD-aligned single-black hits remain ({len(unresolved)}):", file=sys.stderr)
        for u in unresolved[:20]:
            print(f"  frame {u[0]} @ {u[1]}s {u[2]}", file=sys.stderr)
        return 1
    print(f"PASS - 0/{len(pads)} PAD-aligned single-black hits remain")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
