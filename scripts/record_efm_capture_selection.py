#!/usr/bin/env python3
"""Record a human-selected EFM capture after every candidate decode is provenance-verified."""
from __future__ import annotations

import argparse
import getpass
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
from local_source import fingerprint_file
from verify_efm_capture import verify_source_file
from render_stack import project_source_labels
from shared_workspace import validate_device_config_profile, validate_instance

PAIR_RE = re.compile(r"\s*(\d+)\s*\+\s*(-?\d+)\s*")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_alignment_map(value: str, labels: list[str]) -> dict[str, int]:
    parts = [item.strip() for item in value.split(",") if item.strip()]
    if len(parts) != len(labels):
        raise PipelineError("alignment_map must contain exactly one entry for each refreshed Orc source")
    offsets: dict[str, int] = {}
    for expected_index, (part, label) in enumerate(zip(parts, labels), start=1):
        match = PAIR_RE.fullmatch(part)
        if not match or int(match.group(1)) != expected_index:
            raise PipelineError("alignment_map source indices do not match the refreshed Orc project order")
        offset = int(match.group(2))
        if offset < 0:
            raise PipelineError(f"alignment_map has negative prepended-pad count for {label}")
        offsets[label] = offset
    return offsets


def _manifest_lock(path: Path):
    class Lock:
        def __enter__(self):
            self.stream = path.open("x", encoding="utf-8", newline="\n")
            self.stream.write(f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            return self

        def __exit__(self, exc_type, exc, tb):
            self.stream.close()
            path.unlink(missing_ok=True)
            return False
    return Lock()


def _manifest_lock(path: Path):
    class Lock:
        def __enter__(self):
            self.stream = path.open("x", encoding="utf-8", newline="\n")
            self.stream.write(f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            return self

        def __exit__(self, exc_type, exc, tb):
            self.stream.close()
            path.unlink(missing_ok=True)
            return False
    return Lock()


def record_selection(episode: str, series: str, capture: str, provenance_path: Path,
                     expected_provenance_sha256: str, expected_manifest_sha256: str,
                     selection_note: str, *, apply: bool) -> Path:
    episode, series = episode.upper(), series.upper()
    identity = f"{series}:{episode}"
    if len(selection_note.strip()) < 8:
        raise PipelineError("capture selection requires a specific human note of at least 8 characters")
    os.environ["LDP_SERIES"] = series
    os.environ["LDP_EPISODE"] = episode
    os.environ["LDP_IDENTITY"] = identity
    ld = Ld()
    if (ld.profile.get("shared") or {}).get("enabled") is not True:
        raise PipelineError("EFM capture selection requires the verified device workspace profile")
    instance = Path(os.environ.get("LDP_INSTANCE_ROOT", "")).expanduser().resolve(strict=True)
    validate_device_config_profile(ld.profile_path, ld.profile, ROOT)
    validate_instance(str(instance), ld.profile_path, ld.profile, identity)
    raw_provenance_path = provenance_path.expanduser()
    if raw_provenance_path.is_symlink():
        raise PipelineError(f"EFM provenance report may not be a symlink: {raw_provenance_path}")
    provenance_path = raw_provenance_path.resolve(strict=True)
    if not provenance_path.is_file():
        raise PipelineError(f"EFM provenance report is missing or unsafe: {provenance_path}")
    try:
        provenance_path.relative_to((instance / "references").resolve(strict=True))
        report = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"EFM provenance report must be readable under the active references folder: {exc}") from exc
    provenance_sha = sha256_file(provenance_path)
    if provenance_sha != expected_provenance_sha256.lower():
        raise PipelineError("EFM provenance report changed after it was reviewed")
    if (report.get("schema") != "ld-pipeline-efm-capture-provenance-v1" or
            report.get("status") != "VERIFIED" or report.get("identity") != identity or
            str(report.get("capture", "")).casefold() != capture.casefold()):
        raise PipelineError("selected capture does not match the verified EFM provenance report")

    manifest_path = episode_manifest_path(episode, ld, series=series).resolve(strict=True)
    assert_writable_path(manifest_path, ld)
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256.lower():
        raise PipelineError("manifest changed after the EFM capture-selection plan was created")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read current episode manifest: {exc}") from exc
    audio = manifest.get("audio") or {}
    if audio.get("efm_provenance_required") is not True:
        raise PipelineError("manifest does not require V16 EFM provenance; do not silently promote a legacy capture")
    verified = audio.get("efm_provenance") or {}
    if not isinstance(verified, dict):
        raise PipelineError("manifest audio.efm_provenance must be an object keyed by capture label")
    labels = [str(row.get("label", "")) for row in manifest.get("captures", [])]
    if not labels or len(labels) != len(set(labels)):
        raise PipelineError("manifest capture labels are missing or duplicated")
    missing = [label for label in labels if not isinstance(verified.get(label), list) or not verified[label]]
    if missing:
        raise PipelineError("verify each capture's EFM log/WAV/report before selecting one; missing: " + ", ".join(missing))
    capture_rows = {str(row.get("label", "")): row for row in manifest.get("captures", [])}
    for label in labels:
        latest = verified[label][-1]
        if not isinstance(latest, dict):
            raise PipelineError(f"EFM provenance entry for {label} is malformed")
        relative_report = Path(str(latest.get("report_path", "")))
        if relative_report.is_absolute() or any(part in ("..", "") for part in relative_report.parts):
            raise PipelineError(f"EFM provenance path for {label} is not workspace-relative")
        report_file = instance / relative_report
        try:
            if report_file.is_symlink():
                raise OSError("report is a symlink")
            report_file.resolve(strict=True).relative_to((instance / "references").resolve(strict=True))
            candidate_report = json.loads(report_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"verified EFM report for {label} is missing or unsafe: {exc}") from exc
        if (sha256_file(report_file) != latest.get("report_sha256") or
                candidate_report.get("schema") != "ld-pipeline-efm-capture-provenance-v1" or
                candidate_report.get("status") != "VERIFIED" or
                candidate_report.get("identity") != identity or
                candidate_report.get("capture") != label or
                candidate_report.get("source_tbc") != capture_rows[label].get("tbc_file") or
                candidate_report.get("source_tbc_manifest_md5") != capture_rows[label].get("tbc_md5")):
            raise PipelineError(f"EFM provenance for {label} is stale or bound to a different source")
        downloads = ld.downloads_dir(
            episode, (manifest.get("roots") or {}).get("downloads"), series)
        tbc_name = str(capture_rows[label].get("tbc_file", ""))
        tbc_relative = Path(tbc_name)
        if (not tbc_name or tbc_relative.is_absolute() or len(tbc_relative.parts) != 1 or
                any(part in (".", "..") for part in tbc_relative.parts)):
            raise PipelineError(f"TBC source for {label} is not one safe filename")
        tbc_path = downloads / tbc_relative
        source_md5 = str(capture_rows[label].get("tbc_md5") or "").lower()
        tbc_ok = (not tbc_path.is_symlink() and tbc_path.is_file() and
                  tbc_path.stat().st_size == capture_rows[label].get("tbc_bytes"))
        if tbc_ok and source_md5:
            digest = hashlib.md5()
            with tbc_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            tbc_ok = digest.hexdigest() == source_md5
            tbc_ok = tbc_ok and candidate_report.get("source_tbc_manifest_md5") == source_md5
        elif tbc_ok:
            inventory = ((manifest.get("source_provenance") or {}).get("inventory") or {})
            inventory_capture = next((row for row in inventory.get("captures", [])
                                      if row.get("label") == label), {})
            expected_fingerprint = ((inventory_capture.get("files") or {}).get("tbc") or {})
            try:
                actual_fingerprint = fingerprint_file(
                    tbc_path, str(inventory.get("fingerprint_policy") or ""))
            except (OSError, ValueError) as exc:
                raise PipelineError(f"cannot verify local TBC fingerprint for {label}: {exc}") from exc
            tbc_ok = (expected_fingerprint.get("name") == capture_rows[label].get("tbc_file") and
                      expected_fingerprint.get("sha256") == actual_fingerprint.get("sha256") and
                      expected_fingerprint.get("hash_scope") == actual_fingerprint.get("hash_scope") and
                      candidate_report.get("source_tbc_fingerprint") ==
                      {"method": "local-inventory-sha256", **actual_fingerprint})
        if (tbc_path.is_symlink() or not tbc_path.is_file() or
                not tbc_ok):
            raise PipelineError(f"TBC source for {label} changed since EFM provenance verification")
        json_name = str(capture_rows[label].get("json_file") or "")
        if not json_name or Path(json_name).is_absolute() or len(Path(json_name).parts) != 1:
            raise PipelineError(f"TBC JSON source for {label} is not one safe filename")
        metadata_path = downloads / json_name
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise PipelineError(f"TBC JSON source for {label} is missing or unsafe")
        metadata_fingerprint = verify_source_file(
            metadata_path, capture_rows[label], manifest, name_key="json_file",
            bytes_key="json_bytes", md5_key="json_md5", inventory_key="json")
        if (candidate_report.get("source_tbc_json") != json_name or
                candidate_report.get("source_tbc_json_bytes") != capture_rows[label].get("json_bytes") or
                candidate_report.get("source_tbc_json_md5") != capture_rows[label].get("json_md5") or
                candidate_report.get("source_tbc_json_fingerprint") != metadata_fingerprint):
            raise PipelineError(f"TBC JSON metadata for {label} changed since EFM provenance verification")
        wav_rel = Path(str(candidate_report.get("wav_relative_path", "")))
        if wav_rel.is_absolute() or any(part in ("..", "") for part in wav_rel.parts):
            raise PipelineError(f"EFM WAV for {label} is not roots.audio-relative")
        raw_wav = ld.root("audio") / wav_rel
        if raw_wav.is_symlink() or not raw_wav.is_file() or (
                raw_wav.stat().st_size != candidate_report.get("wav_size_bytes") or
                sha256_file(raw_wav) != candidate_report.get("wav_sha256")):
            raise PipelineError(f"EFM WAV for {label} changed since provenance verification")
        efm_report_rel = Path(str(candidate_report.get("efm_report_relative_path", "")))
        if efm_report_rel.is_absolute() or any(part in ("..", "") for part in efm_report_rel.parts):
            raise PipelineError(f"EFMSink text report for {label} is not roots.audio-relative")
        raw_report = ld.root("audio") / efm_report_rel
        if raw_report.is_symlink() or not raw_report.is_file() or (
                raw_report.stat().st_size != candidate_report.get("efm_report_size_bytes") or
                sha256_file(raw_report) != candidate_report.get("efm_report_sha256")):
            raise PipelineError(f"EFMSink text report for {label} changed since provenance verification")
    selected_records = [row for row in verified.get(capture, [])
                        if isinstance(row, dict) and row.get("report_sha256") == provenance_sha]
    report_relpath = provenance_path.relative_to(instance).as_posix()
    selected_records = [row for row in selected_records if row.get("report_path") == report_relpath]
    if (not selected_records or not verified[capture] or
            selected_records[-1] is not verified[capture][-1]):
        raise PipelineError("selection must use the latest verified EFM report recorded in the manifest")
    if report.get("source_tbc") != next((row.get("tbc_file") for row in manifest["captures"]
                                         if row.get("label") == capture), None):
        raise PipelineError("provenance TBC filename differs from the selected manifest capture")

    from ldcommon import Episode
    ep = Episode(episode, manifest, ld, series)
    project = ep.path("stack_project").resolve(strict=True)
    source_order = project_source_labels(project)
    if len(source_order) != len(labels) or set(source_order) != set(labels) or source_order[0] != ep.reference:
        raise PipelineError("refreshed Orc project source labels do not match the manifest/reference")
    pads_by_label = parse_alignment_map(str(manifest.get("alignment_map") or ""), source_order)
    pad_frames = pads_by_label.get(capture)
    if pad_frames is None:
        raise PipelineError(f"selected capture {capture} is absent from the refreshed alignment map")
    wav_relative = Path(str(report.get("wav_relative_path", "")))
    if wav_relative.is_absolute() or any(part in ("..", "") for part in wav_relative.parts):
        raise PipelineError("verified EFM WAV path is not relative to roots.audio")
    raw_wav_path = ld.root("audio") / wav_relative
    if raw_wav_path.is_symlink():
        raise PipelineError("selected EFM WAV may not be a symlink")
    wav_path = raw_wav_path.resolve(strict=True)
    if (wav_path.stat().st_size != report.get("wav_size_bytes") or
            sha256_file(wav_path) != report.get("wav_sha256")):
        raise PipelineError("selected EFM WAV no longer matches the verified provenance report")

    if not apply:
        print(f"ready to record EFM capture {capture}, pad_frames={pad_frames}, identity={identity}")
        return manifest_path
    lock_path = manifest_path.with_name(manifest_path.name + ".efm-selection.lock")
    assert_writable_path(lock_path, ld)
    with _manifest_lock(lock_path):
        original = manifest_path.read_bytes()
        if hashlib.sha256(original).hexdigest() != hashlib.sha256(manifest_bytes).hexdigest():
            raise PipelineError("manifest changed while waiting to record EFM capture selection")
        current = json.loads(original.decode("utf-8-sig"))
        current_audio = current.setdefault("audio", {})
        previous_selection = current_audio.get("capture_selection")
        selection_history = current_audio.setdefault("capture_selection_history", [])
        if not isinstance(selection_history, list):
            raise PipelineError("manifest audio.capture_selection_history must be an array")
        if previous_selection and isinstance(previous_selection, dict):
            selection_history.append(previous_selection)
        current_audio["capture"] = capture
        current_audio["pad_frames"] = pad_frames
        current_audio["capture_selection"] = {
            "schema": "ld-pipeline-efm-capture-selection-v1",
            "capture": capture,
            "pad_frames": pad_frames,
            "verified_provenance_report": report_relpath,
            "verified_provenance_sha256": provenance_sha,
            "alignment_map": str(current.get("alignment_map") or ""),
            "orc_source_order": source_order,
            "selection_note": selection_note.strip(),
            "selected_by": getpass.getuser(),
            "selected_utc": datetime.now(timezone.utc).isoformat(),
        }
        current.setdefault("files", {})["ld_native"] = {
            "root": "audio", "name": wav_relative.as_posix(),
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = manifest_path.with_name(f"{manifest_path.name}.bak-efm-selection-{stamp}")
        with backup.open("xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        temp = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            with temp.open("xb") as stream:
                stream.write((json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if manifest_path.read_bytes() != original:
                raise PipelineError("manifest changed before EFM capture selection replace")
            os.replace(temp, manifest_path)
        finally:
            temp.unlink(missing_ok=True)
    print(f"recorded EFM capture selection {identity}:{capture}, pad_frames={pad_frames}; backup={backup}")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--series", choices=("DS9", "VOY"))
    parser.add_argument("--capture", required=True)
    parser.add_argument("--provenance-report", required=True)
    parser.add_argument("--confirm-provenance-sha256", required=True)
    parser.add_argument("--confirm-manifest-sha256", required=True)
    parser.add_argument("--selection-note", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        identity = os.environ.get("LDP_IDENTITY", "")
        series = (args.series or (identity.split(":", 1)[0] if ":" in identity else "DS9")).upper()
        record_selection(args.episode, series, args.capture, Path(args.provenance_report),
                         args.confirm_provenance_sha256, args.confirm_manifest_sha256,
                         args.selection_note, apply=args.apply)
        return 0
    except (PipelineError, LockError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"EFM SELECTION ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
