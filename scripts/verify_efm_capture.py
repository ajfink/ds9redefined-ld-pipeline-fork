#!/usr/bin/env python3
"""Verify one EFMSink log/WAV/report belongs to the declared episode capture.

This is a provenance check, not a capture-quality ranker or capture selector. Run it
for each capture whose EFM decode will be considered; record the explicit selection
separately. It rejects stale/cross-episode logs before they can support an audio gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from ldcommon import (Ld, PipelineError, assert_writable_path, episode_manifest_path,
                      load_episode)
from dependency_lock import LockError
from shared_workspace import validate_device_config_profile, validate_instance
from local_source import fingerprint_file

SCHEMA = "ld-pipeline-efm-capture-provenance-v1"
TBC_JSON_RE = re.compile(r"using legacy JSON metadata:\s*(.+?\.tbc\.json)\s*$", re.IGNORECASE)
TBC_LOADED_RE = re.compile(r"tbc_source:\s*Loaded\s+'(.+?\.tbc)'", re.IGNORECASE)
EFM_OUTPUT_RE = re.compile(r"EFMSink:\s*mode=audio,\s*output=(.+?)\s*$", re.IGNORECASE)
EFM_REPORT_RE = re.compile(r"(?:Writing decoding report to:|Report file\s*:\s*)(.+?)\s*$", re.IGNORECASE)
EFM_SUCCESS_RE = re.compile(r"EFMSink:\s*Success:\s*EFM decoded to\s+(.+?)\s*$", re.IGNORECASE)
QUALITY_PATTERNS = {
    "assessment": re.compile(r"Overall assessment\s*:\s*(.+)", re.IGNORECASE),
    "post_c2_data_loss_pct": re.compile(r"Data loss \(post-C2\)\s*:\s*([\d.]+)\s*%", re.IGNORECASE),
    "c1_uncorrectable_pct": re.compile(r"C1 uncorrectable\s*:\s*[\d,]+\s*\(([\d.]+)\s*%\)", re.IGNORECASE),
    "c2_uncorrectable_pct": re.compile(r"C2 uncorrectable\s*:\s*[\d,]+\s*\(([\d.]+)\s*%\)", re.IGNORECASE),
    "missing_sections": re.compile(r"Sections missing\s*:\s*([\d,]+)", re.IGNORECASE),
    "concealed_samples": re.compile(r"Concealed \(interp\)\s*:\s*([\d,]+)", re.IGNORECASE),
    "muted_samples": re.compile(r"Silenced \(muted\)\s*:\s*([\d,]+)", re.IGNORECASE),
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_source_file(path: Path, capture: dict, episode_data: dict,
                       *, name_key: str, bytes_key: str, md5_key: str,
                       inventory_key: str) -> dict:
    expected_bytes = capture.get(bytes_key)
    if type(expected_bytes) is not int or path.stat().st_size != expected_bytes:
        raise PipelineError(f"{inventory_key.upper()} size differs from verified manifest: {path}")
    expected_md5 = str(capture.get(md5_key) or "").lower()
    if re.fullmatch(r"[0-9a-f]{32}", expected_md5):
        actual_md5 = file_md5(path)
        if actual_md5 != expected_md5:
            raise PipelineError(f"{inventory_key.upper()} MD5 differs from verified manifest for {path}")
        return {"method": "manifest-md5", "md5": actual_md5}
    provenance = episode_data.get("source_provenance") or {}
    inventory = provenance.get("inventory") or {}
    inventory_captures = inventory.get("captures") or []
    source = next((row for row in inventory_captures if row.get("label") == capture.get("label")), None)
    expected = ((source or {}).get("files") or {}).get(inventory_key) or {}
    if (provenance.get("mode") != "local-inventory" or
            expected.get("name") != capture.get(name_key) or
            expected.get("size_bytes") != expected_bytes or
            not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256", "")).lower())):
        raise PipelineError(f"{inventory_key.upper()} has neither a manifest MD5 nor a matching local-source fingerprint")
    policy = str(inventory.get("fingerprint_policy") or "")
    try:
        actual = fingerprint_file(path, policy)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot verify local {inventory_key.upper()} fingerprint: {exc}") from exc
    if (actual.get("sha256") != expected.get("sha256") or
            actual.get("hash_scope") != expected.get("hash_scope") or
            actual.get("size_bytes") != expected_bytes):
        raise PipelineError(f"local {inventory_key.upper()} fingerprint differs from manifest inventory: {path}")
    return {"method": "local-inventory-sha256", **actual}


def verify_source_tbc(path: Path, capture: dict, episode_data: dict) -> dict:
    return verify_source_file(path, capture, episode_data, name_key="tbc_file",
                              bytes_key="tbc_bytes", md5_key="tbc_md5", inventory_key="tbc")


def _source_path(downloads: Path, name: str, label: str) -> Path:
    relative = Path(name)
    if (not name or relative.is_absolute() or len(relative.parts) != 1 or
            any(part in (".", "..") for part in relative.parts)):
        raise PipelineError(f"manifest {label} must be one safe filename: {name!r}")
    raw = downloads / relative
    if raw.is_symlink():
        raise PipelineError(f"manifest {label} may not be a symlink: {raw}")
    resolved = raw.resolve(strict=True)
    try:
        resolved.relative_to(downloads.resolve(strict=True))
    except ValueError as exc:
        raise PipelineError(f"manifest {label} escaped the selected downloads folder") from exc
    if not resolved.is_file():
        raise PipelineError(f"manifest {label} is not a regular file: {resolved}")
    return resolved


def _decode_log(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"Orc EFM log is missing or unsafe: {path}")
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _logged_paths(text: str, pattern: re.Pattern[str]) -> list[Path]:
    paths = []
    for line in text.splitlines():
        match = pattern.search(line.strip())
        if match:
            value = match.group(1).strip().strip("\"'")
            paths.append(Path(value).expanduser())
    return paths


def _efm_quality_summary(report_path: Path) -> dict:
    raw = report_path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    result = {}
    for key, pattern in QUALITY_PATTERNS.items():
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            result[key] = int(value.replace(",", "")) if key in (
                "missing_sections", "concealed_samples", "muted_samples") else (
                value.upper() if key == "assessment" else float(value))
    return result


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(str(right.resolve(strict=False)))


def verify_efm_log(log_path: Path, expected_tbc: Path, wav_path: Path,
                   report_path: Path, metadata_path: Path | None = None) -> dict:
    text = _decode_log(log_path)
    metadata_paths = _logged_paths(text, TBC_JSON_RE)
    loaded_paths = _logged_paths(text, TBC_LOADED_RE)
    outputs = _logged_paths(text, EFM_OUTPUT_RE)
    reports = _logged_paths(text, EFM_REPORT_RE)
    successful = _logged_paths(text, EFM_SUCCESS_RE)
    expected_metadata = metadata_path or Path(str(expected_tbc) + ".json")
    checks = {
        "loaded_tbc": bool(loaded_paths) and all(_same_path(path, expected_tbc) for path in loaded_paths),
        "tbc_metadata": bool(metadata_paths) and all(_same_path(path, expected_metadata) for path in metadata_paths),
        "efm_output": bool(outputs) and all(_same_path(path, wav_path) for path in outputs),
        "efm_report": bool(reports) and all(_same_path(path, report_path) for path in reports),
        "efm_success": bool(successful) and all(_same_path(path, wav_path) for path in successful),
    }
    if not all(checks.values()):
        details = {
            "expected_tbc": str(expected_tbc),
            "logged_loaded_tbc": [str(path) for path in loaded_paths],
            "expected_tbc_metadata": str(expected_metadata),
            "logged_tbc_metadata": [str(path) for path in metadata_paths],
            "expected_wav": str(wav_path), "logged_wav": [str(path) for path in outputs],
            "expected_report": str(report_path), "logged_report": [str(path) for path in reports],
            "logged_success": [str(path) for path in successful],
        }
        failed = ", ".join(key for key, ok in checks.items() if not ok)
        raise PipelineError(f"EFMSink log provenance mismatch ({failed}); log may belong to another episode/capture: {details}")
    return {"checks": checks, "log_sha256": file_sha256(log_path)}


def verify_capture(episode: str, series: str, capture: str, log_path: Path,
                  wav_path: Path, report_path: Path, provenance_out: Path,
                  expected_manifest_sha256: str,
                  profile_path: Path | None = None) -> Path:
    series = series.upper()
    episode = episode.upper()
    identity = f"{series}:{episode}"
    os.environ["LDP_SERIES"] = series
    os.environ["LDP_EPISODE"] = episode
    os.environ["LDP_IDENTITY"] = identity
    if profile_path:
        os.environ["LDP_PROFILE"] = str(profile_path.expanduser().resolve(strict=True))
    ld = Ld(profile_path)
    if (ld.profile.get("shared") or {}).get("enabled") is not True:
        raise PipelineError("EFM provenance records must be stored in the verified device workspace")
    workspace = Path(os.environ.get("LDP_INSTANCE_ROOT", "")).expanduser().resolve(strict=True)
    validate_device_config_profile(ld.profile_path, ld.profile, ROOT)
    validate_instance(str(workspace), ld.profile_path, ld.profile, identity)
    ep = load_episode(identity, ld, series=series)
    manifest_path = episode_manifest_path(episode, ld, series=series).resolve(strict=True)
    assert_writable_path(manifest_path, ld)
    manifest_original = manifest_path.read_bytes()
    manifest_sha_before = hashlib.sha256(manifest_original).hexdigest()
    if manifest_sha_before != expected_manifest_sha256.lower():
        raise PipelineError("episode manifest changed after the EFM verification plan was created")
    try:
        manifest_data = json.loads(manifest_original.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read episode manifest before EFM provenance update: {exc}") from exc
    audio_data = manifest_data.get("audio") or {}
    if (not isinstance(audio_data, dict) or audio_data.get("efm_provenance_required") is not True or
            not isinstance(audio_data.get("efm_provenance"), dict)):
        raise PipelineError("manifest must explicitly enable V16 audio.efm_provenance_required with an object inventory")
    selected = next((item for item in ep.data.get("captures", [])
                     if str(item.get("label", "")).casefold() == capture.casefold()), None)
    if selected is None:
        raise PipelineError(f"capture {capture!r} is not in {identity}'s manifest")
    tbc_name = selected.get("tbc_file")
    if not isinstance(tbc_name, str) or not tbc_name:
        raise PipelineError(f"capture {capture!r} has no manifest tbc_file")
    downloads = ep.downloads()
    expected_tbc = _source_path(downloads, tbc_name, "TBC source")
    expected_bytes = selected.get("tbc_bytes")
    source_fingerprint = verify_source_tbc(expected_tbc, selected, ep.data)
    metadata_name = selected.get("json_file")
    if not isinstance(metadata_name, str) or not metadata_name:
        raise PipelineError(f"capture {capture!r} has no manifest JSON metadata file")
    expected_metadata = _source_path(downloads, metadata_name, "TBC JSON metadata")
    metadata_fingerprint = verify_source_file(
        expected_metadata, selected, ep.data, name_key="json_file",
        bytes_key="json_bytes", md5_key="json_md5", inventory_key="json")
    raw_wav = wav_path.expanduser()
    raw_efm_report = report_path.expanduser()
    if raw_wav.is_symlink() or raw_efm_report.is_symlink():
        raise PipelineError("EFM WAV/report paths may not be symlinks")
    wav = raw_wav.resolve(strict=True)
    efm_report = raw_efm_report.resolve(strict=True)
    if not wav.is_file() or wav.stat().st_size <= 0:
        raise PipelineError(f"EFM WAV is missing or empty: {wav}")
    if not efm_report.is_file() or efm_report.stat().st_size <= 0:
        raise PipelineError(f"EFMSink report is missing or empty: {efm_report}")
    audio_root = ld.root("audio").resolve(strict=True)
    try:
        wav_relative = wav.relative_to(audio_root)
        efm_report_relative = efm_report.relative_to(audio_root)
    except ValueError as exc:
        raise PipelineError(f"EFM WAV and text report must be inside configured roots.audio: {wav}; {efm_report}") from exc
    raw_log = log_path.expanduser()
    raw_out = provenance_out.expanduser()
    if raw_log.is_symlink() or raw_out.is_symlink():
        raise PipelineError("EFM log/provenance output path may not be a symlink")
    match = verify_efm_log(raw_log.resolve(strict=True), expected_tbc, wav,
                           efm_report, expected_metadata)
    out = raw_out.resolve(strict=False)
    allowed = workspace / "references"
    try:
        out.relative_to(allowed.resolve(strict=True))
    except ValueError as exc:
        raise PipelineError(f"EFM provenance report must be under this device episode references/: {allowed}") from exc
    assert_writable_path(out, ld)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() or out.is_symlink():
        raise PipelineError(f"refusing to overwrite EFM provenance report: {out}")
    report = {
        "schema": SCHEMA, "status": "VERIFIED", "identity": identity,
        "capture": str(selected["label"]), "created_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256_before": manifest_sha_before,
        "verified_by": __import__("getpass").getuser(),
        "source_tbc": tbc_name, "source_tbc_bytes": expected_bytes,
        "source_tbc_manifest_md5": selected.get("tbc_md5"),
        "source_tbc_fingerprint": source_fingerprint,
        "source_tbc_json": metadata_name,
        "source_tbc_json_bytes": selected.get("json_bytes"),
        "source_tbc_json_md5": selected.get("json_md5"),
        "source_tbc_json_fingerprint": metadata_fingerprint,
        "orc_log_name": log_path.name, "orc_log_sha256": match["log_sha256"],
        "wav_name": wav.name, "wav_relative_path": wav_relative.as_posix(),
        "wav_size_bytes": wav.stat().st_size,
        "wav_sha256": file_sha256(wav),
        "efm_report_name": efm_report.name, "efm_report_size_bytes": efm_report.stat().st_size,
        "efm_report_relative_path": efm_report_relative.as_posix(),
        "efm_report_sha256": file_sha256(efm_report),
        "quality_summary": _efm_quality_summary(efm_report),
        "path_checks": match["checks"],
        "selection_note": "provenance only; this report does not rank or select the audio capture",
    }
    temp = out.with_name(f".{out.name}.{os.getpid()}.{secrets.token_hex(5)}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if out.exists():
            raise PipelineError(f"refusing to overwrite EFM provenance report: {out}")
        os.replace(temp, out)
    finally:
        temp.unlink(missing_ok=True)
    lock_path = manifest_path.with_name(manifest_path.name + ".efm-provenance.lock")
    assert_writable_path(lock_path, ld)
    lock = None
    try:
        lock = lock_path.open("x", encoding="utf-8", newline="\n")
        lock.write(f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n")
        lock.flush()
        os.fsync(lock.fileno())
        if manifest_path.read_bytes() != manifest_original:
            raise PipelineError("episode manifest changed during EFM provenance verification")
        audio = manifest_data.setdefault("audio", {})
        if not isinstance(audio, dict):
            raise PipelineError("manifest audio section is not an object")
        provenance = audio.setdefault("efm_provenance", {})
        if not isinstance(provenance, dict):
            raise PipelineError("manifest audio.efm_provenance is not an object")
        record_path = out.relative_to(workspace).as_posix()
        history = provenance.setdefault(str(selected["label"]), [])
        if not isinstance(history, list):
            raise PipelineError(f"manifest EFM provenance for {selected['label']} is not a list")
        history.append({
            "report_path": record_path,
            "report_sha256": file_sha256(out),
            "wav_sha256": report["wav_sha256"],
            "verified_utc": report["created_utc"],
        })
        backup_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = manifest_path.with_name(f"{manifest_path.name}.bak-efm-provenance-{backup_stamp}")
        with backup.open("xb") as stream:
            stream.write(manifest_original)
            stream.flush()
            os.fsync(stream.fileno())
        manifest_temp = manifest_path.with_name(
            f".{manifest_path.name}.{os.getpid()}.{secrets.token_hex(5)}.tmp")
        try:
            with manifest_temp.open("xb") as stream:
                stream.write((json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if manifest_path.read_bytes() != manifest_original:
                raise PipelineError("episode manifest changed before EFM provenance update")
            os.replace(manifest_temp, manifest_path)
        finally:
            manifest_temp.unlink(missing_ok=True)
    finally:
        if lock is not None:
            lock.close()
            lock_path.unlink(missing_ok=True)
    print(f"EFM provenance PASS: {identity} capture={capture}; report={out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"))
    parser.add_argument("--capture", required=True)
    parser.add_argument("--orc-log", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--efm-report", required=True)
    parser.add_argument("--report-out", required=True)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    try:
        verify_capture(args.episode, args.series or os.environ.get("LDP_SERIES", "DS9"),
                       args.capture, Path(args.orc_log),
                       Path(args.wav), Path(args.efm_report), Path(args.report_out),
                       args.confirm_manifest_sha256,
                       Path(args.profile) if args.profile else None)
        return 0
    except (PipelineError, LockError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"EFM PROVENANCE ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
