#!/usr/bin/env python3
"""Build a metadata-only tree of Section 31's public Archive.org uploads.

This queries account upload pages and each item's ``/metadata/<id>/files`` endpoint.
It downloads no TBC/EFM/PCM/media bytes. The resulting JSON is an included navigation
snapshot, not proof that item files remain available; every real transfer must still pass
``download_archive.ps1 -ListOnly`` and its live size/MD5/space gates.

    python scripts/build_archive_catalog.py
    python scripts/build_archive_catalog.py --out archive_catalog/section_31_tree.json --force
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive_catalog import (ACCOUNT_IDENTIFIER, ACCOUNT_URL, CATALOG_SCHEMA, SERIES_ALIASES,
                             build_file_tree, classify_upload, item_episode_groups)

PAGE_SIZE = 100
MAX_WORKERS = 6
MAX_RETRIES = 4
USER_AGENT = "DS9-LD-Pipeline-Archive-Catalog/1.0 (metadata-only)"
FILE_FIELDS = ("name", "size", "md5", "format", "source", "mtime", "sha1", "crc32")
DOWNLOAD_EXT_RE = re.compile(r"(?i)\.(?:tbc(?:\.json)?|efm(?:\.7z)?)$")

SECTION_DEFS = (
    ("ds9-episode-copies", "DS9 LaserDisc Copies", "episode-copy", "DS9"),
    ("voy-episode-copies", "VOY LaserDisc Copies", "episode-copy", "VOY"),
    ("ds9-season-stacks", "DS9 LaserDisc Stacks", "season-stack", "DS9"),
    ("voy-season-stacks", "VOY LaserDisc Stacks", "season-stack", "VOY"),
    ("tng-extra", "TNG Extra LaserDisc Copies", "multi-episode-extra", "TNG"),
)


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/json"})
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Archive metadata request failed after {MAX_RETRIES} attempts: {url}: {last_error}")


def fetch_account_uploads() -> tuple[list[dict], int]:
    all_hits: list[dict] = []
    reported_total: int | None = None
    page = 1
    while reported_total is None or len(all_hits) < reported_total:
        query = urlencode({
            "page_type": "account_details",
            "page_target": ACCOUNT_IDENTIFIER,
            "page_elements": json.dumps(["uploads"], separators=(",", ":")),
            "hits_per_page": str(PAGE_SIZE),
            "page": str(page),
            "sort": "publicdate:desc",
            "aggregations": "false",
        })
        response = fetch_json(f"https://archive.org/services/search/beta/page_production/?{query}")
        try:
            hits = response["response"]["body"]["page_elements"]["uploads"]["hits"]
            total = int(hits["total"])
            rows = hits["hits"]
            fields = [row["fields"] for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Unexpected Archive account-upload response on page {page}") from exc
        if reported_total is None:
            reported_total = total
        elif total != reported_total:
            raise RuntimeError(f"Archive upload count changed while paging ({reported_total} -> {total})")
        if not fields:
            break
        all_hits.extend(fields)
        page += 1
    if reported_total is None or len(all_hits) != reported_total:
        raise RuntimeError(f"Archive account listing was incomplete: got {len(all_hits)} of {reported_total}")
    identifiers = [row.get("identifier") for row in all_hits]
    if any(not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
        raise RuntimeError("Archive account listing contains missing or duplicate identifiers")
    return all_hits, reported_total


def fetch_item_files(identifier: str) -> list[dict]:
    url = f"https://archive.org/metadata/{quote(identifier, safe='')}/files"
    response = fetch_json(url)
    result = response.get("result")
    if not isinstance(result, list):
        raise RuntimeError(f"Archive file list missing for item {identifier}")
    rows = []
    for entry in result:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"Archive item {identifier} has a file entry without a name")
        row = {key: entry[key] for key in FILE_FIELDS if key in entry}
        row["selected_by_download_archive"] = bool(DOWNLOAD_EXT_RE.search(name))
        rows.append(row)
    if len({row["name"] for row in rows}) != len(rows):
        raise RuntimeError(f"Archive item {identifier} returned duplicate filenames")
    return sorted(rows, key=lambda row: row["name"].casefold())


def capture_labels(files: list[dict]) -> list[str]:
    labels = set()
    for row in files:
        name = str(row.get("name", ""))
        if not name.casefold().endswith(".tbc"):
            continue
        stem = name.rsplit("/", 1)[-1][:-4]
        labels.add(stem.rsplit("-", 1)[-1])
    return sorted(labels, key=str.casefold)


def make_item(upload: dict, files: list[dict]) -> dict:
    identifier = str(upload["identifier"])
    classification = classify_upload(identifier)
    tbc_names = {row["name"] for row in files if row["name"].casefold().endswith(".tbc")}
    name_set = {row["name"] for row in files}
    missing_json = sorted(name + ".json" for name in tbc_names if name + ".json" not in name_set)
    selected = [row for row in files if row["selected_by_download_archive"]]
    labels = capture_labels(files)
    raw_efm_count = sum(1 for row in selected
                       if str(row["name"]).casefold().endswith((".efm", ".efm.7z")))
    workflow_warnings = []
    if classification["kind"] == "episode-copy":
        if not 2 <= len(labels) <= 6:
            workflow_warnings.append(
                f"{len(labels)} capture label(s); current weighted-stack workflow requires 2..6")
        if missing_json:
            workflow_warnings.append("one or more TBC files lack paired JSON sidecars")
        if raw_efm_count != len(tbc_names):
            workflow_warnings.append(
                f"raw EFM count ({raw_efm_count}) differs from TBC count ({len(tbc_names)}); "
                "check the audio-stage prerequisites")
    item = {
        "identifier": identifier,
        "title": upload.get("title"),
        "url": f"https://archive.org/details/{identifier}",
        "metadata_url": f"https://archive.org/metadata/{identifier}",
        "category": classification["kind"],
        "series": classification.get("series"),
        "season": classification.get("season"),
        "episode": classification.get("episode"),
        "stack_part": classification.get("part"),
        "stack_parts": classification.get("parts"),
        "mediatype": upload.get("mediatype"),
        "subjects": sorted(upload.get("subject", [])) if isinstance(upload.get("subject"), list)
                    else ([upload["subject"]] if upload.get("subject") else []),
        "publicdate": upload.get("publicdate"),
        "item_size_bytes": int(upload["item_size"]) if str(upload.get("item_size", "")).isdigit() else None,
        "archive_files_count": int(upload["files_count"]) if str(upload.get("files_count", "")).isdigit() else len(files),
        "capture_labels": labels,
        "tbc_file_count": len(tbc_names),
        "workflow_warnings": workflow_warnings,
        "download_inventory": {
            "selected_file_count": len(selected),
            "selected_bytes": sum(int(row["size"]) for row in selected
                                  if str(row.get("size", "")).isdigit()),
            "missing_tbc_json_sidecars": missing_json,
            "raw_efm_count": raw_efm_count,
        },
        "files": files,
        "file_tree": build_file_tree(files),
    }
    if item["category"] == "multi-episode-extra":
        item["episode_groups"] = item_episode_groups(item)
    return item


def build_catalog() -> dict:
    uploads, expected_total = fetch_account_uploads()
    files_by_id = {}
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_item_files, str(row["identifier"])): str(row["identifier"])
                   for row in uploads}
        for future in as_completed(futures):
            identifier = futures[future]
            try:
                files_by_id[identifier] = future.result()
            except Exception as exc:  # preserve identifier context; abort rather than emit a partial tree
                errors.append(f"{identifier}: {exc}")
    if errors:
        raise RuntimeError("Could not enumerate every Archive child item:\n" + "\n".join(sorted(errors)))

    upload_by_id = {str(row["identifier"]): row for row in uploads}
    items = [make_item(upload_by_id[identifier], files_by_id[identifier])
             for identifier in sorted(files_by_id)]
    sections = []
    used = set()
    for key, label, kind, series in SECTION_DEFS:
        selected = [item for item in items if item["category"] == kind and item["series"] == series]
        selected.sort(key=lambda item: (item.get("season") or 0,
                                        item.get("episode") or 0,
                                        item.get("stack_part") or 0,
                                        item["identifier"]))
        used.update(item["identifier"] for item in selected)
        sections.append({"key": key, "label": label, "items": selected})
    other = [item for item in items if item["identifier"] not in used]
    if other:
        sections.append({"key": "other-uploads", "label": "Other uploads", "items": other})

    coverage = {}
    for code in ("DS9", "VOY"):
        seasons: dict[str, list[int]] = {}
        for item in items:
            if item["category"] == "episode-copy" and item["series"] == code:
                seasons.setdefault(str(item["season"]), []).append(item["episode"])
        coverage[code] = {
            "episode_copy_count": sum(len(values) for values in seasons.values()),
            "seasons": {season: sorted(values, key=int) for season, values in sorted(seasons.items(), key=lambda pair: int(pair[0]))},
        }
    tng_extra = next((item for item in items if item["category"] == "multi-episode-extra"), None)
    coverage["TNG"] = {
        "episode_copy_count": 0,
        "seasons_with_standalone_copy_items": {},
        "multi_episode_extra": tng_extra["identifier"] if tng_extra else None,
        "partial_episode_codes_in_extra": [
            f"S{group['season']:02d}E{group['episode']:02d}"
            for group in (tng_extra or {}).get("episode_groups", [])
        ],
    }
    return {
        "schema": CATALOG_SCHEMA,
        "source": {
            "account_url": ACCOUNT_URL,
            "account_identifier": ACCOUNT_IDENTIFIER,
            "enumerated_via": "public Archive.org account uploads page and per-item metadata files API",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "upload_count": expected_total,
            "media_bytes_downloaded": 0,
        },
        "series_aliases": {
            code: list(aliases) for code, aliases in SERIES_ALIASES.items()
        },
        "coverage": coverage,
        "children": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(ROOT / "archive_catalog" / "section_31_tree.json"))
    parser.add_argument("--force", action="store_true", help="replace an existing catalog snapshot")
    args = parser.parse_args()
    output = Path(args.out).expanduser().resolve()
    if output.exists() and not args.force:
        raise RuntimeError(f"refusing to overwrite existing catalog: {output} (pass --force to refresh)")
    catalog = build_catalog()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    temp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if output.exists() and not args.force:
            raise RuntimeError(f"catalog appeared during build; refusing overwrite: {output}")
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    print(f"wrote metadata-only Archive tree: {output}")
    print(f"uploads={catalog['source']['upload_count']} items={sum(len(s['items']) for s in catalog['children'])}")
    print("media bytes downloaded=0")
    for code, summary in catalog["coverage"].items():
        print(f"{code} standalone episode-copy items={summary['episode_copy_count']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
