#!/usr/bin/env python3
"""Decode each declared raw EFM capture with Orc/EFMSink, then verify provenance.

This stage does not rank/select captures. It preserves each native decode and
records source/output provenance; the workflow app performs the existing
unique-Pareto selection or opens its manual comparison UI afterward.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from dependency_lock import LockError  # noqa: E402
from efm_decode import (build_decode_project, extract_7z_member,
                       project_formats, sevenzip_efm_member)  # noqa: E402
from efm_paths import decode_artifact_paths, require_new_outputs  # noqa: E402
from fileutil import sha256_file  # noqa: E402
from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)  # noqa: E402
from shared_workspace import validate_device_config_profile, validate_instance  # noqa: E402
from streamutil import start_line_reader  # noqa: E402
from verify_efm_capture import verify_capture  # noqa: E402


class EfmDecodeError(RuntimeError):
    pass


def _safe_download_file(downloads: Path, name: str, role: str) -> Path:
    relative = Path(str(name))
    if (not name or relative.is_absolute() or len(relative.parts) != 1 or
            any(part in ("", ".", "..") for part in relative.parts)):
        raise EfmDecodeError(f"manifest {role} must be one safe filename: {name!r}")
    raw = downloads / relative
    if raw.is_symlink():
        raise EfmDecodeError(f"manifest {role} may not be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
        resolved.relative_to(downloads.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise EfmDecodeError(f"manifest {role} is missing or escaped downloads/: {raw}") from exc
    if not resolved.is_file():
        raise EfmDecodeError(f"manifest {role} is not a regular file: {resolved}")
    return resolved


def _check_declared_size(path: Path, capture: dict, key: str) -> None:
    expected = capture.get(key)
    if type(expected) is not int or expected <= 0 or path.stat().st_size != expected:
        raise EfmDecodeError(
            f"{path.name} size differs from verified manifest {key}: "
            f"actual={path.stat().st_size} expected={expected}")


def _check_new_file(path: Path, ld: Ld) -> None:
    assert_writable_path(path, ld)
    if path.exists() or path.is_symlink():
        raise EfmDecodeError(f"refusing to overwrite existing EFM decode artifact: {path}")


def _check_audio_wav(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 44:
        raise EfmDecodeError(f"EFMSink WAV is missing, unsafe, or empty: {path}")
    try:
        with wave.open(str(path), "rb") as source:
            info = {
                "frames": source.getnframes(),
                "channels": source.getnchannels(),
                "sample_rate": source.getframerate(),
                "sample_width_bytes": source.getsampwidth(),
                "compression": source.getcomptype(),
            }
    except (OSError, wave.Error) as exc:
        raise EfmDecodeError(f"EFMSink output is not a readable PCM WAV: {path}: {exc}") from exc
    if (info["frames"] <= 0 or info["channels"] != 2 or info["sample_rate"] != 44100 or
            info["sample_width_bytes"] != 2 or info["compression"] != "NONE"):
        raise EfmDecodeError(f"EFMSink output is not native 44.1 kHz/16-bit/stereo PCM: {info}")
    return info


def _orc_executable(ld: Ld) -> Path:
    raw = ld.tool("orc")
    path = Path(raw).expanduser().resolve(strict=True)
    if not path.is_file():
        raise EfmDecodeError(f"configured Orc executable is not a file: {path}")
    return path


def _run_orc(command: list[str], capture: str) -> int:
    print(f"Starting Orc EFMSink audio decode for capture {capture}.", flush=True)
    try:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", bufsize=1)
    except OSError as exc:
        raise EfmDecodeError(f"could not start Orc for capture {capture}: {exc}") from exc
    assert process.stdout is not None
    lines, reader = start_line_reader(process.stdout, name=f"orc-efm-{capture}")
    last_output = "(no output yet)"
    try:
        while True:
            try:
                line = lines.get(timeout=30)
            except queue.Empty:
                print(f"[heartbeat] EFMSink capture {capture} is still running; "
                      f"last output: {last_output}", flush=True)
                continue
            if line is None:
                break
            last_output = line.strip()[-240:] or "(blank line)"
            print(line, end="", flush=True)
        returncode = process.wait()
        reader.join()
        if returncode:
            raise EfmDecodeError(
                f"Orc EFMSink failed for capture {capture} with exit code {returncode}")
        return returncode
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=10)
        raise


def _decode_source(ld: Ld, capture: dict, downloads: Path, work_root: Path,
                   retained_output: Path | None):
    """Resolve the usable .efm and preserve an unpacked archive member as an output."""
    efm_name = str(capture.get("efm_file") or "")
    source = _safe_download_file(downloads, efm_name, "EFM source")
    _check_declared_size(source, capture, "efm_bytes")
    suffix = source.name.casefold()
    if suffix.endswith(".efm"):
        return source, sha256_file(source), None
    if not suffix.endswith(".efm.7z"):
        raise EfmDecodeError(f"unsupported raw EFM filename: {source.name}")

    sevenzip_raw = (ld.profile.get("tools") or {}).get("sevenzip")
    if not sevenzip_raw:
        raise EfmDecodeError("tools.sevenzip is required to decode a .efm.7z source")
    sevenzip = Path(os.path.expandvars(os.path.expanduser(str(sevenzip_raw)))).resolve(strict=True)
    if not sevenzip.is_file():
        raise EfmDecodeError(f"configured tools.sevenzip is not a file: {sevenzip}")
    listed = subprocess.run([str(sevenzip), "l", "-slt", str(source)],
                            capture_output=True, check=False)
    if listed.returncode:
        detail = listed.stderr.decode("utf-8", "replace")[-800:]
        raise EfmDecodeError(f"7-Zip could not list the EFM archive: {detail}")
    expected_member_name = source.name[:-3]  # remove only ".7z", retaining ".efm"
    try:
        member, expected_size = sevenzip_efm_member(
            listed.stdout.decode("utf-8", "replace"), expected_member_name)
    except ValueError as exc:
        raise EfmDecodeError(f"unsafe or ambiguous EFM archive: {exc}") from exc
    work_root.mkdir(parents=True, exist_ok=True)
    reserve_gib = float((ld.profile.get("limits") or {}).get("min_free_gb", 80) or 0)
    required = expected_size + int(reserve_gib * 1024 ** 3)
    free = shutil.disk_usage(work_root).free
    if free < required:
        raise EfmDecodeError(
            f"not enough work space to safely unpack {source.name}: "
            f"need {expected_size / (1024**3):.2f} GiB plus {reserve_gib:.1f} GiB reserve; "
            f"free={free / (1024**3):.2f} GiB")
    if retained_output is None:
        raise EfmDecodeError("compressed EFM decode plan has no preserved raw-EFM output path")
    retained_output.parent.mkdir(parents=True, exist_ok=True)
    try:
        retained_output.resolve(strict=False).relative_to(work_root.resolve(strict=True))
    except ValueError as exc:
        raise EfmDecodeError("unpacked EFM output must remain below configured roots.work") from exc
    if retained_output.exists() or retained_output.is_symlink():
        raise EfmDecodeError(f"refusing to overwrite preserved raw EFM output: {retained_output}")
    assert_writable_path(retained_output, ld)
    extract_7z_member(sevenzip, source, member, expected_size, retained_output)
    return retained_output.resolve(strict=True), sha256_file(retained_output), None


def decode_captures(episode: str, series: str, captures: list[str], run_token: str,
                    expected_manifest_sha256: str, profile_path: Path | None = None) -> list[Path]:
    episode, series = episode.upper(), series.upper()
    identity = f"{series}:{episode}"
    if not re.fullmatch(r"S\d{2}E\d{2}", episode):
        raise EfmDecodeError(f"invalid episode code: {episode!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", run_token):
        raise EfmDecodeError("run token must contain 8..64 safe ASCII characters")
    if not captures or len(captures) != len(set(captures)):
        raise EfmDecodeError("provide one or more unique capture labels")

    os.environ["LDP_SERIES"] = series
    os.environ["LDP_EPISODE"] = episode
    os.environ["LDP_IDENTITY"] = identity
    if profile_path:
        os.environ["LDP_PROFILE"] = str(profile_path.expanduser().resolve(strict=True))
    ld = Ld(profile_path)
    if (ld.profile.get("shared") or {}).get("enabled") is not True:
        raise EfmDecodeError("automatic EFM decoding requires the verified device workspace")
    instance = Path(os.environ.get("LDP_INSTANCE_ROOT", "")).expanduser().resolve(strict=True)
    validate_device_config_profile(ld.profile_path, ld.profile, ROOT)
    validate_instance(str(instance), ld.profile_path, ld.profile, identity)
    episode_obj = load_episode(identity, ld, series=series)
    manifest_path = episode_manifest_path(episode, ld, series=series).resolve(strict=True)
    original_manifest = manifest_path.read_bytes()
    if hashlib.sha256(original_manifest).hexdigest() != expected_manifest_sha256.lower():
        raise EfmDecodeError("episode manifest changed after the EFM decode plan was created")
    manifest = episode_obj.data
    rows = {str(row.get("label", "")): row for row in manifest.get("captures", [])}
    if len(rows) != len(manifest.get("captures") or []):
        raise EfmDecodeError("capture labels are missing or duplicated in the episode manifest")
    unknown = sorted(set(captures) - set(rows))
    if unknown:
        raise EfmDecodeError("decode plan names captures absent from the manifest: " + ", ".join(unknown))
    audio_root = ld.root("audio").resolve(strict=True)
    downloads = episode_obj.downloads().resolve(strict=True)
    work_root = ld.root("work").resolve(strict=False)
    project_template = episode_obj.path("stack_project").resolve(strict=True)
    video_format, source_format, amplitude_unit = project_formats(project_template)
    if source_format != "Composite":
        raise EfmDecodeError(
            "automatic EFM decoding currently requires composite TBC sources; "
            "the selected stack project declares YC input")
    orc = _orc_executable(ld)

    prepared = []
    for label in captures:
        capture = rows[label]
        if not capture.get("efm_file"):
            raise EfmDecodeError(f"capture {label} has no raw EFM source in the manifest")
        tbc = _safe_download_file(downloads, str(capture.get("tbc_file") or ""), "TBC source")
        metadata = _safe_download_file(downloads, str(capture.get("json_file") or ""), "TBC metadata")
        _check_declared_size(tbc, capture, "tbc_bytes")
        _check_declared_size(metadata, capture, "json_bytes")
        artifacts = decode_artifact_paths(audio_root, instance, episode, label, run_token)
        require_new_outputs(artifacts["wav"], artifacts["report"])
        for key in ("wav", "report"):
            assert_writable_path(artifacts[key], ld)
        for key in ("orc_log", "orc_project", "provenance"):
            _check_new_file(artifacts[key], ld)
        retained_efm = None
        if str(capture.get("efm_file", "")).casefold().endswith(".efm.7z"):
            retained_efm = work_root / "efm-decodes" / episode / f"{label}-{run_token}.efm"
            _check_new_file(retained_efm, ld)
        prepared.append((label, capture, tbc, artifacts, retained_efm))

    verified_paths: list[Path] = []
    expected_current_manifest_sha256 = expected_manifest_sha256.lower()
    for label, capture, tbc, artifacts, retained_efm in prepared:
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_current_manifest_sha256:
            raise EfmDecodeError(
                "episode manifest changed outside this decode stage; stopping before the next capture")
        efm_input, efm_input_sha256, _temporary = _decode_source(
            ld, capture, downloads, work_root, retained_efm)
        project_text = build_decode_project(
            episode=episode, capture=label, tbc=tbc, efm=efm_input,
            wav=artifacts["wav"], video_format=video_format,
            source_format=source_format, amplitude_unit=amplitude_unit)
        artifacts["wav"].parent.mkdir(parents=True, exist_ok=True)
        artifacts["orc_log"].parent.mkdir(parents=True, exist_ok=True)
        artifacts["orc_project"].parent.mkdir(parents=True, exist_ok=True)
        with artifacts["orc_project"].open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(project_text)
        command = [str(orc), str(artifacts["orc_project"]), "--process",
                   "--log-file", str(artifacts["orc_log"]), "--log-out", "both"]
        _run_orc(command, label)
        wav_info = _check_audio_wav(artifacts["wav"])
        if (artifacts["report"].is_symlink() or not artifacts["report"].is_file() or
                artifacts["report"].stat().st_size <= 0):
            raise EfmDecodeError(f"EFMSink decode report is missing or empty: {artifacts['report']}")
        if artifacts["orc_log"].is_symlink() or not artifacts["orc_log"].is_file():
            raise EfmDecodeError(f"Orc decode log is missing or unsafe: {artifacts['orc_log']}")
        decoded_efm_sha256 = sha256_file(efm_input)
        if decoded_efm_sha256 != efm_input_sha256:
            raise EfmDecodeError(f"raw EFM source changed during decode for capture {label}")
        actual_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if actual_manifest_sha256 != expected_current_manifest_sha256:
            raise EfmDecodeError("episode manifest changed while Orc was decoding; output is preserved but unverified")
        verify_capture(
            episode, series, label, artifacts["orc_log"], artifacts["wav"],
            artifacts["report"], artifacts["provenance"], actual_manifest_sha256,
            ld.profile_path, decoded_efm_path=efm_input,
            decoded_efm_sha256=decoded_efm_sha256,
            decoded_efm_bytes=efm_input.stat().st_size,
            orc_project_path=artifacts["orc_project"])
        expected_current_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        verified_paths.extend(artifacts.values())
        if retained_efm:
            verified_paths.append(retained_efm)
        print(f"EFM capture {label} verified: {wav_info['frames']} PCM frames; "
              f"source SHA-256 {decoded_efm_sha256}", flush=True)
    return verified_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"))
    parser.add_argument("--capture", action="append", required=True,
                        help="manifest capture label; repeat for each available EFM")
    parser.add_argument("--run-token", required=True)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    try:
        decode_captures(args.episode, args.series or os.environ.get("LDP_SERIES", "DS9"),
                        args.capture, args.run_token, args.confirm_manifest_sha256,
                        Path(args.profile) if args.profile else None)
        return 0
    except (EfmDecodeError, PipelineError, LockError, OSError, ValueError,
            RuntimeError, subprocess.SubprocessError) as exc:
        print(f"EFM DECODE ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
