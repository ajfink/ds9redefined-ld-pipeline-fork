#!/usr/bin/env python3
"""Emit the frame-count-preserving padfix **VapourSynth** script (macOS port).

    python scripts/build_padfix_vpy.py --episode S01E10

Counterpart of `build_padfix_avs.py`, which emits an AviSynth script and cannot
run here (AviSynth+ is Windows-only; `encode_padfixed.ps1` even refuses an input
that is not `.avs`). This re-expresses the same procedure and re-derives nothing:
pad frames still come from `pad_audit.py`, replacement images/methods from
`rife_padframes.py`.

The emitted `.vpy` imports `load_payloads()` from **this** module so the parsing
and pixel conversion live in exactly one tested place rather than being
duplicated into generated text.

What carries over unchanged
---------------------------
* Two independent frame-count assertions (source == replacement == output ==
  `video.frames`), raised as hard checks before `set_output()`. A silently wrong
  layout is what once cost a 3.5 h encode (39 black + 43 wrong-content pads).
* Pad set = audit rows with `zero_max == 0`, bounds-checked against `video.frames`.
* `hold-prev-scene-cut` uses the native 10-bit source frame `n-1` (never a PNG)
  and is rejected at frame 0.
* Refuses to overwrite an existing output unless `--force`.

Documented substitutions (AviSynth -> VapourSynth)
--------------------------------------------------
| AviSynth | VapourSynth |
|---|---|
| `FFVideoSource(...)` + `LoadPlugin(ffms2.dll)` | `core.bs.VideoSource(...)` (bestsource) |
| `LoadPlugin(RemapFrames_x64.dll)` + `ReplaceFramesSimple(src, repl, mappings=…)` | `core.std.FrameEval` at the pad indices — proved to replace exactly the requested frames while preserving frame count |
| `ImageSource(png)` | Pillow reads the PNG; planes injected with `std.ModifyFrame` — this environment has **no** image-file plugin (`imwri`/`vsfpng` absent) |
| `ConvertToYUV420/422/444(matrix="Rec601").ConvertBits(10)` | `rgb_to_yuv_p10(rgb, sw, sh)`: BT.601, RGB full -> YUV limited, chroma averaged over the **source's own** subsampling, x1023/255 |
| `Import("<avs_encode>")` | `exec()` of the source-derived `.vpy` in a private namespace, taking its `out` |
| `src.FrameCount()` / `Assert(…)` | `src.num_frames` checks with the same message intent |

Injected pixels are still verified by the unchanged `verify_pad_identities.py`
gate (own-NCC vs the PNG >= 0.99), so a conversion drift fails closed.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402


def rgb_to_yuv_p10(rgb, subsampling_w: int = 1, subsampling_h: int = 1):
    """RGB 8-bit full range -> (Y, U, V) 10-bit limited range at the given subsampling.

    Mirrors AviSynth ``ConvertToYUV42X(matrix="Rec601").ConvertBits(10)``:
    BT.601 coefficients, RGB 0-255 -> Y 16-235 / C 16-240, then the 8->10 scale
    of 1023/255, with chroma averaged over ``2**subsampling_w`` x
    ``2**subsampling_h`` blocks.

    The subsampling is taken from the **source-derived chain**, not assumed:
    V5's ``build_padfix_avs.py`` selects ConvertToYUV420/422/444 to match the
    probe of the source AVS, and this episode's source chain is YUV422P10 (a
    ProRes 422 stack master), so a hard 4:2:0 here would either reject a valid
    source or write chroma at the wrong width.
    """
    import numpy as np

    bh, bw = 1 << subsampling_h, 1 << subsampling_w
    h, w = rgb.shape[0], rgb.shape[1]
    if h % bh or w % bw:
        raise PipelineError(
            f"raster {w}x{h} is not divisible by the source chroma block "
            f"{bw}x{bh} (subsampling {subsampling_w}x{subsampling_h})")

    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)

    y8 = 16.0 + (0.299 * r + 0.587 * g + 0.114 * b) * 219.0 / 255.0
    u8 = 128.0 + (-0.168736 * r - 0.331264 * g + 0.5 * b) * 224.0 / 255.0
    v8 = 128.0 + (0.5 * r - 0.418688 * g - 0.081312 * b) * 224.0 / 255.0

    scale = 1023.0 / 255.0
    y = np.rint(y8 * scale).clip(0, 1023).astype(np.uint16)
    u = np.rint(u8 * scale).clip(0, 1023).astype(np.uint16)
    v = np.rint(v8 * scale).clip(0, 1023).astype(np.uint16)
    if bw > 1 or bh > 1:
        u = u.reshape(h // bh, bh, w // bw, bw).mean(axis=(1, 3))
        v = v.reshape(h // bh, bh, w // bw, bw).mean(axis=(1, 3))
    return (y, np.rint(u).clip(0, 1023).astype(np.uint16),
            np.rint(v).clip(0, 1023).astype(np.uint16))


def rgb_to_yuv420p10(rgb):
    """Back-compatible 4:2:0 entry point (the original signature)."""
    return rgb_to_yuv_p10(rgb, 1, 1)


def load_source_chain(script: Path):
    """Evaluate the source-derived chain and return the clip it would output.

    V5's ``build_padfix_avs.py`` does this with AviSynth ``Import("<avs>")``.
    VapourSynth has no ``Import``, so the file is executed in a private
    namespace and its output clip is taken from there.

    The base script also calls ``out.set_output()``. That registration is
    suppressed for the duration of the exec so that only the padfix script
    ever publishes an output - relying on "last registration wins" would work
    on R82 today but would silently encode the *unrepaired* chain if the
    ordering ever changed. The original method is always restored in
    ``finally``.
    """
    import vapoursynth as vs
    if not script.is_file():
        raise PipelineError(f"source-derived chain missing: {script}")
    text = script.read_text(encoding="utf-8")
    ns = {"__name__": "__ld_padfix_base__", "__file__": str(script)}
    suppressed: list = []
    original = vs.VideoNode.set_output
    try:
        vs.VideoNode.set_output = lambda self, *a, **k: suppressed.append(self)
        exec(compile(text, str(script), "exec"), ns)
    except Exception as e:                      # noqa: BLE001 - re-raised below
        raise PipelineError(f"could not evaluate source-derived chain {script}: {e}")
    finally:
        vs.VideoNode.set_output = original
    clip = ns.get("out")
    if not isinstance(clip, vs.VideoNode):
        clip = suppressed[0] if suppressed else None
    if not isinstance(clip, vs.VideoNode):
        raise PipelineError(
            f"{script} published no clip (no `out` and no set_output()); "
            f"cannot import the source-derived chain")
    return clip


def load_payloads(audit: str | Path, report: str | Path, label: str, src) -> dict:
    """Pad frames -> (Y, U, V) uint16 planes, loaded from the pad audit + RIFE report.

    Shared by this generator and by the emitted `.vpy`, so the parsing and the
    colour conversion exist in one place.
    """
    import numpy as np
    from PIL import Image

    # Generated .vpy scripts embed these filenames as string literals. Coerce
    # at the loader boundary as well as in the emitter so either representation
    # is safe and the generated script cannot fail late at encode time.
    audit = Path(audit)
    report = Path(report)

    fmt = src.format
    if fmt is None:
        raise PipelineError("source has variable format; padfix needs a constant format")
    sw, sh = fmt.subsampling_w, fmt.subsampling_h

    with audit.open(newline="", encoding="utf-8") as f:
        frames = sorted(int(r["frame"]) for r in csv.DictReader(f, delimiter="\t")
                        if int(r["zero_max"]) == 0)
    if not frames:
        raise PipelineError("no zero_max==0 PAD frames in the audit")
    total = src.num_frames
    if frames[0] < 0 or frames[-1] >= total:
        raise PipelineError(
            f"pad frames outside the encode: {frames[:3]}...{frames[-3:]} "
            f"(frame count {total})")

    with report.open(newline="", encoding="utf-8") as f:
        records = {int(r["frame"]): r for r in csv.DictReader(f, delimiter="\t")
                   if r.get("episode") == label}
    missing = [n for n in frames if n not in records]
    if missing:
        raise PipelineError(
            f"{label}: no replacement method recorded for frames "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''} - run rife_padframes.py first")

    payloads, held = {}, 0
    for n in frames:
        method = records[n]["method"]
        if method == "hold-prev-scene-cut":
            if n == 0:
                raise PipelineError("cannot hold the previous frame for a pad at frame 0")
            pf = src.get_frame(n - 1)
            payloads[n] = (np.array(pf[0], copy=True), np.array(pf[1], copy=True),
                           np.array(pf[2], copy=True))
            held += 1
        else:
            p = Path(records[n]["png"])
            p = p if p.is_absolute() else Path(audit).parent / p
            if not p.exists():
                raise PipelineError(f"missing replacement image: {p}")
            rgb = np.array(Image.open(p).convert("RGB"), dtype=np.uint8)
            if rgb.shape[:2] != (src.height, src.width):
                raise PipelineError(
                    f"pad {n}: image {p.name} is {rgb.shape[1]}x{rgb.shape[0]}, "
                    f"source is {src.width}x{src.height}")
            payloads[n] = rgb_to_yuv_p10(rgb, sw, sh)
    print(f"  payloads: {len(payloads)} pads ({held} hold-prev-scene-cut, "
          f"{len(payloads) - held} from PNG)")
    return payloads


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--encode", help="override the source encode path")
    ap.add_argument("--out", help="override the .vpy output path")
    ap.add_argument("--force", action="store_true",
                    help="replace an existing .vpy after preserving it")
    ap.add_argument("--dry-run", action="store_true", help="print the script, do not write")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    probe = ld.probe()
    # V5 contract: the padfix script imports the *source-derived* chain. It
    # must never be derived from an encoded MKV - encode_full is an output of
    # a later stage, and rebuilding pads from it would re-quantise every frame.
    video = Path(args.encode) if args.encode else ep.path("avs_encode")
    total = ep.frame_count
    out_path = Path(args.out) if args.out else Path(
        str(ep.path("avs_padfix")).replace(".avs", ".vpy"))
    assert_writable_path(out_path, ld)
    audit = probe / f"{ep.code}-pad-audit.tsv"
    report = probe / "rife-pad-replacements-local.tsv"
    label = ep.data.get("pad_report_label") or ep.code
    version_path = Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION"
    if not version_path.exists():
        raise PipelineError(f"workflow version marker missing: {version_path}")
    workflow_version = version_path.read_text(encoding="ascii").strip()
    if not workflow_version.isdecimal() or int(workflow_version) < 1:
        raise PipelineError(f"invalid workflow version marker: {workflow_version!r}")

    if video.suffix.lower() not in (".vpy", ".avs"):
        raise PipelineError(
            f"padfix must import the source-derived script, not encoded video: {video}")
    if out_path.exists() and not args.force:
        raise PipelineError(
            f"refusing to overwrite existing script: {out_path}; preserve it and "
            f"use --force only if intentional")
    for p in (video, audit, report):
        if not p.exists():
            raise PipelineError(f"missing input: {p}")

    import vapoursynth as vs
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise PipelineError("missing Python package Pillow - pip install pillow")

    src = load_source_chain(video)
    if src.num_frames != total:
        raise PipelineError(
            f"source frame count mismatch: {src.num_frames} != manifest {total}")
    fmt = src.format
    if fmt is None:
        raise PipelineError("source has variable format; padfix needs a constant format")
    # V5's build_padfix_avs.py accepts 4:2:0/4:2:2/4:4:4 and picks the
    # matching ConvertToYUV42X. This episode's source chain is YUV422P10
    # (ProRes 422 stack master), so 4:2:2 must be legal here too.
    if (fmt.subsampling_w, fmt.subsampling_h) not in ((0, 0), (1, 0), (1, 1)):
        raise PipelineError(
            f"unsupported source subsampling {fmt.subsampling_w}x{fmt.subsampling_h}; "
            f"expected 4:4:4, 4:2:2 or 4:2:0")
    if fmt.bits_per_sample != 10:
        raise PipelineError(
            f"source is {fmt.bits_per_sample}-bit; the AviSynth path asserted "
            f"ConvertBits(10) - record/produce a 10-bit encode first")

    # validate + build payloads up front so a bad input fails here, not at encode time
    payloads = load_payloads(audit, report, label, src)
    padset = sorted(payloads)
    del payloads                      # the emitted script reloads them itself

    scripts_dir = Path(__file__).resolve().parent
    text = f'''# Generated by scripts/build_padfix_vpy.py - VapourSynth port of the padfix
# .avs written by build_padfix_avs.py. Pad frames come from pad_audit.py +
# rife_padframes.py; nothing is re-derived here.
# LD_PIPELINE_WORKFLOW_VERSION={workflow_version}
#
# {ep.code}: source frame count {total}; {len(padset)} PAD fills; no frame-count change.
#
# Loader notes: this imports the source-derived chain (the equivalent of
# AviSynth Import("<avs_encode>")) and never opens encoded video; payload
# construction lives in build_padfix_vpy.load_payloads so the parsing/colour
# conversion is not duplicated into generated text.

import sys
from pathlib import Path

import numpy as np
import vapoursynth as vs

sys.path.insert(0, {str(scripts_dir)!r})
from build_padfix_vpy import load_payloads, load_source_chain

core = vs.core
# imports source-derived chain (AviSynth Import("<avs_encode>") equivalent):
BASE = {str(video)!r}
AUDIT = {str(audit)!r}
REPORT = {str(report)!r}
LABEL = {label!r}
TOTAL = {total}
PADS = frozenset({padset!r})

src = load_source_chain(Path(BASE))
assert src.num_frames == TOTAL, f"source frame count mismatch: {{src.num_frames}}"

payloads = load_payloads(Path(AUDIT), Path(REPORT), LABEL, src)

def _write(n, f):
    if n not in payloads:
        return f
    nf = f.copy()
    y, u, v = payloads[n]
    np.copyto(np.asarray(nf[0]), y)
    np.copyto(np.asarray(nf[1]), u)
    np.copyto(np.asarray(nf[2]), v)
    return nf

replacement = core.std.ModifyFrame(src, clip=src, selector=_write)
assert replacement.num_frames == TOTAL, f"PAD replacement length mismatch: {{replacement.num_frames}}"

out = core.std.FrameEval(
    src, lambda n, f=None: replacement if n in PADS else src)
assert out.num_frames == TOTAL, f"output frame count mismatch: {{out.num_frames}}"
out.set_output()
'''
    if args.dry_run:
        print(text)
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"{ep.code}: wrote {out_path}; {len(padset)} mapped frames; clip length {total}; "
          f"bytes {out_path.stat().st_size}")
    print(f"workflow version header: LD_PIPELINE_WORKFLOW_VERSION={workflow_version}")
    print("probe it before encoding:")
    print(f'  python scripts/encode_episode_mac.py --episode {ep.code} --pass validate')
    print(f"  (or) vspipe -c y4m \"{out_path}\" - | "
          f"ffmpeg -f yuv4mpegpipe -i - -frames:v 5 -an -f null -")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
