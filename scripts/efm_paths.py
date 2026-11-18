#!/usr/bin/env python3
"""Plan collision-resistant, episode-scoped paths for native EFMSink WAVs/reports.

This helper does not decode, create directories, or overwrite files. Use ``--check``
immediately before an Orc decode to refuse an existing WAV or report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

EPISODE_RE = re.compile(r"^S\d{2}E\d{2}$", re.IGNORECASE)
CAPTURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
RUN_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


class EfmPathError(ValueError):
    pass


def efm_output_paths(work_root: str | Path, episode: str, capture: str) -> tuple[Path, Path]:
    if not EPISODE_RE.fullmatch(episode):
        raise EfmPathError(f"episode must be SxxExx: {episode!r}")
    if not CAPTURE_RE.fullmatch(capture) or capture in (".", ".."):
        raise EfmPathError(f"unsafe capture label: {capture!r}")
    directory = Path(os.path.expandvars(os.path.expanduser(str(work_root)))).resolve() / "audio" / episode.upper()
    wav = directory / f"{capture}-efm.wav"
    return wav, Path(str(wav) + ".txt")


def require_new_outputs(wav: Path, report: Path) -> None:
    existing = [str(path) for path in (wav, report) if path.exists()]
    if existing:
        raise EfmPathError("refusing to overwrite existing EFM output/report: " + ", ".join(existing))


def decode_artifact_paths(audio_root: str | Path, instance_root: str | Path,
                          episode: str, capture: str, run_token: str) -> dict[str, Path]:
    """Return fixed audio plus unique, episode-workspace Orc/provenance artifact paths."""
    if not EPISODE_RE.fullmatch(episode):
        raise EfmPathError(f"episode must be SxxExx: {episode!r}")
    if not CAPTURE_RE.fullmatch(capture) or capture in (".", ".."):
        raise EfmPathError(f"unsafe capture label: {capture!r}")
    if not RUN_TOKEN_RE.fullmatch(run_token):
        raise EfmPathError("run token must be 8..64 safe ASCII characters")
    audio_dir = Path(audio_root).expanduser().resolve(strict=False) / episode.upper()
    reference_dir = Path(instance_root).expanduser().resolve(strict=False) / "references"
    log_dir = Path(instance_root).expanduser().resolve(strict=False) / "logs"
    prefix = f"{episode.upper()}-{capture}-{run_token}"
    wav = audio_dir / f"{capture}-efm.wav"
    return {
        "wav": wav,
        "report": Path(str(wav) + ".txt"),
        "orc_log": log_dir / f"efm-decode-{prefix}.log",
        "orc_project": reference_dir / f"efm-decode-{prefix}.orcprj",
        "provenance": reference_dir / f"efm-provenance-{prefix}.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--check", action="store_true", help="also require the WAV and report paths to be unused")
    args = parser.parse_args()
    try:
        wav, report = efm_output_paths(args.work_root, args.episode, args.capture)
        if args.check:
            require_new_outputs(wav, report)
        print(json.dumps({"episode": args.episode.upper(), "capture": args.capture,
                          "output_wav": str(wav), "report": str(report),
                          "outputs_unused": args.check}, indent=2))
        return 0
    except (EfmPathError, OSError) as exc:
        print(f"EFM PATH ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
