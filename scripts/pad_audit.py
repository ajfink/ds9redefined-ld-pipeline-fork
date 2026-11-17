#!/usr/bin/env python3
"""PAD_1 blip audit for one episode, driven by its manifest.

Replaces the per-episode pad-blip-audit-*.py variants: the episode's reference
capture, alignment offset, head trim and frame count all come from
episodes/<code>.json instead of being pasted into the script.

  python scripts\\pad_audit.py --episode S01E05
  python scripts\\pad_audit.py --episode S01E05 --rescan

Outputs (under the profile's probe root):
  <code>-blackscan-source.txt     full blackdetect log of the source-derived AVS
  <code>-pad-audit.tsv            every single-frame black + classification
  <code>-pad-repair-frames.txt    one frame number per line: the repair set
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # sibling modules (vspipe_util)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ldcommon
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402
from vspipe_util import run_source_ffmpeg  # noqa: E402

W, H = 48, 36
F24 = 24000.0 / 1001.0
BLIP_MAX_S = 0.050
TOLERANCE_FRAMES = 2


def source_id_for_capture(project: Path, capture: str) -> int:
    """Find the Orc source node by its human label, not manifest array order.

    Orc project node order can differ from the manifest's captures order (E05's
    M1 reference is source node 1 while M1 is second in captures[]).
    """
    text = project.read_text(encoding="utf-8", errors="replace")
    for block in re.split(r"(?m)^    - id: ", text)[1:]:
        m = re.match(r"(\d+)", block)
        stage = re.search(r"(?m)^      stage: (\S+)", block)
        label = re.search(r"(?m)^      user_label: TBC Source (.+?)\s*$", block)
        if m and stage and stage.group(1) == "tbc_source" and label and label.group(1) == capture:
            return int(m.group(1))
    raise PipelineError(f"{project}: no TBC source node labeled {capture!r}")


def boundary_orphan_frames(candidate_frames: list[int], matched_candidates: set[int],
                           frame_count: int) -> set[int]:
    """Exclude boundary orphans, while failing closed for boundary repair candidates."""
    if frame_count < 1:
        raise PipelineError("source-derived clip has no frames")
    boundaries = {frame for frame in candidate_frames if frame in (0, frame_count - 1)}
    unsafe = boundaries & matched_candidates
    if unsafe:
        raise PipelineError(
            "a predicted PAD is at a clip boundary; neighbor-based audit is undefined: "
            f"{sorted(unsafe)}")
    return boundaries


def reference_map_ranges(project: Path, source_id: int = 1) -> list[int]:
    """PAD_1 output indices of the frame_map fed by source_id.

    Every project we generate lists sources and their maps in the same order and
    edges each source into exactly one map, so following the edge from source 1
    selects the reference capture's map regardless of how maps are labelled.
    """
    text = project.read_text(encoding="utf-8", errors="replace")
    stages: dict[int, str] = {}
    blocks: dict[int, str] = {}
    for block in re.split(r"(?m)^    - id: ", text)[1:]:
        m = re.match(r"(\d+)", block)
        if not m:
            continue
        node_id = int(m.group(1))
        stage = re.search(r"(?m)^      stage: (\S+)", block)
        stages[node_id] = stage.group(1) if stage else ""
        blocks[node_id] = block
    edges = [(int(a), int(b))
             for a, b in re.findall(r"(?m)^    - from: (\d+)\s*$\n      to: (\d+)", text)]
    map_id = next((b for a, b in edges if a == source_id and stages.get(b) == "frame_map"), None)
    if map_id is None:
        raise PipelineError(f"{project}: no frame_map fed by source {source_id}")
    m = re.search(r"value:\s*([0-9PAD_,\-]+)", blocks[map_id])
    if not m:
        raise PipelineError(f"{project}: frame_map node {map_id} has no ranges value")
    out, pads = 0, []
    for tok in m.group(1).split(","):
        if tok == "PAD_1":
            pads.append(out)
            out += 1
        elif "-" in tok:
            a, b = map(int, tok.split("-"))
            out += b - a + 1
        else:
            out += 1
    return pads


def file_stamp(path: Path) -> str:
    st = path.stat()
    return f"{path.resolve()}\n{st.st_size}\n{st.st_mtime_ns}"


def run_blackdetect(ld: Ld, video: Path, logfile: Path, rescan: bool,
                    expected_frames: int) -> list[tuple]:
    stamp = file_stamp(video)
    meta = logfile.with_suffix(logfile.suffix + ".source")
    cache_ok = logfile.exists() and meta.exists() and meta.read_text(encoding="utf-8") == stamp
    if cache_ok and not rescan:
        print(f"reusing {logfile} (input fingerprint matches)", flush=True)
    else:
        cmd = [ld.ffmpeg(), "-hide_banner", "-nostats"]
        if video.suffix.lower() == ".avs":
            cmd += ["-f", "avisynth", "-i", str(video)]
        else:
            # macOS: no AviSynth demuxer - vspipe feeds y4m on stdin instead.
            cmd += ["-f", "yuv4mpegpipe", "-i", "-"]
        cmd += ["-vf", "blackdetect=d=0:pix_th=0.10:pic_th=0.98", "-an", "-f", "null", "-"]
        p = run_source_ffmpeg(ld, cmd, video, text=True)
        if p.returncode and not re.search(r"black_start:", p.stderr or ""):
            raise PipelineError(f"blackdetect failed ({p.returncode}) on {video}: "
                                f"{(p.stderr or '')[-800:]}")
        # A non-zero exit with a full blackdetect log is a known false alarm
        # (non-monotonic source timestamps); the log itself is what we trust.
        logfile.write_text(p.stderr or "", encoding="utf-8")
        print(f"scanned {video} -> {logfile}", flush=True)
    txt = logfile.read_text(encoding="utf-8", errors="replace")
    frame_counts = re.findall(r"frame=(\d+)", txt)
    if not frame_counts:
        raise PipelineError(f"blackdetect log has no decoded frame count: {logfile}")
    if int(frame_counts[-1]) != expected_frames:
        raise PipelineError(f"blackdetect decoded {frame_counts[-1]} frames, expected {expected_frames}: {logfile}")
    if not (meta.exists() and meta.read_text(encoding="utf-8") == stamp):
        meta.write_text(stamp, encoding="utf-8")
    pat = re.compile(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)")
    return [(float(a), float(b), float(d)) for a, b, d in pat.findall(txt)]


def descriptors(ld: Ld, video: Path, cache: Path, rescan: bool) -> np.ndarray:
    import numpy as np
    stamp = file_stamp(video)
    meta = cache.with_suffix(cache.suffix + ".source")
    if cache.exists() and meta.exists() and meta.read_text(encoding="utf-8") == stamp and not rescan:
        return np.load(cache, mmap_mode="r")
    cmd = [ld.ffmpeg(), "-hide_banner", "-v", "error"]
    if video.suffix.lower() == ".avs":
        cmd += ["-f", "avisynth", "-i", str(video)]
    else:
        cmd += ["-f", "yuv4mpegpipe", "-i", "-"]
    cmd += ["-vf", f"scale={W}:{H}:flags=area,format=gray", "-f", "rawvideo",
            "-pix_fmt", "gray", "-"]
    p = run_source_ffmpeg(ld, cmd, video, text=False)
    if p.returncode:
        raise PipelineError(f"descriptor extraction failed: {p.stderr.decode('utf-8', 'replace')[-600:]}")
    n = len(p.stdout) // (W * H)
    a = np.frombuffer(p.stdout[: n * W * H], np.uint8).reshape(n, H * W).copy()
    np.save(cache, a)
    meta.write_text(stamp, encoding="utf-8")
    return np.load(cache, mmap_mode="r")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True, help="manifest code, e.g. S01E05")
    ap.add_argument("--encode", help="override the source-derived 23.976 video (defaults to avs_encode)")
    ap.add_argument("--rescan", action="store_true", help="re-decode instead of reusing cached log/descriptors")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    video = Path(args.encode) if args.encode else ep.path("avs_encode")
    project = ep.path("stack_project")
    probe = ld.probe()
    for p in (video, project):
        if not p.exists():
            raise PipelineError(f"missing source AVS or stack project: {p}")
    if video.suffix.lower() not in (".avs", ".vpy"):
        raise PipelineError(
            f"PAD discovery must read the source-derived post-IVTC script "
            f"(.avs/.vpy), not encoded video: {video}")
    if video.name.endswith(("-encode-full.mkv", "-23.976.mkv", "-padfixed.mkv")):
        raise PipelineError(f"refusing an encoded file as the PAD source: {video}")

    captures = ep.capture_set
    if ep.reference not in captures:
        raise PipelineError(f"reference {ep.reference!r} is not in captures {captures}")
    source_id = source_id_for_capture(project, ep.reference)
    pads_master = reference_map_ranges(project, source_id=source_id)
    print(f"{ep.code} reference={ep.reference} map pads={len(pads_master)} "
          f"source_AVS={video} frames={ep.frame_count}", flush=True)

    events = run_blackdetect(ld, video, probe / f"{ep.code}-blackscan-source.txt",
                             args.rescan, ep.frame_count)
    blips = [(a, round(a * F24), d) for a, b, d in events if d <= BLIP_MAX_S]
    print(f"blackdetect hits={len(events)} single-frame candidates={len(blips)}")

    # master frame -> output frame: remove the AVS source-pad trim, IVTC 5:4,
    # then the head trim. All three are measured values from the manifest.
    trim_in, head = ep.pad_trim, ep.head_trim

    def to_out(master: int) -> int:
        return (master - trim_in) * 4 // 5 - head

    pred = [(m, to_out(m)) for m in pads_master]
    in_range = [(m, o) for m, o in pred if o >= 0]
    cut_away = [(m, o) for m, o in pred if o < 0]
    print(f"predicted pads in output: {len(in_range)}; removed by head trim: {len(cut_away)} {cut_away}")

    cand = [i for _, i, _ in blips]
    # Greedy minimum-distance bipartite matching: one observed blink can satisfy
    # at most one predicted pad, and repairs target the observed black frame.
    pairs = sorted((abs(c - o), m, o, c) for m, o in in_range for c in cand)
    used_pred, used_cand, matches = set(), set(), []
    for distance, m, o, c in pairs:
        if distance > TOLERANCE_FRAMES:
            break
        if m in used_pred or c in used_cand:
            continue
        used_pred.add(m)
        used_cand.add(c)
        matches.append((m, o, c, c - o))
    unmatched = [(m, o) for m, o in in_range if m not in used_pred]
    print(f"PAD->blip matches within {TOLERANCE_FRAMES} frames: {len(matches)}/{len(in_range)}")
    print(f"predicted pads with no isolated blink: {len(unmatched)} {unmatched}")
    orphans = sorted(set(cand) - used_cand)
    print(f"single-frame blacks with no predicted pad: {len(orphans)} {orphans}")

    import numpy as np
    raw = descriptors(ld, video, probe / f"{ep.code}-desc-source.npy", args.rescan)
    if len(raw) != ep.frame_count:
        raise PipelineError(f"descriptor cache has {len(raw)} frames, expected {ep.frame_count}")
    if len(raw) <= max(cand, default=0):
        raise PipelineError("descriptor cache is shorter than the encode")
    matched_candidates = {candidate for _, _, candidate, _ in matches}
    boundary_orphans = boundary_orphan_frames(cand, matched_candidates, len(raw))
    if boundary_orphans:
        print(f"boundary orphan(s) excluded from neighbor score: {sorted(boundary_orphans)}")
    frames = np.array(cand, dtype=int)
    dmax = np.array([raw[i].max() for i in frames])
    v = raw.astype(np.float32)
    v -= v.mean(axis=1, keepdims=True)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1
    v /= n
    score = np.array([
        np.nan if int(i) in boundary_orphans else
        1.0 - float(np.dot(v[i - 1], v[i + 1]))
        for i in frames
    ], dtype=np.float64)
    zero = np.array([raw[i].max() == 0 for i in range(len(raw))])
    ix = np.arange(1, len(raw) - 1)
    ok = (~zero[ix - 1]) & (~zero[ix]) & (~zero[ix + 1])
    base = 1.0 - np.sum(v[ix[ok] - 1] * v[ix[ok] + 1], axis=1)
    if len(base):
        baseline = f"motion baseline p90/p95={np.percentile(base,90):.4f}/{np.percentile(base,95):.4f}"
    else:
        baseline = "motion baseline unavailable (no non-black triplets)"
    print(f"pure-zero (48x36): {int(np.sum(dmax == 0))}/{len(blips)}; {baseline}")

    audit = probe / f"{ep.code}-pad-audit.tsv"
    with audit.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(("frame", "time", "black_duration", "zero_max", "motion_score",
                    "baseline_percentile", "predicted_master_pad", "is_predicted_pad"))
        for j, (a, i, d) in enumerate(blips):
            near = min(in_range, key=lambda pq: abs(pq[1] - i)) if in_range else (None, None)
            is_pad = near[1] is not None and abs(near[1] - i) <= TOLERANCE_FRAMES
            score_value = float(score[j])
            score_text = f"{score_value:.6f}" if np.isfinite(score_value) else ""
            percentile_text = (f"{100.0 * float((base <= score_value).mean()):.2f}"
                               if np.isfinite(score_value) and len(base) else "")
            w.writerow((i, f"{i/F24:.6f}", f"{d:.6f}", int(dmax[j]), score_text,
                        percentile_text, near[0], int(is_pad)))

    repair = sorted(c for _, _, c, _ in matches)
    repair_file = probe / f"{ep.code}-pad-repair-frames.txt"
    repair_file.write_text("\n".join(str(i) for i in repair) + "\n", encoding="utf-8")
    print(f"audit:      {audit}")
    print(f"repair set: {repair_file} ({len(repair)} frames)")
    print("next: run fullres_check.py on this set before generating RIFE frames")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
