#!/usr/bin/env python3
"""Verify a TFM decision log using its FFmpeg session log, not the TFM log itself.

The TFM decision log contains match decisions and does not report FFmpeg's
``frame=`` progress. This check binds that file to the session log that owns the
decoder exit status and final frame count.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ldcommon import (Ld, PipelineError, assert_writable_path, load_episode,
                      redact_shared_text)  # noqa: E402

FRAME_RE = re.compile(r"\bframe\s*=\s*(\d+)", re.IGNORECASE)
EXIT_RE = re.compile(
    r"\b(?:ffmpeg\s+)?(?:exit(?:\s+code)?|returncode)\s*[:=]\s*(-?\d+)\b",
    re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_session_log(text: str) -> tuple[int | None, int | None]:
    frames = [int(value) for value in FRAME_RE.findall(text)]
    exits = [int(value) for value in EXIT_RE.findall(text)]
    return (frames[-1] if frames else None, exits[-1] if exits else None)


def verify_tfm_run(decision_log: Path, session_log: Path,
                   expected_frames: int) -> dict:
    result = {
        "schema": "ld-pipeline-tfm-run-verification-v1",
        "status": "FAIL",
        "expected_frames": expected_frames,
        "decision_log": str(decision_log),
        "session_log": str(session_log),
        "decoded_frames": None,
        "ffmpeg_exit_code": None,
        "decision_log_bytes": None,
        "decision_log_sha256": None,
        "session_log_sha256": None,
        "failures": [],
    }
    if not decision_log.is_file():
        result["failures"].append(f"TFM decision log is missing: {decision_log}")
    else:
        stat = decision_log.stat()
        result["decision_log_bytes"] = stat.st_size
        result["decision_log_sha256"] = sha256_file(decision_log)
        if stat.st_size <= 0:
            result["failures"].append("TFM decision log is empty")
    if not session_log.is_file():
        result["failures"].append(f"FFmpeg session log is missing: {session_log}")
        return result
    text = session_log.read_text(encoding="utf-8-sig", errors="replace")
    result["session_log_sha256"] = sha256_file(session_log)
    frames, exit_code = parse_session_log(text)
    result["decoded_frames"] = frames
    result["ffmpeg_exit_code"] = exit_code
    if exit_code is None:
        result["failures"].append("session log has no explicit FFmpeg exit-code record")
    elif exit_code != 0:
        result["failures"].append(f"FFmpeg exited with {exit_code}")
    if frames is None:
        result["failures"].append("session log has no FFmpeg frame= progress record")
    elif frames != expected_frames:
        result["failures"].append(
            f"FFmpeg decoded {frames} frames; expected {expected_frames}")
    if not result["failures"]:
        result["status"] = "PASS"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--session-log", required=True,
                        help="the FFmpeg session log containing frame= stats and exit status")
    parser.add_argument("--report-out", help="optional new JSON report; refuses overwrite")
    args = parser.parse_args()
    try:
        ld = Ld()
        ep = load_episode(args.episode, ld)
        expected = int(ep.data.get("stack", {}).get("frames") or 0)
        if expected <= 0:
            raise PipelineError(f"{ep.identity}: stack.frames must be measured before TFM verification")
        decision_log = ep.path("tfm_log")
        session_log = Path(args.session_log).expanduser().resolve()
        report = verify_tfm_run(decision_log, session_log, expected)
        if (ld.profile.get("shared") or {}).get("enabled") is True:
            report = json.loads(redact_shared_text(json.dumps(report), ld))
        if args.report_out:
            out = Path(args.report_out).expanduser()
            if out.exists() or out.is_symlink():
                raise PipelineError(f"refusing to overwrite TFM verification report: {out}")
            assert_writable_path(out, ld)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if report["status"] != "PASS":
            print("TFM RUN VERIFICATION FAIL: do not use this decision log for cadence.",
                  file=sys.stderr)
            return 1
        print(f"TFM RUN VERIFIED: {ep.identity} {expected} frames; exit 0.")
        return 0
    except (OSError, ValueError, PipelineError) as exc:
        print(f"TFM RUN VERIFICATION ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
