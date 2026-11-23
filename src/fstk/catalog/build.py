"""Refresh the Archive.org catalog snapshot (port of v38 ``scripts/build_archive_catalog.py``).

Metadata only: the account's upload listing and each item's ``/metadata/<id>/files``. No media
bytes are downloaded. A snapshot is navigation, not proof the files are still there; every real
transfer still verifies live size/MD5.

Owner decision 2026-11-21: check after every 30 days. ``is_stale`` compares the snapshot's
``source.generated_at_utc`` with now. A refresh writes a **new** dated file under
``<data_root>/catalog/`` and never overwrites. ``current_path`` picks the newest valid one, and
falls back to the snapshot shipped with fstk.
"""

from __future__ import annotations

import gzip
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .archive import (
    ACCOUNT_IDENTIFIER,
    ACCOUNT_URL,
    CATALOG_SCHEMA,
    SERIES_ALIASES,
    build_file_tree,
    classify_upload,
    item_episode_groups,
)

MAX_AGE = timedelta(days=30)
PAGE_SIZE = 100
MAX_WORKERS = 6
MAX_RETRIES = 4
USER_AGENT = "FromStunToKill-Archive-Catalog/1.0 (metadata-only)"
FILE_FIELDS = ("name", "size", "md5", "format", "source", "mtime", "sha1", "crc32")
DOWNLOAD_EXT_RE = re.compile(r"(?i)\.(?:tbc(?:\.json)?|efm(?:\.7z)?)$")
SECTION_DEFS = (
    ("ds9-episode-copies", "DS9 LaserDisc Copies", "episode-copy", "DS9"),
    ("voy-episode-copies", "VOY LaserDisc Copies", "episode-copy", "VOY"),
    ("ds9-season-stacks", "DS9 LaserDisc Stacks", "season-stack", "DS9"),
    ("voy-season-stacks", "VOY LaserDisc Stacks", "season-stack", "VOY"),
    ("tng-extra", "TNG Extra LaserDisc Copies", "multi-episode-extra", "TNG"),
)
SNAPSHOT_GLOB = "section_31_tree-*.json.gz"

Fetch = Callable[[str], dict]


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"Archive metadata request failed after {MAX_RETRIES} attempts: {url}: {last}")


def fetch_account_uploads(fetch: Fetch) -> tuple[list[dict], int]:
    hits_all: list[dict] = []
    total: int | None = None
    page = 1
    while total is None or len(hits_all) < total:
        query = urlencode(
            {
                "page_type": "account_details",
                "page_target": ACCOUNT_IDENTIFIER,
                "page_elements": json.dumps(["uploads"], separators=(",", ":")),
                "hits_per_page": str(PAGE_SIZE),
                "page": str(page),
                "sort": "publicdate:desc",
                "aggregations": "false",
            }
        )
        resp = fetch(f"https://archive.org/services/search/beta/page_production/?{query}")
        try:
            hits = resp["response"]["body"]["page_elements"]["uploads"]["hits"]
            page_total = int(hits["total"])
            fields = [row["fields"] for row in hits["hits"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"unexpected Archive account-upload response on page {page}") from exc
        if total is None:
            total = page_total
        elif page_total != total:
            raise RuntimeError(f"Archive upload count changed while paging ({total} -> {page_total})")
        if not fields:
            break
        hits_all.extend(fields)
        page += 1
    if total is None or len(hits_all) != total:
        raise RuntimeError(f"Archive account listing was incomplete: got {len(hits_all)} of {total}")
    ids = [row.get("identifier") for row in hits_all]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("Archive account listing contains missing or duplicate identifiers")
    return hits_all, total


def fetch_item_files(identifier: str, fetch: Fetch) -> list[dict]:
    result = fetch(f"https://archive.org/metadata/{quote(identifier, safe='')}/files").get("result")
    if not isinstance(result, list):
        raise RuntimeError(f"Archive file list missing for item {identifier}")
    rows = []
    for entry in result:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"Archive item {identifier} has a file entry without a name")
        row = {k: entry[k] for k in FILE_FIELDS if k in entry}
        row["selected_by_download_archive"] = bool(DOWNLOAD_EXT_RE.search(name))
        rows.append(row)
    if len({r["name"] for r in rows}) != len(rows):
        raise RuntimeError(f"Archive item {identifier} returned duplicate filenames")
    return sorted(rows, key=lambda r: r["name"].casefold())


