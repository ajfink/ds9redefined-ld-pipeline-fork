#!/usr/bin/env python3
"""Deterministic manifest/stage validator for OpenCode and shell workflows.

Exit 0 means the requested stage's declared prerequisites are present. Exit 1 is
a failed gate; exit 2 is a malformed/missing profile or manifest. The validator
never fills, guesses, or edits episode data.

  python scripts\\validate_episode.py --episode S01E05 --stage inputs
  python scripts\\validate_episode.py --episode S01E05 --stage retime
  python scripts\\validate_episode.py --episode S01E05 --stage inputs --check-hashes
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402
from local_source import LocalSourceError, fingerprint_file  # noqa: E402
from fileutil import hash_file  # noqa: E402

STAGES = ("manifest", "inputs", "stack", "cadence", "audio", "retime", "encode", "padfix", "deliverable")


class Checks:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str):
        self.rows.append((status, name, detail))

    def require(self, condition: bool, name: str, detail: str):
        self.add("PASS" if condition else "FAIL", name, detail)

    def file(self, path: Path | None, name: str, optional: bool = False) -> bool:
        if path is not None and path.is_file():
            self.add("PASS", name, str(path))
            return True
        self.add("WARN" if optional else "FAIL", name, f"missing: {path or '<not recorded>'}")
        return False

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _, _ in self.rows)


def md5(path: Path) -> str:
    return hash_file(path, algorithm="md5", chunk_size=8 * 1024 * 1024)


def resolve(ep, key: str) -> Path | None:
    try:
        return ep.path(key)
    except PipelineError:
        return None


def nonnull(data: dict, section: str, key: str) -> bool:
    return isinstance(data.get(section), dict) and data[section].get(key) is not None


def trim_review_is_current(ep, data: dict, ld: Ld) -> tuple[bool, str]:
    video = data.get("video") or {}
    audio = data.get("audio") or {}
    review = video.get("trim_review") or {}
    head, tail, pad = video.get("head_trim"), video.get("tail_trim"), audio.get("pad_frames")
    if (review.get("status") != "APPROVED" or type(head) is not int or head < 0 or
            type(tail) is not int or tail < 0 or type(pad) is not int or pad < 0 or
            review.get("head_trim") != head or review.get("tail_trim") != tail or
            review.get("audio_pad_frames") != pad or
            review.get("capture") != audio.get("capture")):
        return False, "visual trim approval is missing or does not match the measured trim/pad values"
    try:
        source = ep.path("stack_master")
        native = ep.path("ld_native")
        if (sha256(source) != review.get("stack_master_sha256") or
                sha256(native) != review.get("native_audio_sha256")):
            return False, "stack master or native audio changed since trim review"
        selected = str(audio.get("capture") or "")
        provenance = (audio.get("efm_provenance") or {}).get(selected) or []
        if (not provenance or not isinstance(provenance[-1], dict) or
                provenance[-1].get("report_sha256") != review.get("efm_provenance_sha256")):
            return False, "trim review is not bound to current selected-capture provenance"
        capture = next((row for row in ep.data.get("captures", [])
                        if str(row.get("label")) == selected), None)
        if capture is None:
            return False, "selected capture inventory is missing"
        provenance_checks = Checks()
        validate_efm_capture_provenance(provenance_checks, ep, ld, capture, native)
        if provenance_checks.failed:
            return False, "selected EFM provenance is no longer current"
        if data.get("dvd_reference_available") is True:
            alignment_review = video.get("dvd_alignment_review") or {}
            alignment = ep.path("align_npy")
            raw = Path(str(alignment_review.get("path") or ""))
            if not raw.is_absolute():
                parts = raw.parts
                if not parts or parts[0].casefold() != "probe" or ".." in parts:
                    return False, "approved alignment path is unsafe"
                raw = ld.probe() / Path(*parts[1:])
            if (alignment_review.get("status") != "APPROVED" or
                    raw.resolve(strict=True) != alignment.resolve(strict=True) or
                    sha256(alignment) != alignment_review.get("sha256") or
                    sha256(ep.path("dvd")) != alignment_review.get("dvd_source_sha256") or
                    sha256(source) != alignment_review.get("master_source_sha256") or
                    review.get("alignment_sha256") != alignment_review.get("sha256") or
                    review.get("review_assets_sha256") != alignment_review.get("review_assets_sha256")):
                return False, "trim review is not bound to the current approved DVD/LD alignment"
            assets_raw = Path(str(alignment_review.get("review_assets_path") or ""))
            if assets_raw.is_symlink():
                return False, "paired boundary assets may not be a symbolic link"
            if not assets_raw.is_absolute():
                parts = assets_raw.parts
                if not parts or parts[0].casefold() != "probe" or ".." in parts:
                    return False, "paired boundary asset path is unsafe"
                assets_raw = ld.probe() / Path(*parts[1:])
            if sha256(assets_raw) != alignment_review.get("review_assets_sha256"):
                return False, "paired boundary assets changed since alignment review"
        elif data.get("dvd_reference_available") is False and data.get("no_dvd_proceed_approved") is not True:
            return False, "no-DVD cadence requires the explicit no-DVD route approval"
        return True, "visual trim review is hash-bound to current video/audio evidence"
    except (OSError, ValueError, PipelineError) as exc:
        return False, f"could not verify trim review inputs: {exc}"


def _review_digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_review_path(raw: str, ld: Ld) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve(strict=True)
    parts = path.parts
    if not parts or ".." in parts or parts[0] not in ("probe", "avs"):
        raise PipelineError("review artifact has an unsafe root-relative path")
    return (ld.root(parts[0]) / Path(*parts[1:])).resolve(strict=True)


def cadence_review_is_current(ep, data: dict, ld: Ld) -> tuple[bool, str]:
    video = data.get("video") or {}
    review = video.get("cadence_review") or {}
    trim_ok, trim_detail = trim_review_is_current(ep, data, ld)
    if not trim_ok:
        return False, trim_detail
    if review.get("status") != "APPROVED":
        return False, "visual cadence approval is missing"
    if review.get("trim_review_sha256") != _review_digest(video.get("trim_review") or {}):
        return False, "head/tail trim review changed after cadence review"
    try:
        mapped = ep.path("avs_encode")
        if (_resolve_review_path(str(review.get("map_script_path") or ""), ld) != mapped.resolve(strict=True) or
                sha256(mapped) != review.get("map_script_sha256")):
            return False, "approved cadence map script changed"
        evidence = _resolve_review_path(str(review.get("metrics_path") or ""), ld)
        assets = _resolve_review_path(str(review.get("review_assets_path") or ""), ld)
        if (sha256(evidence) != review.get("metrics_sha256") or
                sha256(assets) != review.get("review_assets_sha256")):
            return False, "approved cadence metrics or visual assets changed"
        for path_key, hash_key in (
                ("tfm_log_path", "tfm_log_sha256"),
                ("motion_trace_path", "motion_trace_sha256"),
                ("motion_report_path", "motion_report_sha256"),
                ("di_map_path", "di_map_sha256"),
                ("verification_report_path", "verification_report_sha256")):
            artifact = _resolve_review_path(str(review.get(path_key) or ""), ld)
            if sha256(artifact) != review.get(hash_key):
                return False, f"approved cadence artifact changed: {path_key}"
        metrics = json.loads(evidence.read_text(encoding="utf-8-sig"))
        if (metrics.get("schema") != "cadence-measurements-v1" or
                metrics.get("identity") != ep.identity or
                metrics.get("status") != "CANDIDATE_ONLY"):
            return False, "cadence metrics report is not candidate-only evidence for this episode"
        if (type(video.get("tfm_period5_pct")) not in (int, float) or
                type(video.get("tfm_unmatched")) is not int or
                type(video.get("tfm_combed_count")) is not int or
                type(video.get("inserts")) is not int or
                type(video.get("repair_frames")) is not int or
                type(video.get("repair_pct")) not in (int, float) or
                not isinstance(video.get("tfm_combed_spans"), list)):
            return False, "manifest cadence values are missing or malformed"
        if (float(video["tfm_period5_pct"]) != float(metrics.get("period5_agreement_pct")) or
                video["tfm_unmatched"] != metrics.get("unmatched_count") or
                video["tfm_combed_count"] != metrics.get("combed_count") or
                video["inserts"] != metrics.get("insert_count") or
                video["tfm_combed_spans"] != metrics.get("combed_spans") or
                video["repair_frames"] != metrics.get("repair_frame_count") or
                float(video["repair_pct"]) != float(metrics.get("repair_frame_pct"))):
            return False, "manifest cadence measurements differ from the approved metrics report"
        return True, "visual cadence approval and evidence hashes match current files"
    except (OSError, ValueError, PipelineError) as exc:
        return False, f"could not verify cadence evidence: {exc}"


def sha256(path: Path) -> str:
    return hash_file(path)


def validate_efm_capture_provenance(checks: Checks, ep, ld: Ld, capture: dict,
                                    wav_path: Path | None) -> None:
    """Require the selected native WAV to be tied to an Orc log for this exact TBC."""
    audio = ep.data.get("audio") or {}
    if audio.get("efm_provenance_required") is not True:
        checks.add("WARN", "efm-capture-provenance",
                   "legacy manifest predates V16 EFM log/source binding; recover provenance before a new delivery")
        return
    provenance_map = audio.get("efm_provenance") or {}
    if not isinstance(provenance_map, dict):
        checks.add("FAIL", "efm-capture-provenance", "audio.efm_provenance must be an object")
        return
    entries = provenance_map.get(str(capture.get("label", "")))
    if not isinstance(entries, list) or not entries or not isinstance(entries[-1], dict):
        checks.add("FAIL", "efm-capture-provenance",
                   f"no verified Orc-log/WAV provenance for selected capture {capture.get('label')!r}")
        return
    entry = entries[-1]
    selection = audio.get("capture_selection") or {}
    checks.require(selection.get("schema") == "ld-pipeline-efm-capture-selection-v1" and
                   selection.get("capture") == capture.get("label") and
                   selection.get("verified_provenance_sha256") == entry.get("report_sha256") and
                   selection.get("verified_provenance_report") == entry.get("report_path") and
                   len(str(selection.get("selection_note", "")).strip()) >= 8 and
                   selection.get("alignment_map") == ep.data.get("alignment_map"),
                   "efm-capture-selection", "explicit selection note and provenance match the current manifest/map")
    checks.require(audio.get("pad_frames") == selection.get("pad_frames"),
                   "efm-capture-pad-frames", "audio.pad_frames matches the selected capture's Orc input order")
    instance_value = os.environ.get("LDP_INSTANCE_ROOT")
    if not instance_value:
        checks.add("FAIL", "efm-capture-provenance", "active device episode workspace is unavailable")
        return
    instance = Path(instance_value).expanduser().resolve(strict=True)
    relative = Path(str(entry.get("report_path", "")))
    if relative.is_absolute() or any(part in ("..", "") for part in relative.parts) or relative.parts[:1] != ("references",):
        checks.add("FAIL", "efm-capture-provenance", "stored provenance report path is not workspace-relative")
        return
    report_path = instance / relative
    try:
        if report_path.is_symlink():
            raise OSError("provenance report is a symlink")
        report_path.resolve(strict=True).relative_to((instance / "references").resolve(strict=True))
        report_bytes = report_path.read_bytes()
        report = json.loads(report_bytes.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        checks.add("FAIL", "efm-capture-provenance", f"verified report is missing or unsafe: {exc}")
        return
    checks.require(hashlib.sha256(report_bytes).hexdigest() == entry.get("report_sha256"),
                   "efm-provenance-report-hash", "report digest matches the manifest evidence reference")
    checks.require(report.get("schema") == "ld-pipeline-efm-capture-provenance-v1" and
                   report.get("status") == "VERIFIED" and
                   report.get("identity") == ep.identity and
                   report.get("capture") == capture.get("label"),
                   "efm-provenance-identity", "verified report is bound to this episode and selected capture")
    checks.require(report.get("source_tbc") == capture.get("tbc_file") and
                   str(report.get("source_tbc_manifest_md5") or "").lower() ==
                   str(capture.get("tbc_md5", "")).lower() and
                   report.get("source_tbc_bytes") == capture.get("tbc_bytes"),
                   "efm-provenance-source", "verified report names the selected manifest TBC and source size/hash evidence")
    try:
        tbc_name = str(capture.get("tbc_file", ""))
        tbc_relative = Path(tbc_name)
        downloads = ep.downloads().resolve(strict=True)
        source_tbc = downloads / tbc_relative
        tbc_ok = (bool(tbc_name) and not tbc_relative.is_absolute() and
                  len(tbc_relative.parts) == 1 and not any(part in (".", "..") for part in tbc_relative.parts) and
                  not source_tbc.is_symlink() and source_tbc.is_file() and
                  source_tbc.stat().st_size == capture.get("tbc_bytes"))
        if tbc_ok and capture.get("tbc_md5"):
            tbc_ok = (md5(source_tbc).lower() == str(capture.get("tbc_md5")).lower() and
                      report.get("source_tbc_fingerprint") == {
                          "method": "manifest-md5", "md5": str(capture.get("tbc_md5")).lower()})
        elif tbc_ok:
            inventory = ((ep.data.get("source_provenance") or {}).get("inventory") or {})
            source = next((row for row in inventory.get("captures", [])
                           if row.get("label") == capture.get("label")), {})
            expected_fingerprint = ((source.get("files") or {}).get("tbc") or {})
            actual_fingerprint = fingerprint_file(
                source_tbc, str(inventory.get("fingerprint_policy") or ""))
            tbc_ok = (expected_fingerprint.get("name") == capture.get("tbc_file") and
                      expected_fingerprint.get("sha256") == actual_fingerprint.get("sha256") and
                      expected_fingerprint.get("hash_scope") == actual_fingerprint.get("hash_scope") and
                      report.get("source_tbc_fingerprint") == {
                          "method": "local-inventory-sha256", **actual_fingerprint})
        if tbc_ok:
            json_name = str(capture.get("json_file", ""))
            json_relative = Path(json_name)
            metadata = downloads / json_relative
            tbc_ok = (bool(json_name) and not json_relative.is_absolute() and
                      len(json_relative.parts) == 1 and not any(part in (".", "..") for part in json_relative.parts) and
                      not metadata.is_symlink() and metadata.is_file() and
                      metadata.stat().st_size == capture.get("json_bytes"))
            if tbc_ok and capture.get("json_md5"):
                json_md5 = md5(metadata).lower()
                tbc_ok = (json_md5 == str(capture.get("json_md5")).lower() and
                          report.get("source_tbc_json_fingerprint") == {
                              "method": "manifest-md5", "md5": json_md5})
            elif tbc_ok:
                inventory = ((ep.data.get("source_provenance") or {}).get("inventory") or {})
                source = next((row for row in inventory.get("captures", [])
                               if row.get("label") == capture.get("label")), {})
                expected_fingerprint = ((source.get("files") or {}).get("json") or {})
                actual_fingerprint = fingerprint_file(
                    metadata, str(inventory.get("fingerprint_policy") or ""))
                tbc_ok = (expected_fingerprint.get("name") == capture.get("json_file") and
                          expected_fingerprint.get("sha256") == actual_fingerprint.get("sha256") and
                          expected_fingerprint.get("hash_scope") == actual_fingerprint.get("hash_scope") and
                          report.get("source_tbc_json_fingerprint") == {
                              "method": "local-inventory-sha256", **actual_fingerprint})
            tbc_ok = (tbc_ok and report.get("source_tbc_json") == capture.get("json_file") and
                      report.get("source_tbc_json_bytes") == capture.get("json_bytes") and
                      str(report.get("source_tbc_json_md5") or "").lower() ==
                      str(capture.get("json_md5", "")).lower())
        if tbc_ok:
            efm_name = str(capture.get("efm_file", ""))
            efm_relative = Path(efm_name)
            efm_source = downloads / efm_relative
            efm_ok = (bool(efm_name) and not efm_relative.is_absolute() and
                      len(efm_relative.parts) == 1 and
                      not any(part in ("", ".", "..") for part in efm_relative.parts) and
                      not efm_source.is_symlink() and efm_source.is_file() and
                      efm_source.stat().st_size == capture.get("efm_bytes"))
            if efm_ok and capture.get("efm_md5"):
                efm_md5 = md5(efm_source).lower()
                efm_ok = (efm_md5 == str(capture.get("efm_md5")).lower() and
                          report.get("source_efm_fingerprint") == {
                              "method": "manifest-md5", "md5": efm_md5})
            elif efm_ok:
                inventory = ((ep.data.get("source_provenance") or {}).get("inventory") or {})
                source = next((row for row in inventory.get("captures", [])
                               if row.get("label") == capture.get("label")), {})
                expected_fingerprint = ((source.get("files") or {}).get("efm") or {})
                actual_fingerprint = fingerprint_file(
                    efm_source, str(inventory.get("fingerprint_policy") or ""))
                efm_ok = (expected_fingerprint.get("name") == capture.get("efm_file") and
                          expected_fingerprint.get("sha256") == actual_fingerprint.get("sha256") and
                          expected_fingerprint.get("hash_scope") == actual_fingerprint.get("hash_scope") and
                          report.get("source_efm_fingerprint") == {
                              "method": "local-inventory-sha256", **actual_fingerprint})
            decoded_efm = report.get("decoded_efm") or {}
            decoded_relative = Path(str(decoded_efm.get("relative_path") or ""))
            decoded_root = decoded_efm.get("root")
            decoded_root_path = (downloads if decoded_root == "downloads" else
                                 ld.root("work") if decoded_root == "work" else None)
            decoded_path = (decoded_root_path / decoded_relative
                            if decoded_root_path is not None else None)
            decoded_ok = (decoded_path is not None and not decoded_relative.is_absolute() and
                          decoded_relative.parts and
                          all(part not in ("", ".", "..") for part in decoded_relative.parts) and
                          not decoded_path.is_symlink() and decoded_path.is_file() and
                          type(decoded_efm.get("size_bytes")) is int and
                          decoded_path.stat().st_size == decoded_efm.get("size_bytes") and
                          sha256(decoded_path) == decoded_efm.get("sha256"))
            if efm_ok and efm_source.suffix.casefold() == ".efm":
                decoded_ok = decoded_ok and (
                    decoded_root == "downloads" and
                    decoded_path.resolve(strict=True) == efm_source.resolve(strict=True))
            elif efm_ok and efm_source.name.casefold().endswith(".efm.7z"):
                decoded_ok = decoded_ok and decoded_root == "work"
            efm_ok = efm_ok and decoded_ok
            efm_ok = (efm_ok and report.get("source_efm_file") == capture.get("efm_file") and
                      report.get("source_efm_bytes") == capture.get("efm_bytes") and
                      str(report.get("source_efm_manifest_md5") or "").lower() ==
                      str(capture.get("efm_md5", "")).lower())
        else:
            efm_ok = False
    except (AttributeError, OSError, PipelineError, TypeError, ValueError):
        tbc_ok = False
        efm_ok = False
    checks.require(tbc_ok,
                   "efm-provenance-tbc", "selected TBC still exists at the manifest size")
    if report.get("source_efm_file"):
        checks.require(efm_ok,
                       "efm-provenance-source",
                       "selected raw EFM source still matches its verified manifest fingerprint")
    else:
        checks.add("WARN", "efm-provenance-source",
                   "legacy provenance does not bind the raw EFM source; reverify before a new capture selection")
    checks.require(isinstance(report.get("path_checks"), dict) and
                   all(report["path_checks"].get(key) is True for key in
                       ("loaded_tbc", "tbc_metadata", "efm_output", "efm_report", "efm_success")),
                   "efm-provenance-log", "Orc log source/output path checks all passed")
    if wav_path is None or not wav_path.is_file():
        checks.add("FAIL", "efm-provenance-wav", "selected native EFM WAV is missing")
        return
    wav_relative = Path(str(report.get("wav_relative_path", "")))
    if wav_relative.is_absolute() or any(part in ("..", "") for part in wav_relative.parts):
        checks.add("FAIL", "efm-provenance-wav-path", "report WAV path is not audio-root-relative")
        return
    expected_wav = (ld.root("audio") / wav_relative).resolve(strict=False)
    checks.require(expected_wav == wav_path.resolve(strict=False),
                   "efm-provenance-wav-path", "provenance WAV path matches files.ld_native")
    try:
        wav_ok = (not wav_path.is_symlink() and wav_path.stat().st_size == report.get("wav_size_bytes") and
                  sha256(wav_path) == report.get("wav_sha256") and
                  entry.get("wav_sha256") == report.get("wav_sha256"))
    except OSError:
        wav_ok = False
    checks.require(wav_ok, "efm-provenance-wav-hash",
                   "native WAV size and SHA-256 match the verified decode")
    efm_report_relative = Path(str(report.get("efm_report_relative_path", "")))
    if efm_report_relative.is_absolute() or any(part in ("..", "") for part in efm_report_relative.parts):
        checks.add("FAIL", "efm-provenance-text-report", "EFMSink report path is not audio-root-relative")
    else:
        efm_report_path = ld.root("audio") / efm_report_relative
        try:
            report_ok = (efm_report_path.is_file() and
                         efm_report_path.stat().st_size == report.get("efm_report_size_bytes") and
                         sha256(efm_report_path) == report.get("efm_report_sha256"))
        except OSError:
            report_ok = False
        checks.require(report_ok, "efm-provenance-text-report",
                       "EFMSink report still matches its recorded size and SHA-256")


def validate_local_inventory(checks: Checks, episode_data: dict, captures: list[dict],
                             downloads: Path, report: dict | None) -> None:
    """Verify local-only capture fingerprints without claiming Archive provenance."""
    if not isinstance(report, dict):
        checks.add("FAIL", "local-source-inventory", "manifest has no embedded local inventory evidence")
        return
    provenance = episode_data.get("source_provenance") or {}
    checks.require(provenance.get("mode") == "local-inventory" and
                   provenance.get("archive_verified") is False,
                   "source-provenance", "local inventory is not Archive-verified")
    checks.require(report.get("schema") == "ld-pipeline-local-source-inventory-v1" and
                   report.get("source_mode") == "local-inventory" and
                   report.get("archive_verified") is False,
                   "local-source-inventory-schema", str(report.get("schema")))
    checks.require(report.get("episode") == episode_data.get("episode"),
                   "local-source-inventory-episode", str(report.get("episode")))
    checks.require(str(report.get("series", "DS9")).upper() ==
                   str(episode_data.get("series", "DS9")).upper(),
                   "local-source-inventory-series", str(report.get("series", "DS9")))
    policy = report.get("fingerprint_policy")
    checks.require(policy in ("balanced", "full"), "local-source-fingerprint-policy", str(policy))
    report_captures = report.get("captures")
    if not isinstance(report_captures, list):
        checks.add("FAIL", "local-source-capture-inventory", "inventory captures must be an array")
        return
    by_label = {str(row.get("label", "")): row for row in report_captures if isinstance(row, dict)}
    labels = [str(row.get("label", "")) for row in report_captures if isinstance(row, dict)]
    checks.require(len(by_label) == len(report_captures) and len(labels) == len(set(labels)),
                   "local-source-capture-labels", f"labels={labels}")
    checks.require(set(by_label) == {str(row.get("label", "")) for row in captures},
                   "local-source-capture-match",
                   f"inventory={sorted(by_label)} manifest={[row.get('label') for row in captures]}")
    for capture in captures:
        label = str(capture.get("label", ""))
        recorded = by_label.get(label)
        if not recorded:
            continue
        files = recorded.get("files") or {}
        for role, key, bytes_key in (("TBC", "tbc", "tbc_bytes"),
                                     ("TBC JSON", "json", "json_bytes"),
                                     ("EFM", "efm", "efm_bytes")):
            name = str(capture.get(f"{key}_file") or "")
            if not name:
                if role == "EFM":
                    checks.add("WARN", f"{label}:{role}", "no raw EFM recorded for this capture")
                else:
                    checks.add("FAIL", f"{label}:{role}", "filename not identified")
                continue
            entry = files.get(key) if isinstance(files, dict) else None
            if not isinstance(entry, dict):
                checks.add("FAIL", f"{label}:{role}:fingerprint", "file is missing from local inventory report")
                continue
            checks.require(entry.get("name") == name, f"{label}:{role}:name",
                           f"inventory={entry.get('name')!r}; manifest={name!r}")
            path = downloads / name
            if not checks.file(path, f"{label}:{role}"):
                continue
            expected_size = capture.get(bytes_key)
            if expected_size is not None:
                checks.require(path.stat().st_size == int(expected_size) == int(entry.get("size_bytes", -1)),
                               f"{label}:{role}:size",
                               f"actual={path.stat().st_size}; manifest={expected_size}; inventory={entry.get('size_bytes')}")
            try:
                actual = fingerprint_file(path, str(policy))
            except (LocalSourceError, OSError) as exc:
                checks.add("FAIL", f"{label}:{role}:fingerprint", str(exc))
                continue
            expected = {key_name: entry.get(key_name)
                        for key_name in ("size_bytes", "mtime_ns", "sha256", "hash_scope")}
            checks.require(actual == expected, f"{label}:{role}:fingerprint",
                           f"policy={policy}; local source fingerprint changed" if actual != expected else
                           f"policy={policy}; {actual['hash_scope']} SHA-256 matches")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--stage", choices=STAGES, default="manifest")
    ap.add_argument("--check-hashes", action="store_true", help="recompute MD5s for source files; may take a long time")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    c = Checks()
    d = ep.data
    c.require(d.get("episode") == ep.code, "episode-code", f"manifest says {d.get('episode')!r}, requested {ep.code}")
    c.require(str(d.get("series", "DS9")).upper() == ep.series, "episode-series",
              f"manifest series says {d.get('series', 'DS9')!r}, requested {ep.series}")
    c.require(d.get("identity", ep.identity) == ep.identity, "episode-identity",
              f"manifest identity says {d.get('identity', ep.identity)!r}, requested {ep.identity}")
    archive_item = str(d.get("archive_item", ""))
    item_series = re.match(r"^(DS9|VOY|TNG)-", archive_item, re.IGNORECASE)
    item_episode = re.search(r"S\d{2}E\d{2}", archive_item, re.IGNORECASE)
    if archive_item.upper().startswith("TNG-"):
        c.require(False, "archive-series", "TNG combined/extra items are blocked from single-episode processing")
    elif item_series:
        c.require(item_series.group(1).upper() == ep.series, "archive-series",
                  f"Archive item prefix {item_series.group(1).upper()} does not match manifest series {ep.series}")
    if item_episode:
        c.require(item_episode.group(0).upper() == ep.code, "archive-episode",
                  f"Archive item episode {item_episode.group(0).upper()} does not match manifest {ep.code}")
    c.require(d.get("record_status") not in ("template", "measured-partial"), "record-status",
              f"record_status={d.get('record_status')!r}; partial/two-part records need their dedicated workflow")
    captures = d.get("captures") or []
    labels = [str(x.get("label", "")) for x in captures]
    c.require(bool(captures) and all(labels) and len(labels) == len(set(labels)), "capture-inventory",
              f"{len(captures)} capture(s); labels={labels}")
    ref = d.get("reference")
    c.require(not ref or ref in labels, "reference-inventory", f"reference={ref!r}; captures={labels}")
    if args.stage not in ("manifest", "inputs"):
        c.require(bool(ref) and ref in labels, "reference", "a measured/declared stack reference is required after input validation")
    if not d.get("title"):
        c.add("WARN", "title", "title is unconfirmed; verify from the Archive/capture filenames before delivery")
    dvd = resolve(ep, "dvd")
    dvd_available = d.get("dvd_reference_available")
    if dvd_available is None:  # backward-compatible inference for pre-V6 manifests
        dvd_available = dvd is not None
    if dvd_available:
        c.file(dvd, "dvd-reference")
    else:
        c.add("INFO", "dvd-reference", "not supplied; DVD-free mode must have explicit user approval")
        c.require(d.get("no_dvd_proceed_approved") is True, "no-dvd-approval",
                  "record the user's explicit choice to proceed without a DVD")
        image_choice = d.get("cut_pad_images_choice")
        c.require(image_choice in ("use", "skip", "not-available"), "cut-pad-image-choice",
                  "record whether available shared cut-pad stills may be used")

    if args.stage in ("inputs", "manifest"):
        downloads = ep.downloads()
        c.require(downloads.is_dir(), "download-folder", str(downloads))
        provenance = d.get("source_provenance") or {}
        if provenance.get("mode") == "local-inventory":
            validate_local_inventory(c, d, captures, downloads, provenance.get("inventory"))
            c.add("WARN", "source-origin", "local inventory fingerprints do not establish Archive origin/authenticity")
        else:
            manifest_path = downloads / "download-manifest.csv"
            c.file(manifest_path, "download-manifest")
            rows = {}
            if manifest_path.is_file():
                try:
                    with manifest_path.open(newline="", encoding="utf-8-sig") as f:
                        rows = {r.get("Name", ""): r for r in csv.DictReader(f)}
                except (OSError, csv.Error) as e:
                    c.add("FAIL", "download-manifest", str(e))
            for cap in captures:
                label = cap.get("label", "")
                tbc_name = cap.get("tbc_file")
                json_name = cap.get("json_file")
                if not tbc_name:
                    hits = list(downloads.glob(f"*-{label}.tbc"))
                    tbc_name = hits[0].name if len(hits) == 1 else None
                if not json_name and tbc_name:
                    json_name = f"{tbc_name}.json"
                efm_name = cap.get("efm_file")
                if not efm_name and tbc_name:
                    base = tbc_name[:-4]
                    efm_hits = [n for n in rows if n.casefold() in (f"{base}.efm".casefold(), f"{base}.efm.7z".casefold())]
                    if len(efm_hits) == 1:
                        efm_name = efm_hits[0]
                for role, name, bytes_key, hash_key in (
                    ("TBC", tbc_name, "tbc_bytes", "tbc_md5"),
                    ("TBC JSON", json_name, "json_bytes", "json_md5"),
                    ("EFM", efm_name, "efm_bytes", "efm_md5"),
                ):
                    if not name:
                        if role == "EFM":
                            c.add("WARN", f"{label}:{role}", "no raw EFM recorded for this capture")
                        else:
                            c.add("FAIL", f"{label}:{role}", "filename not identified")
                        continue
                    path = downloads / name
                    ok = c.file(path, f"{label}:{role}")
                    row = rows.get(name)
                    if row:
                        c.require(row.get("Status") == "verified", f"{label}:{role}:download-status", str(row.get("Status")))
                    else:
                        c.add("FAIL", f"{label}:{role}:download-status", f"not present in {manifest_path.name}")
                    expected_size, expected_hash = cap.get(bytes_key), cap.get(hash_key)
                    if expected_size is not None and ok:
                        c.require(path.stat().st_size == int(expected_size), f"{label}:{role}:size",
                                  f"actual={path.stat().st_size} expected={expected_size}")
                    if args.check_hashes and expected_hash and ok:
                        actual = md5(path)
                        c.require(actual.lower() == str(expected_hash).lower(), f"{label}:{role}:md5",
                                  f"actual={actual} expected={expected_hash}")

    if args.stage in ("stack", "cadence", "audio", "retime", "encode", "padfix", "deliverable"):
        c.require(ref in labels, "stack-reference", f"reference capture {ref!r} must be declared")

    if args.stage == "stack":
        c.require(2 <= len(captures) <= 6, "weighted-capture-count", f"weighted stack supports 2..6 captures; found {len(captures)}")
        c.file(resolve(ep, "stack_project"), "orc-project")
        c.file(resolve(ep, "stack_master"), "stack-master")
        c.require(nonnull(d, "stack", "frames") and int(d["stack"]["frames"]) > 0, "stack-frames", "measured positive stack.frames required")
        c.require(bool(d.get("alignment_map")), "alignment-map", "measured Orc alignment map required")

    if args.stage == "cadence":
        for key in ("tfm_log", "stack_master", "avs_encode"):
            c.file(resolve(ep, key), key)
        c.require(nonnull(d, "audio", "pad_frames"), "audio-pad", "measured audio.pad_frames required for frame-domain alignment")
        selected_label = str((d.get("audio") or {}).get("capture") or "")
        selected_capture = next((row for row in captures
                                 if str(row.get("label")) == selected_label), None)
        native_audio = resolve(ep, "ld_native")
        c.require((d.get("audio") or {}).get("efm_provenance_required") is True and
                  selected_capture is not None,
                  "cadence-efm-provenance", "verified selected Orc capture is required")
        if selected_capture is not None:
            validate_efm_capture_provenance(c, ep, ld, selected_capture, native_audio)
        c.require(nonnull(d, "video", "head_trim") and nonnull(d, "video", "tail_trim"), "video-trims", "measured video.head_trim and video.tail_trim required")
        windows_cadence = str(ld.profile.get("os") or "").casefold() in ("windows", "win32")
        if windows_cadence:
            trim_ok, trim_detail = trim_review_is_current(ep, d, ld)
            c.require(trim_ok, "visual-trim-review", trim_detail)
        for key in ("tfm_period5_pct", "tfm_unmatched", "inserts"):
            c.require(nonnull(d, "video", key), f"video.{key}", "record measured cadence analysis")
        if windows_cadence:
            cadence_ok, cadence_detail = cadence_review_is_current(ep, d, ld)
            c.require(cadence_ok, "visual-cadence-review", cadence_detail)

    if args.stage == "audio":
        selected = d.get("audio", {}).get("capture")
        selected_cap = next((x for x in captures if x.get("label") == selected), None)
        c.require(selected_cap is not None, "audio-capture", f"audio.capture={selected!r} must name an inventoried capture")
        if selected_cap:
            efm = selected_cap.get("efm_file")
            c.file(ep.downloads() / efm if efm else None, "selected-raw-EFM")
        if dvd_available:
            for key in ("rate_fit_ppm", "rate_fit_stderr_ppm", "asetrate", "skip_s"):
                c.require(nonnull(d, "audio", key), f"audio.{key}", "record the measured value; do not use a theory/default")
        else:
            for key in ("duration_candidate_rate_hz", "asetrate", "skip_s", "endpoint_residual_frames"):
                c.require(nonnull(d, "audio", key), f"audio.{key}", "record the accepted duration/PAD result")
            c.require(d.get("audio", {}).get("rate_method") == "dvd-free-duration-pad",
                      "audio.rate_method", "use the accepted DVD-free duration/PAD method")
            c.require(d.get("audio", {}).get("dvd_free_approved") is True,
                      "audio.dvd_free_approved", "promote only via the explicit no-DVD audio stage")
            c.require(d.get("audio", {}).get("absolute_av_reference_available") is False,
                      "audio.absolute_av_reference", "record that no external DVD A/V reference was available")
        native = resolve(ep, "ld_native")
        c.file(native, "ld-native-WAV")
        if selected_cap:
            validate_efm_capture_provenance(c, ep, ld, selected_cap, native)
        c.file(resolve(ep, "ld_corrected"), "ld-corrected-WAV")
        c.file(resolve(ep, "ld_synced"), "sample-accurate-LD-synced-FLAC")

    if args.stage == "retime":
        tracks = list(d.get("deliverable_tracks") or ["ld"])
        if not dvd_available and not ("51" in tracks or "srt" in tracks):
            c.add("SKIP", "retime", "no DVD was supplied and the deliverable is LD-only")
        else:
            c.file(resolve(ep, "dvd"), "dvd-reference")
            c.file(resolve(ep, "align_npy"), "dvd-LD-alignment-path")
            c.file(resolve(ep, "srt_source"), "English-source-SRT")
            r = d.get("retime", {})
            for key in ("breaks", "tail_off", "head_sil_end", "tail_sil_start"):
                c.require(r.get(key) is not None and (key != "breaks" or bool(r.get(key))), f"retime.{key}", "measured/adjudicated input required")
            for i, br in enumerate(r.get("breaks") or [], 1):
                if br.get("merged") or not br.get("dvd_silence"):
                    continue
                c.require(bool(br.get("ld_black")) and br.get("off_before") is not None and br.get("off_after") is not None,
                          f"retime.breaks[{i}]", "adjudicated black and before/after offsets required")

    if args.stage == "encode":
        c.file(resolve(ep, "avs_padfix"), "source-derived-PAD-repaired-encode-AVS")
        c.require(nonnull(d, "video", "frames") and int(d["video"]["frames"]) > 0, "video.frames", "probe source-derived AVS metadata and record its frame count")
        for key in ("duration_s", "head_trim", "tail_trim", "delta_s"):
            c.require(nonnull(d, "video", key), f"video.{key}", "measured value required")
        if str(ld.profile.get("os") or "").casefold() in ("windows", "win32"):
            cadence_ok, cadence_detail = cadence_review_is_current(ep, d, ld)
            c.require(cadence_ok, "visual-cadence-review", cadence_detail)

    if args.stage == "padfix":
        for key in ("avs_encode", "stack_project", "avs_padfix"):
            c.file(resolve(ep, key), key)
        if str(ld.profile.get("os") or "").casefold() in ("windows", "win32"):
            cadence_ok, cadence_detail = cadence_review_is_current(ep, d, ld)
            c.require(cadence_ok, "visual-cadence-review", cadence_detail)
        probe = ld.probe()
        c.file(probe / f"{ep.code}-pad-audit.tsv", "source-AVS-pad-audit")
        c.file(probe / f"{ep.code}-pad-repair-frames.txt", "confirmed-PAD-repair-list")
        for key in ("map_pads", "rendered_isolated", "repaired", "cut_pads"):
            c.require(nonnull(d, "pads", key), f"pads.{key}", "record the audit result")
        pad_data = d.get("pads") or {}
        raw_cut_count = pad_data.get("cut_pads")
        raw_cut_frames = pad_data.get("cut_pad_frames") or []
        cut_count_is_int = type(raw_cut_count) is int and raw_cut_count >= 0
        cut_frames_are_ints = isinstance(raw_cut_frames, list) and all(
            type(frame) is int and frame >= 0 for frame in raw_cut_frames)
        cut_count = raw_cut_count if cut_count_is_int else -1
        cut_frames = raw_cut_frames if cut_frames_are_ints else []
        c.require(cut_count_is_int and cut_frames_are_ints and
                  len(cut_frames) == cut_count and
                  len(set(cut_frames)) == len(cut_frames), "pads.cut_pad_frames",
                  "record the exact, unique output-frame list for every classified cut PAD")
        if cut_frames:
            try:
                repair_frames = {int(value) for value in
                                 (ld.probe() / f"{ep.code}-pad-repair-frames.txt")
                                 .read_text(encoding="utf-8").split()}
            except (OSError, ValueError):
                repair_frames = set()
            c.require(set(cut_frames).issubset(repair_frames), "pads.cut_pad_frames-confirmed",
                      "every cut PAD must also be in the confirmed PAD repair list")

    if args.stage == "deliverable":
        key = d.get("deliverable")
        c.require(bool(key), "deliverable-key", "set deliverable to a files.* key")
        c.file(resolve(ep, key) if key else None, "deliverable")
        source_key = d.get("ld_audio_source")
        c.file(resolve(ep, source_key) if source_key else None, "ld-audio-source-for-remux")
        tracks = d.get("deliverable_tracks") or []
        c.require("ld" in tracks, "default-audio", "LD stereo must be declared and default")

    for status, name, detail in c.rows:
        print(f"[{status}] {name}: {detail}")
    passes = sum(s == "PASS" for s, _, _ in c.rows)
    warnings = sum(s == "WARN" for s, _, _ in c.rows)
    failures = sum(s == "FAIL" for s, _, _ in c.rows)
    print(f"{ep.code} stage={args.stage}: {passes} pass, {warnings} warn, {failures} fail")
    return 1 if c.failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PipelineError, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
