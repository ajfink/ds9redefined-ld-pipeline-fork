#!/usr/bin/env python3
"""Generate the full-side TFM decision log used by the cadence-analysis stage.

This is a deterministic measurement step, not a cadence approval. It trims only
the measured capture-alignment PAD count, uses the established TFM parameters,
and verifies that one decision was logged per expected input frame. Human review
of hybrid/native-30p flags and measured head/tail trims remains separate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, assert_writable_path, load_episode, redact_shared_text  # noqa: E402
from vspipe_util import vspipe_bin, vspipe_env  # noqa: E402

TFM_RE = re.compile(r"^(\d+)\s+[a-z]\s+(?:[-+]\s+)?\[(\d+)\]", re.IGNORECASE)


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def count_tfm_decisions(text: str) -> tuple[int, int | None, int | None]:
    frames = []
    for line in text.splitlines():
        match = TFM_RE.match(line.strip())
        if match:
            frames.append(int(match.group(1)))
    if not frames:
        return 0, None, None
    return len(frames), frames[0], frames[-1]


def tfm_frame_indices(text: str) -> list[int]:
    frames = []
    for line in text.splitlines():
        match = TFM_RE.match(line.strip())
        if match:
            frames.append(int(match.group(1)))
    return frames


def _quote_avs(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_avs_script(source: Path, pad_frames: int, log_path: Path,
                     loader: Path | None = None) -> str:
    lines = []
    if loader:
        lines.append(f'Import("{_quote_avs(loader)}")')
    lines.extend((
        f'src = LSMASHVideoSource("{_quote_avs(source)}").Trim({pad_frames}, 0)',
        f'fm = src.TFM(order=-1, pp=0, chroma=true, slow=2, output="{_quote_avs(log_path)}")',
        "return fm",
    ))
    return "\r\n".join(lines) + "\r\n"


def write_avs_script(path: Path, source: Path, pad_frames: int,
                     log_path: Path, loader: Path | None = None) -> None:
    # AviSynth accepts UTF-8 without a BOM; FFmpeg's AviSynth demuxer rejects
    # UTF-8-BOM scripts before evaluation.
    path.write_text(build_avs_script(source, pad_frames, log_path, loader),
                    encoding="utf-8", newline="")


def build_vpy_script(source: Path, pad_frames: int, log_path: Path) -> str:
    return f'''import vapoursynth as vs

core = vs.core
clip = core.bs.VideoSource({str(source)!r})
clip = core.std.Trim(clip, {pad_frames}, None)
clip = core.std.SetFrameProps(clip, _FieldBased=2)
clip = core.tivtc.TFM(clip, order=-1, PP=0, chroma=1, slow=2,
                      output={str(log_path)!r})
clip.set_output()
'''


def _stream_process(command: list[str], env: dict[str, str] | None = None,
                    *, stdin=None, stderr=None) -> tuple[int, str]:
    try:
        proc = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                                stderr=stderr if stderr is not None else subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
    except OSError as exc:
        raise PipelineError(f"could not start TFM decode command {command[0]}: {exc}") from exc
    assert proc.stdout is not None
    output = []
    try:
        for line in proc.stdout:
            output.append(line)
            print(line, end="", flush=True)
        proc.stdout.close()
        return proc.wait(), "".join(output)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise


def _run_windows(ld: Ld, source: Path, pad: int, temp_root: Path,
                 temporary_log: Path) -> tuple[int, str]:
    script = temp_root / "tfm-probe.avs"
    loader_value = (ld.profile.get("tools") or {}).get("avisynth_loader")
    loader = Path(os.path.expandvars(os.path.expanduser(str(loader_value)))) if loader_value else None
    if loader and not loader.is_file():
        raise PipelineError(f"configured AviSynth loader is missing: {loader}")
    write_avs_script(script, source, pad, temporary_log, loader)
    command = [ld.ffmpeg(), "-hide_banner", "-nostats", "-v", "error", "-progress", "pipe:1",
               "-f", "avisynth", "-i", str(script), "-an", "-f", "null", "-"]
    return _stream_process(command)


def _run_macos(ld: Ld, source: Path, pad: int, temp_root: Path,
               temporary_log: Path, diagnostic_log: Path) -> tuple[int, str]:
    script = temp_root / "tfm-probe.vpy"
    script.write_text(build_vpy_script(source, pad, temporary_log), encoding="utf-8")
    vs_command = [vspipe_bin(), "-c", "y4m", str(script), "-"]
    ff_command = [ld.ffmpeg(), "-hide_banner", "-nostats", "-v", "error", "-progress", "pipe:1",
                  "-f", "yuv4mpegpipe", "-i", "-", "-an", "-f", "null", "-"]
    diagnostic_log.parent.mkdir(parents=True, exist_ok=True)
    with diagnostic_log.open("xb") as vspipe_stderr:
        vs = subprocess.Popen(vs_command, stdout=subprocess.PIPE, stderr=vspipe_stderr,
                              env=vspipe_env())
        assert vs.stdout is not None
        try:
            ff_exit, text = _stream_process(ff_command, stdin=vs.stdout)
        except Exception:
            vs.terminate()
            try:
                vs.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vs.kill()
                vs.wait()
            raise
        vs.stdout.close()
        vs_exit = vs.wait()
    if vs_exit != 0:
        tail = diagnostic_log.read_text(encoding="utf-8", errors="replace")[-1200:]
        raise PipelineError(f"vspipe exited {vs_exit}; see {diagnostic_log}: {tail}")
    return ff_exit, text


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--out", help="override the manifest TFM-log output")
    args = parser.parse_args()
    ld = Ld()
    ep = load_episode(args.episode, ld)
    source = ep.path("stack_master")
    if not source.is_file():
        raise PipelineError(f"stack master is missing: {source}")
    pad = ep.pad_trim
    expected_frames = ep.stack_frames - pad
    if expected_frames <= 0:
        raise PipelineError(f"measured capture PAD {pad} leaves no TFM frames from stack.frames={ep.stack_frames}")
    out = Path(args.out).expanduser().resolve() if args.out else ep.path("tfm_log")
    assert_writable_path(out, ld)
    if out.exists():
        raise PipelineError(f"refusing to overwrite TFM log: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary_log = out.with_name(f".{out.name}.{token}.partial")
    if temporary_log.exists():
        raise PipelineError(f"temporary TFM log already exists: {temporary_log}")
    profile_os = str((ld.profile.get("os") or "windows")).casefold()
    started = datetime.now(timezone.utc).isoformat()
    print(f"episode={ep.code} source={source} measured_pad_frames={pad} expected_tfm_decisions={expected_frames}", flush=True)
    with tempfile.TemporaryDirectory(prefix=f"ld-tfm-{ep.code}-") as tmp:
        temp_root = Path(tmp)
        if profile_os in ("windows", "win32"):
            exit_code, _ = _run_windows(ld, source, pad, temp_root, temporary_log)
        elif profile_os in ("darwin", "macos"):
            diagnostic_log = ld.root("logs") / f"{ep.code}-tfm-vspipe-{token}.log"
            exit_code, _ = _run_macos(ld, source, pad, temp_root, temporary_log, diagnostic_log)
            print(f"vspipe diagnostic log: {diagnostic_log}", flush=True)
        else:
            raise PipelineError(f"TFM log generation is not enabled for profile os={profile_os!r}")

    if not temporary_log.is_file():
        raise PipelineError(f"TFM did not create its decision log: {temporary_log} (ffmpeg exit {exit_code})")
    text = temporary_log.read_text(encoding="utf-8", errors="replace")
    frame_indices = tfm_frame_indices(text)
    count = len(frame_indices)
    first = frame_indices[0] if frame_indices else None
    last = frame_indices[-1] if frame_indices else None
    if exit_code != 0:
        raise PipelineError(f"full TFM decode exited {exit_code}; partial log retained at {temporary_log}")
    consecutive = (not frame_indices or
                   all(current == previous + 1 for previous, current in zip(frame_indices, frame_indices[1:])))
    if count != expected_frames or not consecutive or (first is not None and last - first + 1 != expected_frames):
        raise PipelineError(f"TFM log has {count} decisions spanning {first}..{last}; expected {expected_frames}; partial log retained at {temporary_log}")
    os.replace(temporary_log, out)
    report = {
        "schema": "ld-pipeline-tfm-log-v1",
        "workflow_version": (Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION").read_text(encoding="ascii").strip(),
        "episode": ep.code,
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "platform": profile_os,
        "source": str(source),
        "source_size": source.stat().st_size,
        "source_mtime_ns": source.stat().st_mtime_ns,
        "source_pad_frames": pad,
        "expected_decisions": expected_frames,
        "decision_count": count,
        "first_frame": first,
        "last_frame": last,
        "tfm_log": str(out),
    }
    report_path = ld.root("logs") / f"{ep.code}-tfm-log-{token}.json"
    assert_writable_path(report_path, ld)
    report_text = json.dumps(report, indent=2) + "\n"
    report_text = redact_shared_text(report_text, ld)
    with report_path.open("x", encoding="utf-8") as stream:
        stream.write(report_text)
    print(f"TFM LOG PASS: decisions={count} frames={first}..{last}; log={out}; report={report_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"TFM LOG ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