def _capture_labels(files: list[dict]) -> list[str]:
    labels = set()
    for row in files:
        name = str(row.get("name", ""))
        if name.casefold().endswith(".tbc"):
            labels.add(name.rsplit("/", 1)[-1][:-4].rsplit("-", 1)[-1])
    return sorted(labels, key=str.casefold)


def make_item(upload: dict, files: list[dict]) -> dict:
    identifier = str(upload["identifier"])
    cls = classify_upload(identifier)
    tbc = {r["name"] for r in files if r["name"].casefold().endswith(".tbc")}
    names = {r["name"] for r in files}
    missing_json = sorted(n + ".json" for n in tbc if n + ".json" not in names)
    selected = [r for r in files if r["selected_by_download_archive"]]
    labels = _capture_labels(files)
    raw_efm = sum(1 for r in selected if str(r["name"]).casefold().endswith((".efm", ".efm.7z")))
    warnings = []
    if cls["kind"] == "episode-copy":
        if len(labels) < 2:
            warnings.append(f"{len(labels)} capture label(s); the weighted-stack workflow needs at least two")
        if missing_json:
            warnings.append("one or more TBC files lack paired JSON sidecars")
        if raw_efm != len(tbc):
            warnings.append(
                f"raw EFM count ({raw_efm}) differs from TBC count ({len(tbc)}); "
                "check the audio-stage prerequisites"
            )
    subj = upload.get("subject")
    item = {
        "identifier": identifier,
        "title": upload.get("title"),
        "url": f"https://archive.org/details/{identifier}",
        "metadata_url": f"https://archive.org/metadata/{identifier}",
        "category": cls["kind"],
        "series": cls.get("series"),
        "season": cls.get("season"),
        "episode": cls.get("episode"),
        "stack_part": cls.get("part"),
        "stack_parts": cls.get("parts"),
        "mediatype": upload.get("mediatype"),
        "subjects": sorted(subj) if isinstance(subj, list) else ([subj] if subj else []),
        "publicdate": upload.get("publicdate"),
        "item_size_bytes": int(upload["item_size"]) if str(upload.get("item_size", "")).isdigit() else None,
        "archive_files_count": (
            int(upload["files_count"]) if str(upload.get("files_count", "")).isdigit() else len(files)
        ),
        "capture_labels": labels,
        "tbc_file_count": len(tbc),
        "workflow_warnings": warnings,
        "download_inventory": {
            "selected_file_count": len(selected),
            "selected_bytes": sum(int(r["size"]) for r in selected if str(r.get("size", "")).isdigit()),
            "missing_tbc_json_sidecars": missing_json,
            "raw_efm_count": raw_efm,
        },
        "files": files,
        "file_tree": build_file_tree(files),
    }
    if item["category"] == "multi-episode-extra":
        item["episode_groups"] = item_episode_groups(item)
    return item


def build_catalog(fetch: Fetch = fetch_json, *, workers: int = MAX_WORKERS) -> dict:
    uploads, total = fetch_account_uploads(fetch)
    files_by_id: dict[str, list[dict]] = {}
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_item_files, str(r["identifier"]), fetch): str(r["identifier"]) for r in uploads
        }
        for fut in as_completed(futures):
            ident = futures[fut]
            try:
                files_by_id[ident] = fut.result()
            except Exception as exc:
                errors.append(f"{ident}: {exc}")
    if errors:
        raise RuntimeError("could not enumerate every Archive item:\n" + "\n".join(sorted(errors)))
    by_id = {str(r["identifier"]): r for r in uploads}
    items = [make_item(by_id[i], files_by_id[i]) for i in sorted(files_by_id)]
    sections, used = [], set()
    for key, label, kind, series in SECTION_DEFS:
        sel = sorted(
            (it for it in items if it["category"] == kind and it["series"] == series),
            key=lambda it: (
                it.get("season") or 0,
                it.get("episode") or 0,
                it.get("stack_part") or 0,
                it["identifier"],
            ),
        )
        used.update(it["identifier"] for it in sel)
        sections.append({"key": key, "label": label, "items": sel})
    other = [it for it in items if it["identifier"] not in used]
    if other:
        sections.append({"key": "other-uploads", "label": "Other uploads", "items": other})
    coverage: dict = {}
    for code in ("DS9", "VOY"):
        seasons: dict[str, list[int]] = {}
        for it in items:
            if it["category"] == "episode-copy" and it["series"] == code:
                seasons.setdefault(str(it["season"]), []).append(it["episode"])
        coverage[code] = {
            "episode_copy_count": sum(len(v) for v in seasons.values()),
            "seasons": {s: sorted(v, key=int) for s, v in sorted(seasons.items(), key=lambda p: int(p[0]))},
        }
    extra = next((it for it in items if it["category"] == "multi-episode-extra"), None)
    coverage["TNG"] = {
        "episode_copy_count": 0,
        "seasons_with_standalone_copy_items": {},
        "multi_episode_extra": extra["identifier"] if extra else None,
        "partial_episode_codes_in_extra": [
            f"S{g['season']:02d}E{g['episode']:02d}" for g in (extra or {}).get("episode_groups", [])
        ],
    }
    return {
        "schema": CATALOG_SCHEMA,
        "source": {
            "account_url": ACCOUNT_URL,
            "account_identifier": ACCOUNT_IDENTIFIER,
            "enumerated_via": "public Archive.org account uploads page and per-item metadata files API",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "upload_count": total,
            "media_bytes_downloaded": 0,
        },
        "series_aliases": {c: list(a) for c, a in SERIES_ALIASES.items()},
        "coverage": coverage,
        "children": sections,
    }


