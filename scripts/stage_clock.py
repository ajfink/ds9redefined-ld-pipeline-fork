#!/usr/bin/env python3
"""Measure active time for a manually executed pipeline stage.

``run_stage.py`` records timing automatically. For manual stages, use ``start``;
pause before waiting for a human gate decision, resume after the answer, and stop
when stage work is complete. Paused human wait is reported separately and excluded
from active time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dependency_lock import LockError  # noqa: E402
from shared_workspace import resolve_instance_workspace  # noqa: E402


class ClockError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_clock(episode: str, stage: str, machine: str | None, workspace_root: str,
              workspace_mode: str, now_ns: int | None = None,
              now_utc: str | None = None) -> dict:
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    now_utc = utc_now() if now_utc is None else now_utc
    return {
        "schema": "ld-pipeline-stage-clock-v1",
        "workflow_version": (ROOT / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": episode,
        "stage": stage,
        "machine": machine,
        "workspace_root": workspace_root,
        "workspace_mode": workspace_mode,
        "state": "running",
        "started_utc": now_utc,
        "started_monotonic_ns": now_ns,
        "active_since_monotonic_ns": now_ns,
        "paused_since_monotonic_ns": None,
        "active_elapsed_ns": 0,
        "human_wait_elapsed_ns": 0,
        "events": [{"event": "start", "utc": now_utc}],
    }


def transition(clock: dict, action: str, now_ns: int | None = None,
               now_utc: str | None = None, result: str | None = None) -> dict:
    """Pause/resume/stop an active timer, accumulating only active intervals."""
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    now_utc = utc_now() if now_utc is None else now_utc
    if clock.get("schema") != "ld-pipeline-stage-clock-v1":
        raise ClockError("not an ld-pipeline stage-clock record")
    state = clock.get("state")
    if action == "pause":
        if state != "running":
            raise ClockError(f"pause requires state=running; current state is {state!r}")
        clock["active_elapsed_ns"] += max(0, now_ns - int(clock["active_since_monotonic_ns"]))
        clock["active_since_monotonic_ns"] = None
        clock["paused_since_monotonic_ns"] = now_ns
        clock["state"] = "paused"
    elif action == "resume":
        if state != "paused":
            raise ClockError(f"resume requires state=paused; current state is {state!r}")
        clock["human_wait_elapsed_ns"] += max(0, now_ns - int(clock["paused_since_monotonic_ns"]))
        clock["paused_since_monotonic_ns"] = None
        clock["active_since_monotonic_ns"] = now_ns
        clock["state"] = "running"
    elif action == "stop":
        if state == "running":
            clock["active_elapsed_ns"] += max(0, now_ns - int(clock["active_since_monotonic_ns"]))
            clock["active_since_monotonic_ns"] = None
        elif state == "paused":
            clock["human_wait_elapsed_ns"] += max(0, now_ns - int(clock["paused_since_monotonic_ns"]))
            clock["paused_since_monotonic_ns"] = None
        else:
            raise ClockError(f"stop requires state=running or paused; current state is {state!r}")
        clock["state"] = "complete"
        clock["finished_utc"] = now_utc
        clock["wall_elapsed_ns"] = max(0, now_ns - int(clock["started_monotonic_ns"]))
        clock["result"] = result or "UNRECORDED"
    else:
        raise ClockError(f"unknown clock transition: {action}")
    clock.setdefault("events", []).append({"event": action, "utc": now_utc})
    clock["active_elapsed_seconds"] = round(clock["active_elapsed_ns"] / 1e9, 3)
    clock["human_wait_seconds"] = round(clock["human_wait_elapsed_ns"] / 1e9, 3)
    if clock.get("wall_elapsed_ns") is not None:
        clock["wall_elapsed_seconds"] = round(clock["wall_elapsed_ns"] / 1e9, 3)
    return clock


def save_update(path: Path, clock: dict) -> None:
    temp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as f:
            json.dump(clock, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def clock_file(path_value: str) -> tuple[Path, dict]:
    path = Path(os.path.expandvars(os.path.expanduser(path_value))).resolve()
    if path.parent.name != "logs" or not (path.parent.parent / "workspace.json").is_file():
        raise ClockError("clock file must be directly under an initialized episode workspace logs/ folder")
    try:
        clock = json.loads(path.read_text(encoding="utf-8"))
        workspace = json.loads((path.parent.parent / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClockError(f"cannot read timer/workspace record: {exc}") from exc
    if clock.get("schema") != "ld-pipeline-stage-clock-v1":
        raise ClockError(f"not an ld-pipeline stage-clock file: {path}")
    if (clock.get("episode") != workspace.get("episode") or
            clock.get("machine") != workspace.get("machine")):
        raise ClockError("stage clock does not match its workspace metadata")
    return path, clock


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="begin timing a manual stage")
    start.add_argument("--profile", required=True)
    start.add_argument("--episode", required=True)
    start.add_argument("--stage", required=True)
    start.add_argument("--instance-root", help="resume/use a specific device-instance workspace")
    for name in ("pause", "resume", "stop"):
        action = sub.add_parser(name, help=f"{name} an active timer; pauses exclude human wait")
        action.add_argument("--clock-file", required=True)
        if name == "stop":
            action.add_argument("--result", choices=("PASS", "FAIL", "BLOCKED", "STOP"),
                                default="UNRECORDED", help="record the manual stage result")
    args = ap.parse_args()
    try:
        if args.command == "start":
            from shared_workspace import load_profile
            profile_path, profile = load_profile(args.profile)
            episode = args.episode.upper()
            if not re.fullmatch(r"S\d{2}E\d{2}", episode, re.IGNORECASE):
                raise ClockError(f"episode must be SxxExx: {args.episode!r}")
            workspace, warning = resolve_instance_workspace(
                profile_path, profile, episode, args.instance_root, create=True)
            if not workspace:
                raise ClockError(warning or "could not resolve an episode workspace")
            root = Path(workspace["instance_root"])
            label = re.sub(r"[^A-Za-z0-9._-]+", "-", args.stage).strip(".-_")[:64]
            if not label:
                raise ClockError("stage label must contain a letter or digit")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = root / "logs" / f"stage-clock-{episode}-{label}-{stamp}-{secrets.token_hex(4)}.json"
            clock = new_clock(episode, label, profile.get("machine"), str(root.resolve()),
                              workspace["storage_mode"])
            with path.open("x", encoding="utf-8", newline="\n") as f:
                json.dump(clock, f, indent=2)
                f.write("\n")
            if warning:
                print("WARN: " + warning, file=sys.stderr)
            print(json.dumps({"clock_file": str(path.resolve()), "episode": episode,
                              "stage": label, "state": "running",
                              "workspace_mode": workspace["storage_mode"]}, indent=2))
            return 0

        path, clock = clock_file(args.clock_file)
        transition(clock, args.command, result=getattr(args, "result", None))
        save_update(path, clock)
        print(json.dumps({"clock_file": str(path), "episode": clock["episode"],
                          "stage": clock["stage"], "state": clock["state"],
                          "active_elapsed_seconds": clock["active_elapsed_seconds"],
                          "human_wait_seconds": clock["human_wait_seconds"],
                          "wall_elapsed_seconds": clock.get("wall_elapsed_seconds"),
                          "result": clock.get("result")}, indent=2))
        return 0
    except (LockError, ClockError, OSError, json.JSONDecodeError) as exc:
        print(f"STAGE CLOCK ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
