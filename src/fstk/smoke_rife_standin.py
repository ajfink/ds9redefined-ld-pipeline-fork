#!/usr/bin/env python3
"""SMOKE-ONLY stand-in for ``rife-ncnn-vulkan`` (same CLI subset as ``fstk.tools.rife.Rife.command``).

It linearly blends the two frames. It exists so a CPU-only sandbox can exercise the PAD-repair
plumbing end to end; its output is NOT RIFE and must never be used for a real deliverable. The
sandbox profile points ``rife_model_dir`` at a folder named ``rife-v...-STANDIN`` so every PAD-repair
report carries that label.
"""
import argparse
import sys

import cv2
import numpy as np


def main(argv: list[str]) -> int:
    opts: dict[str, str] = {}
    flags = {"-x", "-z"}
    i = 0
    while i < len(argv):                       # argparse cannot cope with "-g -1"
        k = argv[i]
        if k in flags:
            i += 1
            continue
        opts[k] = argv[i + 1] if i + 1 < len(argv) else ""
        i += 2
    try:
        ns = argparse.Namespace(a=opts["-0"], b=opts["-1"], out=opts["-o"], step=float(opts.get("-s", "0.5")))
    except KeyError as exc:
        print(f"standin: missing option {exc}", file=sys.stderr)
        return 2
    a = cv2.imread(ns.a, cv2.IMREAD_UNCHANGED)
    b = cv2.imread(ns.b, cv2.IMREAD_UNCHANGED)
    if a is None or b is None or a.shape != b.shape or a.dtype != b.dtype:
        print("standin: unreadable or mismatched inputs", file=sys.stderr)
        return 2
    mix = a.astype(np.float64) * (1 - ns.step) + b.astype(np.float64) * ns.step
    top = 65535 if a.dtype == np.uint16 else 255
    return 0 if cv2.imwrite(ns.out, np.clip(np.rint(mix), 0, top).astype(a.dtype)) else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
