#!/usr/bin/env python3
"""Small cross-platform helpers for supervised stage subprocesses."""
from __future__ import annotations

import os
import signal
import subprocess
from typing import TextIO


def safe_console_write(text: str, stream: TextIO | None = None) -> None:
    """Write decoded UTF-8 subprocess output even to legacy Windows consoles."""
    import sys

    stream = stream or sys.stdout
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe)
    stream.flush()


def process_group_options() -> dict:
    """Start a child in a group that can be stopped without matching by command text."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(proc: subprocess.Popen, grace_seconds: float = 8.0) -> bool:
    """Best-effort, PID-scoped tree termination; return whether the child exited."""
    if proc.poll() is not None:
        return True
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=grace_seconds)
            return True
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                proc.wait(timeout=3)
                return True
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                    return True
                except (OSError, subprocess.TimeoutExpired):
                    return False
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return proc.poll() is not None
        try:
            proc.wait(timeout=grace_seconds)
            return True
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
                return True
            except subprocess.TimeoutExpired:
                return False
