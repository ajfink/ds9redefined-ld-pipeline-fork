#!/usr/bin/env python3
"""Generate the ASCII companion from the main Markdown runbook in this folder."""
from __future__ import annotations

import argparse
import re
import unicodedata
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent
TRANS = {
    0x2192: " -> ", 0x2194: " <-> ", 0x2019: "'", 0x2018: "'",
    0x201C: '"', 0x201D: '"', 0x2014: "-", 0x2013: "-", 0x00D7: "x",
    0x2265: ">=", 0x2264: "<=", 0x2248: "~", 0x03C3: "sigma",
    0x00B1: "+/-", 0x0394: "delta", 0x2026: "...", 0x2212: "-",
    0x00A0: " ",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(BUNDLE / "DS9 LaserDisc OpenCode Runbook.md"))
    ap.add_argument("--output", default=str(BUNDLE / "DS9 LaserDisc Plain Text Instructions.txt"))
    args = ap.parse_args()
    if (BUNDLE / "devices").is_dir():
        print("shared release root is read-only; regenerate in the authoring checkout and republish the release",
              file=sys.stderr)
        return 2
    text = Path(args.source).read_text(encoding="utf-8")
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^```[^\n]*\n?", "", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = unicodedata.normalize("NFKD", text.translate(TRANS)).encode("ascii", "ignore").decode("ascii")
    text = "\n".join(line.rstrip() for line in text.splitlines()).rstrip() + "\n"
    Path(args.output).write_text(text, encoding="ascii", newline="\r\n")
    if not Path(args.output).read_bytes().isascii():
        raise SystemExit("ASCII companion contains non-ASCII bytes")
    print(f"wrote {args.output} ({len(text)} chars, ASCII-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
