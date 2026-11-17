#!/usr/bin/env python3
"""Pure process-status helpers for the desktop workflow monitor."""
from __future__ import annotations

STALL_WARNING_SECONDS = 20 * 60


def process_status_text(action: str, pid: int | None, elapsed_s: float,
                        idle_s: float, alive: bool) -> tuple[str, bool]:
    state = "running" if alive else "finishing"
    pid_text = str(pid) if pid is not None else "starting"
    message = (f"Workflow {action}: {state}; PID {pid_text}; elapsed {elapsed_s / 60:.1f} min; "
               f"no console output for {idle_s / 60:.1f} min.")
    stalled = alive and idle_s >= STALL_WARNING_SECONDS
    if stalled:
        message += " Inspect process/log/output evidence; no automatic kill or retry."
    return message, stalled