# ------------------------------------------------------------------------------ snapshots
def generated_at(catalog: dict) -> datetime | None:
    raw = (catalog.get("source") or {}).get("generated_at_utc")
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_stale(catalog: dict, *, now: datetime | None = None, max_age: timedelta = MAX_AGE) -> bool:
    when = generated_at(catalog)
    return when is None or (now or datetime.now(UTC)) - when > max_age


def snapshot_dir(data_root: Path) -> Path:
    return Path(data_root) / "catalog"


def _snapshot_key(path: Path) -> tuple[str, int]:
    m = re.fullmatch(r"section_31_tree-(\d{8}T\d{6}Z)(?:-(\d+))?\.json\.gz", path.name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def current_path(data_root: Path | None, fallback: Path) -> Path:
    """Newest refreshed snapshot under ``<data_root>/catalog`` if any, else the packaged one."""
    if data_root:
        found = sorted(snapshot_dir(data_root).glob(SNAPSHOT_GLOB), key=_snapshot_key)
        if found:
            return found[-1]
    return fallback


def write_snapshot(catalog: dict, data_root: Path) -> Path:
    """A new, dated, gzip snapshot; never overwrites an existing one."""
    if catalog.get("schema") != CATALOG_SCHEMA or not catalog.get("children"):
        raise RuntimeError("refusing to save an empty or foreign catalog")
    folder = snapshot_dir(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (generated_at(catalog) or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    out = folder / f"section_31_tree-{stamp}.json.gz"
    n = 1
    while out.exists():
        n += 1
        out = folder / f"section_31_tree-{stamp}-{n}.json.gz"
    tmp = out.with_name(out.name + ".tmp")
    with gzip.open(tmp, "wb") as fh:
        fh.write((json.dumps(catalog, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    tmp.rename(out)
    return out


def refresh(data_root: Path, *, fetch: Fetch = fetch_json) -> tuple[Path, dict]:
    catalog = build_catalog(fetch)
    return write_snapshot(catalog, data_root), catalog


def resolve_path(profile) -> Path:
    """Snapshot to use for this profile: explicit ``catalog_path`` > newest refreshed > packaged."""
    from .index import default_catalog_path

    if getattr(profile, "catalog_path", ""):
        return Path(profile.catalog_path)
    return current_path(getattr(profile, "data_root", None), default_catalog_path())


def status(profile, *, now: datetime | None = None) -> dict:
    from .archive import load_catalog

    path = resolve_path(profile)
    catalog = load_catalog(path)
    when = generated_at(catalog)
    days = int((getattr(profile, "ui", None) or {}).get("catalog_refresh_days") or MAX_AGE.days)
    return {
        "path": str(path),
        "generated_at_utc": when.isoformat() if when else None,
        "stale": is_stale(catalog, now=now, max_age=timedelta(days=days)),
        "explicit": bool(getattr(profile, "catalog_path", "")),
        "max_age_days": days,
    }
