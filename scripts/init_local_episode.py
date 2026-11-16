#!/usr/bin/env python3
"""Initialize an episode manifest from a local LaserDisc rip without Archive provenance."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from init_episode import episode_files, verified_instance_cut_pad_images  # noqa: E402
from ldcommon import (BUNDLE, Ld, PipelineError, assert_writable_path,
                      episode_manifest_path, shared_instance_root)  # noqa: E402
from local_source import LocalSourceError, build_local_source_report, inspect_local_source  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--local-source", required=True,
                        help="existing folder or a TBC/JSON/EFM file inside the source folder")
    parser.add_argument("--confirmed-title", required=True)
    parser.add_argument("--fingerprint-policy", choices=("balanced", "full"), default="balanced")
    parser.add_argument("--manifest-out", help="alternate new episode manifest path")
    dvd_group = parser.add_mutually_exclusive_group(required=True)
    dvd_group.add_argument("--dvd", help="absolute path to the verified matching DVD MKV")
    dvd_group.add_argument("--no-dvd", action="store_true")
    parser.add_argument("--cut-pad-images", choices=("use", "skip"))
    args = parser.parse_args()

    try:
        scan = inspect_local_source(Path(args.local_source))
        episode = args.episode.strip().upper()
        if scan["episode"] != episode:
            raise PipelineError(f"local capture names identify {scan['episode']}, not requested {episode}")
        if episode in ("S01E01", "S01E02"):
            raise PipelineError("Emissary is two-part; use its existing S01E01 manifest and dedicated workflow")

        ld = Ld()
        shared_mode = (ld.profile.get("shared") or {}).get("enabled") is True
        if shared_mode:
            shared_instance_root(ld.profile, ld.profile_path, episode)
        source_dir = Path(scan["source_dir"]).resolve()
        roots = ld.profile.get("roots") or {}
        root_overrides = {}
        download_root = roots.get("downloads_root")
        download_template = roots.get("downloads_template") or "DS9 {episode} LaserDisc"
        if download_root:
            try:
                expected = (Path(os.path.expandvars(os.path.expanduser(str(download_root)))) /
                            download_template.format(episode=episode)).resolve()
            except (ValueError, KeyError) as exc:
                raise PipelineError(f"invalid profile downloads_template: {exc}") from exc
            if source_dir != expected:
                root_overrides["downloads"] = str(source_dir)
        else:
            root_overrides["downloads"] = str(source_dir)
        if shared_mode and root_overrides:
            raise PipelineError(
                "shared-profile local source must match roots.downloads_root/downloads_template; "
                "configure the machine profile for this source folder")

        dvd = Path(args.dvd).expanduser().resolve() if args.dvd else None
        if dvd is not None and not dvd.is_file():
            raise PipelineError(f"DVD file does not exist: {dvd}")
        if dvd is not None and args.cut_pad_images is not None:
            raise PipelineError("--cut-pad-images is valid only with --no-dvd")
        if not args.no_dvd and dvd is None:
            raise PipelineError("choose --dvd or explicitly select --no-dvd")
        dvd_spec = None
        if dvd is not None:
            dvd_root = roots.get("dvd_root")
            dvd_spec = {"path": str(dvd)}
            if dvd_root:
                try:
                    dvd_spec = {"root": "dvd_root", "name": dvd.relative_to(Path(dvd_root).resolve()).as_posix()}
                except ValueError:
                    pass
            if shared_mode and "path" in dvd_spec:
                raise PipelineError("shared profile requires the DVD under configured roots.dvd_root")

        cut_pad_dir = BUNDLE / "Cut Pad Images" / episode
        cut_images = sorted(cut_pad_dir.glob("DVD-*.png")) if cut_pad_dir.is_dir() else []
        if os.environ.get("LDP_CUT_PAD_IMAGES"):
            cut_images.extend(verified_instance_cut_pad_images(Path(os.environ["LDP_CUT_PAD_IMAGES"]), episode))
        if shared_mode:
            devices = Path((ld.profile.get("shared") or {}).get("release_root") or BUNDLE) / "devices"
            for candidate in devices.glob(f"*/episodes/{episode}/cut-pad-images"):
                cut_images.extend(verified_instance_cut_pad_images(candidate, episode))
        cut_images = sorted({path.resolve() for path in cut_images})
        if args.no_dvd and cut_images and args.cut_pad_images is None:
            raise PipelineError("verified shared cut-PAD images exist; choose --cut-pad-images use or skip")
        if args.no_dvd and args.cut_pad_images == "use" and not cut_images:
            raise PipelineError(f"--cut-pad-images use selected but no verified stills exist in {cut_pad_dir}")
        image_choice = (args.cut_pad_images if cut_images else "not-available") if args.no_dvd else "not-applicable"

        inventory = build_local_source_report(scan, policy=args.fingerprint_policy)
        inventory_evidence = {key: value for key, value in inventory.items()
                              if key not in ("source_dir", "selected_path")}
        inventory_evidence["selected_name"] = Path(scan["selected_path"]).name
        captures = []
        for row in inventory["captures"]:
            files = row["files"]
            tbc, sidecar, efm = files["tbc"], files["json"], files.get("efm")
            captures.append({
                "label": row["label"],
                "tbc_file": tbc["name"], "tbc_bytes": tbc["size_bytes"], "tbc_md5": "",
                "json_file": sidecar["name"], "json_bytes": sidecar["size_bytes"], "json_md5": "",
                "efm_file": efm["name"] if efm else "",
                "efm_bytes": efm["size_bytes"] if efm else None,
                "efm_md5": "",
            })

        manifest_default = episode_manifest_path(episode, ld)
        manifest_out = Path(args.manifest_out).expanduser().resolve() if args.manifest_out else manifest_default
        assert_writable_path(manifest_out, ld)
        if shared_mode and manifest_out.parent != manifest_default.parent.resolve():
            raise PipelineError("shared-mode manifest output must stay in the current episode workspace")
        if manifest_out.exists():
            raise PipelineError(f"refusing to overwrite existing manifest: {manifest_out}")

        template = json.loads((BUNDLE / "episodes" / "_template.json").read_text(encoding="utf-8"))
        title = args.confirmed_title.strip()
        archive_id_hint = str(scan.get("archive_id_hint", ""))
        template.update({
            "episode": episode, "title": title, "pad_report_label": episode,
            "record_status": "draft", "format": "one_part",
            "archive_item": archive_id_hint,
            "archive_item_verified": False,
            "source_provenance": {
                "mode": "local-inventory", "archive_verified": False,
                "verification_method": "local source fingerprint only; Archive origin/authenticity not established",
                "fingerprint_policy": args.fingerprint_policy,
                "inventory_created_utc": inventory["created_utc"],
                "inventory": inventory_evidence,
            },
            "dvd_reference_available": dvd is not None,
            "no_dvd_proceed_approved": bool(args.no_dvd),
            "cut_pad_images_choice": image_choice,
            "captures": captures,
            "reference": "", "alignment_map": "",
            "files": ({"dvd": dvd_spec} if dvd_spec else {}) | episode_files(
                episode, title, ".vpy" if (ld.profile.get("os") or "windows").casefold() == "darwin" else ".avs"),
            "deliverable": "pre_upscale", "deliverable_tracks": ["ld"],
            "mux_tracks": ["ld"], "ld_audio_source": "ld_synced",
            "_init_note": ("Local source inventory only; no Archive verification or authenticity claim. "
                           "Capture files were not modified. All episode measurements remain unmeasured."),
        })
        if root_overrides:
            template["roots"] = root_overrides
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        with manifest_out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(template, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        print(f"initialized local-inventory manifest {manifest_out}")
        print(f"episode={episode} title={title} captures={','.join(row['label'] for row in captures)}")
        print("PROVENANCE LIMIT: local fingerprints do not prove Archive origin or authenticity.")
        return 0
    except (LocalSourceError, PipelineError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"LOCAL INIT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
