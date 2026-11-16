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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

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
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resolve(ep, key: str) -> Path | None:
    try:
        return ep.path(key)
    except PipelineError:
        return None


def nonnull(data: dict, section: str, key: str) -> bool:
    return isinstance(data.get(section), dict) and data[section].get(key) is not None


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
        c.require(nonnull(d, "video", "head_trim") and nonnull(d, "video", "tail_trim"), "video-trims", "measured video.head_trim and video.tail_trim required")
        for key in ("tfm_period5_pct", "tfm_unmatched", "inserts"):
            c.require(nonnull(d, "video", key), f"video.{key}", "record measured cadence analysis")

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
        c.file(resolve(ep, "ld_native"), "ld-native-WAV")
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

    if args.stage == "padfix":
        for key in ("avs_encode", "stack_project", "avs_padfix"):
            c.file(resolve(ep, key), key)
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
