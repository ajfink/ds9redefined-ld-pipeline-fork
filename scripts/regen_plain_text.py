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


def generate_text(source_text: str) -> str:
    """Convert the canonical Markdown into normalized ASCII-only plain text."""
    text = re.sub(r"(?m)^#{1,6}\s*", "", source_text)
    text = re.sub(r"(?m)^```[^\n]*\n?", "", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = unicodedata.normalize("NFKD", text.translate(TRANS)).encode("ascii", "ignore").decode("ascii")
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip() + "\n"


def output_bytes(text: str) -> bytes:
    """Return the exact CRLF-terminated ASCII bytes written by the generator."""
    return text.replace("\n", "\r\n").encode("ascii")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(BUNDLE / "DS9 LaserDisc OpenCode Runbook.md"))
    ap.add_argument("--output", default=str(BUNDLE / "DS9 LaserDisc Plain Text Instructions.txt"))
    ap.add_argument("--check", action="store_true",
                    help="verify the ASCII companion is current without writing")
    args = ap.parse_args(argv)
    # Authoring checkouts may have device workspaces; published release copies
    # without Git metadata stay application/source read-only.
    if not args.check and (BUNDLE / "devices").is_dir() and not (BUNDLE / ".git").exists():
        print("shared release root is read-only; regenerate in the authoring checkout and republish the release",
              file=sys.stderr)
        return 2
    text = generate_text(Path(args.source).read_text(encoding="utf-8"))
    output = Path(args.output)
    expected = output_bytes(text)
    if args.check:
        try:
            current = output.read_bytes()
        except OSError:
            current = None
        if current != expected:
            print(f"ASCII companion is stale or missing: {output}", file=sys.stderr)
            return 1
        print(f"ASCII companion is current: {output}")
        return 0
    output.write_bytes(expected)
    print(f"wrote {args.output} ({len(text)} chars, ASCII-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
