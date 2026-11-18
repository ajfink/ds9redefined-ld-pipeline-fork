#!/usr/bin/env python3
"""Print UTF-8, UTF-16LE, or mixed PowerShell/UTF-8 stage logs safely."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _looks_utf16le(line: bytes) -> bool:
    sample = line[:512]
    if len(sample) < 4:
        return False
    pairs = max(1, len(sample) // 2)
    return sample.count(b"\x00") / pairs >= 0.22


def decode_log(data: bytes) -> str:
    """Decode common PowerShell BOM files and UTF-8 logs, including mixed appends."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    decoded = []
    pieces = data.split(b"\n")
    previous_utf16 = False
    for index, piece in enumerate(pieces):
        has_newline = index < len(pieces) - 1
        if previous_utf16 and piece.startswith(b"\x00"):
            # split(b"\n") separates the second byte of a UTF-16LE newline.
            piece = piece[1:]
        if _looks_utf16le(piece + (b"\n" if has_newline else b"")):
            encoded_line = piece + (b"\n\x00" if has_newline else b"")
            decoded.append(encoded_line.decode("utf-16-le", errors="replace").lstrip("\ufeff"))
            previous_utf16 = has_newline
        else:
            decoded.append(piece.decode("utf-8", errors="replace"))
            if has_newline:
                decoded.append("\n")
            previous_utf16 = False
    return "".join(decoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="stage log file to display")
    args = parser.parse_args()
    try:
        text = decode_log(args.path.read_bytes())
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass
    stream.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
