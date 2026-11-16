#!/usr/bin/env python3
"""Initialize an episode manifest from the three job inputs.

This records identity, verified download inventory, an optional DVD reference,
and planned profile-root file locations. It deliberately leaves measurements null.

  python scripts\\init_episode.py --url <archive-url> --downloads <folder> --dvd <dvd.mkv>
  python scripts\\init_episode.py --url <archive-url> --downloads <folder> --no-dvd

It refuses to overwrite an existing manifest. It currently supports one-part
episodes only; E01/E02 Emissary needs its existing two-part manifest/workflow.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import (BUNDLE, Ld, PipelineError, assert_writable_path,
                     episode_manifest_path, shared_instance_root)  # noqa: E402


def archive_id(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ("archive.org", "www.archive.org"):
        raise PipelineError("Archive URL must use https://archive.org/details/<item-id> or /download/<item-id>")
    parts = [unquote(x) for x in u.path.strip("/").split("/") if x]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        raise PipelineError("Archive URL must identify an item under /details/ or /download/")
    item = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", item):
        raise PipelineError(f"unexpected Archive identifier: {item!r}")
    return item


def title_from_dvd(path: Path, code: str, ffprobe: str) -> str:
    m = re.search(re.escape(code) + r"\s*[-_]\s*(.+)$", path.stem, re.IGNORECASE)
    if m:
        return m.group(1).strip(" -_")
    p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format_tags=title",
                        "-of", "json", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode == 0:
        try:
            title = (json.loads(p.stdout).get("format", {}).get("tags") or {}).get("title") or ""
            m = re.search(re.escape(code) + r"\s*[-_]\s*(.+)$", title, re.IGNORECASE)
            if m:
                return m.group(1).strip(" -_")
            return title.strip()
        except (json.JSONDecodeError, AttributeError):
            pass
    return ""


def title_from_capture(captures: list[dict], code: str) -> str:
    """Read the episode title from a verified capture filename when no DVD exists."""
    for capture in captures:
        name = Path(str(capture.get("tbc_file", ""))).stem
        label = str(capture.get("label", ""))
        if not name or not label:
            continue
        match = re.search(
            re.escape(code) + r"\s*[-_]\s*(.+?)\s*[-_]\s*" + re.escape(label) + r"$",
            name, re.IGNORECASE)
        if match:
            return match.group(1).strip(" -_")
    return ""


def verified_instance_cut_pad_images(folder: Path, code: str) -> list[Path]:
    """Only offer instance-staged stills whose V7 provenance sidecar matches the PNG."""
    out = []
    for png in sorted(folder.glob("DVD-*.png")) if folder.is_dir() else []:
        match = re.fullmatch(r"DVD-(\d{2})-(\d{2})-(\d{2})\.(\d{3})-f(\d{8})\.png",
                             png.name, re.IGNORECASE)
        sidecar = png.with_suffix(".provenance.json")
        if not match or not sidecar.is_file():
            continue
        try:
            provenance = json.loads(sidecar.read_text(encoding="utf-8"))
            if (str(provenance.get("episode", "")).casefold() != code.casefold() or
                    provenance.get("reviewed_episode_scene_and_pad_mapping") is not True or
                    not isinstance(provenance.get("output_pad_frame"), int) or
                    provenance.get("png_sha256") != hashlib.sha256(png.read_bytes()).hexdigest() or
                    provenance.get("dvd_timestamp") !=
                    f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)}" or
                    not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_dvd_sha256", "")))):
                continue
        except (OSError, json.JSONDecodeError):
            continue
        out.append(png)
    return out


def episode_files(code: str, title: str = "", script_ext: str = ".avs") -> dict:
    low = code.lower()
    label = f"{code}-{title}" if title else code
    if script_ext not in (".avs", ".vpy"):
        raise PipelineError(f"unsupported script extension for this platform: {script_ext}")
    return {
        "stack_project": {"root": "stack", "name": f"stack-full-{low}.orcprj"},
        "stack_master": {"root": "prores", "name": f"{code}-stack-master.mov"},
        "tfm_log": {"root": "probe", "name": f"full-tfm-log-{low}.txt"},
        "motion": {"root": "probe", "name": f"motion-{low}.txt"},
        "cadence_summary": {"root": "probe", "name": f"cadence-summary-{low}.txt"},
        "di_map": {"root": "probe", "name": f"di-map-{low}.txt"},
        "avs_log": {"root": "avs", "name": f"{code}-log{script_ext}"},
        "avs_trim": {"root": "avs", "name": f"{code}-trim{script_ext}"},
        "avs_encode": {"root": "avs", "name": f"{code}-encode{script_ext}"},
        # The one-pass final encode writes padfixed_video. encode_full and
        # encode_validate remain only in older hand-maintained episode records.
        "avs_padfix": {"root": "avs", "name": f"{code}-padfix{script_ext}"},
        "padfixed_video": {"root": "out", "name": f"{code}-23.976-padfixed-video.mkv"},
        "padfixed": {"root": "out", "name": f"{code}-23.976-padfixed.mkv"},
        "pre_upscale": {"root": "out", "name": f"{code}-23.976-pre-upscale.mkv"},
        "dvd51_source": {"root": "audio", "name": f"{label}-dvd-51-original.ac3"},
        "ld_synced": {"root": "audio", "name": f"{code}-LD20-synced.flac"},
        "srt_source": {"root": "subs", "name": f"{code}-dvd-eng-original.srt"},
        # ld_native / ld_corrected are deliberately absent: their names embed the
        # audio capture label and the measured correction rate. Record them in the
        # manifest at stage 5 once both are measured, never at initialization.

        "retime_graph": {"root": "audio", "name": f"dvd-51-retimed-{low}.filter"},
        "retimed_51": {"root": "audio", "name": f"dvd-51-retimed-{low}.flac"},
        "retimed_srt": {"root": "subs", "name": f"{code}-dvd-eng-retimed.srt"},
        "retime_map": {"root": "probe", "name": f"dvd-retime-map-{low}.tsv"},
        "master_descriptors": {"root": "probe", "name": f"desc-master-{low}.npy"},
        "dvd_descriptors": {"root": "probe", "name": f"desc-dvd-{low}.npy"},
        "align_npy": {"root": "probe", "name": f"dvd-align-{low}.npy"},
        "alignment_report": {"root": "probe", "name": f"dvd-align-report-{low}.tsv"},
        "break_edge_report": {"root": "probe", "name": f"break-edge-measurements-{low}.tsv"},
        "audio_rate_report": {"root": "probe", "name": f"audio-rate-diagnostic-{low}.tsv"},
        "audio_timing_report": {"root": "probe", "name": f"{code}-dvd-free-audio-timing.json"},
        "edges": {"root": "probe", "name": f"adjudicated-edges-{low}.tsv"},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="verified Archive /details/<item-id> URL")
    ap.add_argument("--downloads", required=True, help="destination used by download_archive.ps1")
    dvd_group = ap.add_mutually_exclusive_group(required=True)
    dvd_group.add_argument("--dvd", help="absolute path to the verified matching DVD MKV")
    dvd_group.add_argument("--no-dvd", action="store_true",
                           help="record the user's explicit choice to proceed without a DVD reference")
    ap.add_argument("--cut-pad-images", choices=("use", "skip"),
                    help="when --no-dvd and shared episode cut-pad PNGs exist, record the user's choice")
    ap.add_argument("--manifest-out", help="alternate manifest path for testing")
    args = ap.parse_args()

    item = archive_id(args.url)
    m = re.search(r"S\d{2}E\d{2}", item, re.IGNORECASE)
    if not m:
        raise PipelineError(f"could not derive an SxxExx code from Archive item id {item!r}")
    code = m.group(0).upper()
    if code in ("S01E01", "S01E02"):
        raise PipelineError("Emissary is two-part; use the existing S01E01 manifest and dedicated Part I/II workflow")

    ld = Ld()
    if (ld.profile.get("shared") or {}).get("enabled") is True:
        shared_instance_root(ld.profile, ld.profile_path, code)
    download_dir = Path(args.downloads).resolve()
    if not download_dir.is_dir():
        raise PipelineError(f"download destination is not a directory: {download_dir}")
    dvd = Path(args.dvd).resolve() if args.dvd else None
    if dvd is not None and not dvd.is_file():
        raise PipelineError(f"DVD file does not exist: {dvd}")
    if not args.no_dvd and args.cut_pad_images is not None:
        raise PipelineError("--cut-pad-images is only valid with --no-dvd")
    root_overrides = {}
    roots = ld.profile.get("roots") or {}
    dl_root = roots.get("downloads_root")
    dl_template = roots.get("downloads_template") or "DS9 {episode} LaserDisc"
    if dl_root:
        try:
            expected = (Path(dl_root) / dl_template.format(episode=code)).resolve()
            if download_dir != expected:
                root_overrides["downloads"] = str(download_dir)
        except (ValueError, KeyError):
            root_overrides["downloads"] = str(download_dir)
    else:
        root_overrides["downloads"] = str(download_dir)
    shared_mode = (ld.profile.get("shared") or {}).get("enabled") is True
    if shared_mode and root_overrides:
        raise PipelineError(
            "shared mode keeps machine-local absolute paths out of the shared manifest; "
            "set local roots.downloads_root/downloads_template in machine.json so this folder resolves canonically")
    dl_manifest = download_dir / "download-manifest.csv"
    if not dl_manifest.is_file():
        raise PipelineError(f"verified download manifest missing: {dl_manifest}; run download_archive.ps1 first")
    with dl_manifest.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows or any(r.get("Status") != "verified" for r in rows):
        raise PipelineError(f"download-manifest.csv does not record all files verified for Archive item {item}")
    ids = {r.get("ArchiveId") for r in rows if r.get("ArchiveId")}
    if ids and ids != {item}:
        raise PipelineError(f"download manifest ArchiveId values {sorted(ids)} do not match supplied item {item}")
    # Older episode download manifests did not store ArchiveId. Prove their file
    # inventory against this URL's current Archive metadata instead of trusting the note.
    try:
        with urlopen(f"https://archive.org/metadata/{item}", timeout=60) as response:
            metadata = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise PipelineError(f"could not verify download manifest against Archive metadata for {item}: {e}") from e
    remote = {str(x.get("name")): x for x in metadata.get("files", [])}
    for row in rows:
        entry = remote.get(row.get("Name", ""))
        if not entry or str(entry.get("md5", "")).lower() != str(row.get("MD5", "")).lower():
            raise PipelineError(f"download manifest file is not verified against Archive item {item}: {row.get('Name')}")
        if str(entry.get("size", "")) != str(row.get("Bytes", "")):
            raise PipelineError(f"download manifest size differs from Archive metadata: {row.get('Name')}")
    by_name = {r["Name"]: r for r in rows}

    tbc_rows = [r for r in rows if r["Name"].lower().endswith(".tbc")]
    if not tbc_rows:
        raise PipelineError("download manifest contains no TBC captures")
    if len(tbc_rows) > 6:
        raise PipelineError(f"found {len(tbc_rows)} TBC captures; bundled Orc template supports at most six, so inspect and extend it explicitly")
    captures = []
    for row in sorted(tbc_rows, key=lambda x: x["Name"].casefold()):
        name = row["Name"]
        label = Path(name).stem.rsplit("-", 1)[-1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", label):
            raise PipelineError(f"cannot derive a capture label from {name!r}; record it explicitly")
        json_name = name + ".json"
        json_row = by_name.get(json_name)
        if not json_row:
            raise PipelineError(f"paired TBC JSON absent from verified download manifest: {json_name}")
        efm_names = [n for n in by_name if n.lower().startswith(Path(name).stem.lower()) and n.lower().endswith((".efm", ".efm.7z"))]
        if len(efm_names) > 1:
            raise PipelineError(f"multiple raw EFM candidates for capture {label}: {efm_names}")
        efm_row = by_name.get(efm_names[0]) if efm_names else None
        for required_name in (name, json_name, *(efm_names or [])):
            if not (download_dir / required_name).is_file():
                raise PipelineError(f"manifested source file missing: {download_dir / required_name}")
        captures.append({
            "label": label,
            "tbc_file": name, "tbc_bytes": int(row["Bytes"]), "tbc_md5": row["MD5"].lower(),
            "json_file": json_name, "json_bytes": int(json_row["Bytes"]), "json_md5": json_row["MD5"].lower(),
            "efm_file": efm_names[0] if efm_names else "",
            "efm_bytes": int(efm_row["Bytes"]) if efm_row else None,
            "efm_md5": efm_row["MD5"].lower() if efm_row else "",
        })
    labels = [c["label"].casefold() for c in captures]
    if len(labels) != len(set(labels)):
        raise PipelineError(f"capture labels are not unique: {labels}")

    dvd_spec = None
    if dvd is not None:
        dvd_root = (ld.profile.get("roots") or {}).get("dvd_root")
        dvd_spec = {"path": str(dvd)}
        if dvd_root:
            try:
                rel = dvd.relative_to(Path(dvd_root).resolve())
                dvd_spec = {"root": "dvd_root", "name": rel.as_posix()}
            except ValueError:
                pass
        if shared_mode and "path" in dvd_spec:
            raise PipelineError(
                "shared mode keeps machine-local absolute paths out of the shared manifest; "
                "add this DVD library to the local profile roots.dvd_root and retry")
        dvd_info = subprocess.run([ld.ffprobe(), "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,channels:format=duration:format_tags=title", "-of", "json", str(dvd)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if dvd_info.returncode:
            raise PipelineError(f"ffprobe failed on supplied DVD: {dvd_info.stderr[-800:]}")
        dvd_data = json.loads(dvd_info.stdout or "{}")
        if not any(s.get("codec_type") == "video" for s in dvd_data.get("streams", [])):
            raise PipelineError("supplied DVD has no video stream")
        title = title_from_dvd(dvd, code, ld.ffprobe())
    else:
        title = title_from_capture(captures, code)
        if not title:
            archive_title = (metadata.get("metadata") or {}).get("title") or ""
            title = str(archive_title).strip()
        if not title:
            print("WARN: could not derive a human title from verified capture filenames or Archive metadata; "
                  "confirm it before reporting.", file=sys.stderr)

    cut_pad_dir = BUNDLE / "Cut Pad Images" / code
    cut_pad_images = sorted(cut_pad_dir.glob("DVD-*.png")) if cut_pad_dir.is_dir() else []
    instance_cut_dir = os.environ.get("LDP_CUT_PAD_IMAGES")
    if instance_cut_dir:
        instance_cut = Path(os.path.expandvars(os.path.expanduser(instance_cut_dir)))
        cut_pad_images.extend(verified_instance_cut_pad_images(instance_cut, code))
    shared = ld.profile.get("shared") or {}
    if shared.get("enabled") is True:
        devices = Path(shared["release_root"]) / "devices"
        for candidate_dir in devices.glob(f"*/episodes/{code}/cut-pad-images"):
            cut_pad_images.extend(verified_instance_cut_pad_images(candidate_dir, code))
    # Deduplicate identical paths while retaining deterministic order.
    cut_pad_images = sorted({p.resolve() for p in cut_pad_images})
    if args.no_dvd and cut_pad_images and args.cut_pad_images is None:
        raise PipelineError(
            f"shared cut-pad stills are available in {cut_pad_dir}; ask the user whether to use them, "
            "then pass --cut-pad-images use or --cut-pad-images skip")
    if args.no_dvd and args.cut_pad_images == "use" and not cut_pad_images:
        raise PipelineError(f"--cut-pad-images use was selected but no shared PNGs exist in {cut_pad_dir}")
    image_choice = (args.cut_pad_images if cut_pad_images else "not-available") if args.no_dvd else "not-applicable"
    if not title and dvd is not None:
        print("WARN: could not derive a human title from DVD tags/filename; confirm it and update title before reporting.", file=sys.stderr)

    default_manifest = episode_manifest_path(code, ld)
    out = Path(args.manifest_out).resolve() if args.manifest_out else default_manifest
    assert_writable_path(out, ld)
    if (ld.profile.get("shared") or {}).get("enabled") is True and out.parent != default_manifest.parent.resolve():
        raise PipelineError("shared mode manifest output must stay in this instance's manifest folder")
    if out.exists():
        raise PipelineError(f"refusing to overwrite existing manifest: {out}")
    template = json.loads((BUNDLE / "episodes" / "_template.json").read_text(encoding="utf-8"))
    template.update({
        "episode": code, "title": title, "pad_report_label": code,
        "record_status": "draft", "format": "one_part", "archive_item": item,
        "dvd_reference_available": dvd is not None,
        "no_dvd_proceed_approved": bool(args.no_dvd),
        "cut_pad_images_choice": image_choice,
        "captures": captures,
        "reference": "", "alignment_map": "",
        "files": ({"dvd": dvd_spec} if dvd_spec else {}) | episode_files(
            code, title, ".vpy" if (ld.profile.get("os") or "windows").casefold() == "darwin" else ".avs"),
        "deliverable": "pre_upscale", "deliverable_tracks": ["ld"],
        "mux_tracks": ["ld"], "ld_audio_source": "ld_synced",
        "_init_note": ("DVD/download locations use machine profile roots when possible and explicit paths only for outside roots. "
                       "Episode measurements are intentionally null; measure them before running stage scripts. "
                       "No-DVD mode is permitted only after the user explicitly chose --no-dvd; DVD-derived tracks are unavailable."),
    })
    if root_overrides:
        template["roots"] = root_overrides
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"initialized {out}")
    print(f"episode={code} title={title or '<confirm>'} archive={item}")
    print(f"dvd_reference_available={dvd is not None}; cut_pad_images_choice={image_choice}")
    print(f"captures={','.join(c['label'] for c in captures)} reference=UNMEASURED")
    print("No stack, cadence, audio, trim, alignment, or retime values were guessed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
