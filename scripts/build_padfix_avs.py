#!/usr/bin/env python3
"""Build the frame-count-preserving pre-encode PAD-repair AviSynth wrapper.

  python scripts\\build_padfix_avs.py --episode S01E05

The imported source is the measured, source-derived avs_encode chain, not an
already encoded HEVC file. Two frame-count assertions protect the source and
replacement layout before the single final x265 encode.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

FPS = "23.976023976"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--encode", help="override the source-derived AVS path (defaults to avs_encode)")
    ap.add_argument("--out", help="override the AVS output path")
    ap.add_argument("--force", action="store_true", help="replace an existing AVS output after preserving it")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    probe = ld.probe()
    source_avs = Path(args.encode) if args.encode else ep.path("avs_encode")
    total = ep.frame_count
    avs_path = Path(args.out) if args.out else ep.path("avs_padfix")
    audit = probe / f"{ep.code}-pad-audit.tsv"
    repair_file = probe / f"{ep.code}-pad-repair-frames.txt"
    report = probe / "rife-pad-replacements-local.tsv"
    version_path = Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION"
    if not version_path.exists():
        raise PipelineError(f"workflow version marker missing: {version_path}")
    workflow_version = version_path.read_text(encoding="ascii").strip()
    if not workflow_version.isdecimal() or int(workflow_version) < 1:
        raise PipelineError(f"invalid workflow version marker: {workflow_version!r}")

    if avs_path.exists() and not args.force:
        raise PipelineError(f"refusing to overwrite existing AVS: {avs_path}; preserve it and use --force only if intentional")

    for p in (source_avs, audit, repair_file):
        if not p.exists():
            raise PipelineError(f"missing input: {p}")
    if not source_avs.suffix.lower() == ".avs":
        raise PipelineError(f"source must be the source-derived .avs chain, not encoded video: {source_avs}")
    probe_result = subprocess.run(
        [ld.ffprobe(), "-v", "error", "-f", "avisynth", "-i", str(source_avs),
         "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
         "-of", "default=nw=1:nk=1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if probe_result.returncode:
        raise PipelineError(f"could not probe source AVS pixel format: {probe_result.stderr[-1000:]}")
    source_pix_fmt = probe_result.stdout.strip().splitlines()[-1] if probe_result.stdout.strip() else ""
    image_conversion = {
        "yuv420p10le": 'ConvertToYUV420(matrix="Rec601").ConvertBits(10)',
        "yuv422p10le": 'ConvertToYUV422(matrix="Rec601").ConvertBits(10)',
        "yuv444p10le": 'ConvertToYUV444(matrix="Rec601").ConvertBits(10)',
    }.get(source_pix_fmt)
    if image_conversion is None:
        raise PipelineError(f"unsupported source AVS pixel format {source_pix_fmt!r}; expected 10-bit 4:2:0/4:2:2/4:4:4")

    with audit.open(newline="", encoding="utf-8") as f:
        audit_rows = {int(r["frame"]): r for r in csv.DictReader(f, delimiter="\t")}
    frames = sorted({int(x) for x in repair_file.read_text(encoding="utf-8").split() if x.strip()})
    for frame in frames:
        row = audit_rows.get(frame)
        if row is None or int(row.get("is_predicted_pad", 0)) != 1:
            raise PipelineError(f"repair frame {frame} is not a PAD-map-matched audit row")
    if frames and (frames[0] < 0 or frames[-1] >= total):
        raise PipelineError(f"pad frames outside the encode: {frames[:3]}...{frames[-3:]} "
                            f"(frame count {total})")
    label = ep.data.get("pad_report_label") or ep.code
    if frames and not report.exists():
        raise PipelineError(f"replacement report missing: {report} - run rife_padframes.py first")
    records = {}
    if report.exists():
        with report.open(newline="", encoding="utf-8") as f:
            records = {int(r["frame"]): r for r in csv.DictReader(f, delimiter="\t")
                       if r.get("episode") == label}
    missing = [n for n in frames if n not in records]
    if missing:
        raise PipelineError(f"{label}: no replacement method recorded for frames "
                            f"{missing[:5]}{'...' if len(missing) > 5 else ''} - "
                            f"run rife_padframes.py first")
    methods = {n: records[n]["method"] for n in frames}
    images = {}
    for n in frames:
        image_path = Path(records[n]["png"])
        images[n] = image_path if image_path.is_absolute() else probe / image_path
    absent = [str(p) for n, p in images.items() if methods[n] != "hold-prev-scene-cut" and not p.exists()]
    if absent:
        raise PipelineError(f"missing replacement images: {absent[:5]}")

    quote = lambda p: str(p).replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        f'src = Import("{quote(source_avs)}")',
        f'# LD_PIPELINE_WORKFLOW_VERSION={workflow_version}',
        f'# {ep.code} imported source-derived AVS; {len(frames)} confirmed PAD fills; no frame-count change.',
        f'Assert(src.FrameCount() == {total}, "source frame count mismatch: " + String(src.FrameCount()))',
    ]
    if frames:
        remap = ld.tool("avisynth_plugin_dir") / "RemapFrames_x64.dll"
        if not remap.exists():
            raise PipelineError(f"required AviSynth plugin missing: {remap}")
        lines.insert(0, f'LoadPlugin("{quote(remap)}")')
    parts, cursor, img_num = [], 0, 0
    for frame in frames:
        if frame > cursor:
            part = f"blank{len(parts)}"
            lines.append(f"{part} = BlankClip(src, length={frame-cursor}, color=$000000)")
            parts.append(part)
        img_num += 1
        img = f"img{img_num}"
        if methods[frame] == "hold-prev-scene-cut":
            if frame == 0:
                raise PipelineError("cannot hold the previous frame for a pad at frame 0")
            lines.append(f"{img} = src.Trim({frame-1}, {frame-1}) # scene-cut hold, native 10-bit")
        else:
            lines.append(f'{img} = ImageSource("{images[frame]}", start=0, end=0, fps={FPS})')
            lines.append(f"{img} = {img}.{image_conversion}")
            lines.append(f"{img} = AssumeFPS({img}, src)")
        parts.append(img)
        cursor = frame + 1
    if cursor < total:
        part = f"blank{len(parts)}"
        lines.append(f"{part} = BlankClip(src, length={total-cursor}, color=$000000)")
        parts.append(part)
    if frames:
        lines.append(f"replacement = {parts[0]}")
        for part in parts[1:]:
            lines.append(f"replacement = replacement + {part}")
        lines.append('Assert(replacement.FrameCount() == %d, "PAD replacement length mismatch: " + String(replacement.FrameCount()))' % total)
        mapping = " ".join(f"[{n} {n}]" for n in frames)
        lines.append(f'out = ReplaceFramesSimple(src, replacement, mappings="{mapping}")')
        lines.append("return out")
    else:
        lines.append("return src")
    avs_path.parent.mkdir(parents=True, exist_ok=True)
    if avs_path.exists():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup = avs_path.with_name(f"{avs_path.name}.backup-{stamp}")
        shutil.copy2(avs_path, backup)
        print(f"preserved existing AVS: {backup}")
    avs_path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    print(f"{ep.code}: wrote {avs_path}; {len(frames)} mapped frames; clip length {total}; "
          f"AVS bytes {avs_path.stat().st_size}")
    print(f"probe it before encoding:  ffmpeg -f avisynth -i \"{avs_path}\" -frames:v 5 -an -f null -")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
