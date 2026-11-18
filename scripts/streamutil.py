"""Shared console setup and line-stream reader for long-running child processes."""
from __future__ import annotations

import queue
import sys
import threading
from typing import TextIO


def configure_console() -> None:
    """Replace unencodable console characters rather than failing a completed stage."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def start_line_reader(stream: TextIO, *, name: str) -> tuple[queue.Queue[str | None], threading.Thread]:
    """Start a named daemon reader that queues lines and one EOF sentinel."""
    lines: queue.Queue[str | None] = queue.Queue()

    def read_lines() -> None:
        try:
            for line in stream:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_lines, name=name, daemon=True)
    reader.start()
    return lines, reader
