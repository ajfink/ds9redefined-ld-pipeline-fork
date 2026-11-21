# Ported verbatim from v38 scripts/archive_catalog.py (tools/port_v38.py). Do not hand-edit ported logic
# without a characterization test that states the intended behaviour change.
"""Shared parsing and resolution logic for the included Archive.org item tree."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

CATALOG_SCHEMA = "ld-pipeline-archive-tree-v1"
ACCOUNT_IDENTIFIER = "@section_31"
ACCOUNT_URL = "https://archive.org/details/@section_31"

SERIES_ALIASES = {
    "DS9": ("ds9", "deep space nine", "deep space 9"),
    "VOY": ("voy", "voyager"),
    "TNG": ("tng", "the next generation", "next generation"),
}

EPISODE_ITEM_RE = re.compile(
    r"^(?P<series>DS9|VOY)-S(?P<season>\d{2})E(?P<episode>\d{2})-LaserDisc-Copies$",
    re.IGNORECASE,
)
STACK_ITEM_RE = re.compile(
    r"^(?P<series>DS9|VOY)-S(?P<season>\d{2})-LaserDisc-(?P<part>\d+)-of-(?P<parts>\d+)$",
    re.IGNORECASE,
)
TNG_FILE_RE = re.compile(r"^TNG-S(?P<season>\d{2})E(?P<episode>\d{2})-(?P<rest>.+)$", re.IGNORECASE)
DIRECT_CODE_RE = re.compile(r"\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,2})\b", re.IGNORECASE)
SEASON_RE = re.compile(r"\bseason\s+(?P<season>\d{1,2})\b", re.IGNORECASE)
EPISODE_RE = re.compile(r"\b(?:episode|ep)\s*#?\s*(?P<episode>\d{1,2})\b", re.IGNORECASE)


def normalize_tokens(value: str) -> tuple[str, ...]:
    """Case/punctuation-insensitive tokens, preserving digits such as the 9 in DS9."""
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def load_catalog(path: Path) -> dict:
    """Load a catalog snapshot (``.json`` or gzip-compressed ``.json.gz``)."""
    try:
        raw = Path(path).read_bytes()
        if str(path).endswith(".gz"):
            import gzip

            raw = gzip.decompress(raw)
        catalog = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Archive catalog {path}: {exc}") from exc
    if not isinstance(catalog, dict):
        raise ValueError(f"Archive catalog root is not an object: {path}")
    return catalog


def _contains_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return any(haystack[index:index + len(needle)] == needle
               for index in range(len(haystack) - len(needle) + 1))


def identify_series(text: str) -> str | None:
    """Find one clarified franchise alias; a generic 'Star Trek' alone is not a show."""
    tokens = normalize_tokens(text)
    found = []
    for code, aliases in SERIES_ALIASES.items():
        if any(_contains_sequence(tokens, normalize_tokens(alias)) for alias in aliases):
            found.append(code)
    if len(found) > 1:
        raise ValueError(f"request names multiple series: {', '.join(found)}")
    return found[0] if found else None


def parse_episode_request(text: str) -> dict:
    """Parse a natural request such as 'Download Episode 18 of season 2 of DS9'."""
    direct = DIRECT_CODE_RE.search(text)
    if direct:
        season = int(direct.group("season"))
        episode = int(direct.group("episode"))
    else:
        season_match = SEASON_RE.search(text)
        episode_match = EPISODE_RE.search(text)
        if not season_match or not episode_match:
            raise ValueError("include a season and episode (for example: episode 18 of season 2)")
        season = int(season_match.group("season"))
        episode = int(episode_match.group("episode"))
    if season < 1 or episode < 1:
        raise ValueError("season and episode numbers must be positive")
    series = identify_series(text)
    if not series:
        raise ValueError("series not recognized; use DS9/Deep Space Nine, VOY/Voyager, or TNG/The Next Generation")
    return {"series": series, "season": season, "episode": episode}


def classify_upload(identifier: str) -> dict:
    match = EPISODE_ITEM_RE.fullmatch(identifier)
    if match:
        return {"kind": "episode-copy", "series": match.group("series").upper(),
                "season": int(match.group("season")), "episode": int(match.group("episode"))}
    match = STACK_ITEM_RE.fullmatch(identifier)
    if match:
        return {"kind": "season-stack", "series": match.group("series").upper(),
                "season": int(match.group("season")), "part": int(match.group("part")),
                "parts": int(match.group("parts"))}
    if identifier == "TNG-Extra-LaserDisc-Copies":
        return {"kind": "multi-episode-extra", "series": "TNG"}
    return {"kind": "other"}


def build_file_tree(files: list[dict]) -> dict:
    """Build an explicit nested directory tree from Archive's item-relative paths."""
    root = {"name": ".", "directories": {}, "files": []}
    for record in files:
        raw_name = str(record.get("name") or "")
        path = PurePosixPath(raw_name)
        if not raw_name or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe Archive metadata path: {raw_name!r}")
        node = root
        for component in path.parts[:-1]:
            node = node["directories"].setdefault(
                component, {"name": component, "directories": {}, "files": []})
        # The item record holds full file metadata once; the tree keeps only leaf names.
        node["files"].append(path.name)

    def finish(node: dict) -> dict:
        return {
            "name": node["name"],
            "directories": [finish(node["directories"][name]) for name in sorted(node["directories"])],
            "files": sorted(node["files"], key=str.casefold),
        }
    return finish(root)


def item_episode_groups(item: dict) -> list[dict]:
    """Group episode-prefixed files inside a combined extra item, without promoting it."""
    grouped: dict[tuple[int, int], list[dict]] = {}
    for file_row in item.get("files", []):
        basename = PurePosixPath(str(file_row.get("name", ""))).name
        match = TNG_FILE_RE.match(basename)
        if not match:
            continue
        key = (int(match.group("season")), int(match.group("episode")))
        grouped.setdefault(key, []).append(file_row)
    return [
        {"series": "TNG", "season": season, "episode": episode,
         "files": sorted(files, key=lambda row: str(row["name"]).casefold()),
         "title_candidates": sorted({
             TNG_FILE_RE.match(PurePosixPath(str(row["name"])).name).group("rest")
             .split("-US-", 1)[0].rsplit(".", 1)[0]
             for row in files
         })}
        for (season, episode), files in sorted(grouped.items())
    ]


def resolve_episode(catalog: dict, series: str, season: int, episode: int) -> dict:
    """Resolve only an exact per-episode copy item; refuse combined-item auto-downloads."""
    series = series.upper()
    items = [item for section in catalog.get("children", []) for item in section.get("items", [])]
    exact = [item for item in items
             if item.get("category") == "episode-copy" and item.get("series") == series
             and item.get("season") == season and item.get("episode") == episode]
    request = {"series": series, "season": season, "episode": episode,
               "episode_code": f"S{season:02d}E{episode:02d}"}
    if len(exact) == 1:
        item = exact[0]
        inventory = item.get("download_inventory", {})
        selected_names = [row["name"] for row in item.get("files", [])
                          if row.get("selected_by_download_archive")]
        workflow_warnings = list(item.get("workflow_warnings", []))
        if series == "DS9" and season == 1 and episode in (1, 2):
            workflow_warnings.append(
                "Emissary is a two-part special case; use its dedicated Part I/II workflow, not generic init_episode")
        return {
            "status": "matched",
            "request": request,
            "identifier": item["identifier"],
            "url": item["url"],
            "title": item.get("title"),
            "workflow_identity": f"{series}:S{season:02d}E{episode:02d}",
            "workflow_route": {
                "supported": series in ("DS9", "VOY"),
                "series": series,
                "episode": f"S{season:02d}E{episode:02d}",
                "run_stage_cli": (f"--episode S{season:02d}E{episode:02d} --series {series}"
                                  if series != "DS9" else f"--episode S{season:02d}E{episode:02d}"),
                "manifest_namespace": series,
                "note": "Lookup does not download media; initialization still requires verified download-manifest and the live ListOnly/integrity/paired-file/space gates.",
            },
            "suggested_download_folder_name": f"{series} S{season:02d}E{episode:02d} LaserDisc",
            "catalog_item": {
                "category": item.get("category"),
                "capture_labels": item.get("capture_labels", []),
                "tbc_file_count": item.get("tbc_file_count"),
                "workflow_warnings": workflow_warnings,
                "selected_file_count": inventory.get("selected_file_count"),
                "selected_file_names": selected_names,
                "missing_tbc_json_sidecars": inventory.get("missing_tbc_json_sidecars", []),
                "raw_efm_count": inventory.get("raw_efm_count"),
            },
            "next_step": "Confirm the human title from capture filenames and DVD evidence; choose the show-prefixed destination (or explicit user path); run download_archive.ps1 -ListOnly and verify live metadata, hashes, paired sidecars, and space. After the download is separately approved and verified, initialize with the matching series and use a series-aware plan/stage identity.",
        }
    if len(exact) > 1:
        return {"status": "ambiguous", "request": request,
                "identifiers": sorted(item["identifier"] for item in exact),
                "automatic_download_allowed": False}

    extras = []
    for item in items:
        if item.get("category") != "multi-episode-extra" or item.get("series") != series:
            continue
        for group in item.get("episode_groups", []):
            if group.get("season") == season and group.get("episode") == episode:
                extras.append({"identifier": item["identifier"], "url": item["url"],
                               "file_names": [file["name"] for file in group.get("files", [])],
                               "title_candidates": group.get("title_candidates", [])})
    if extras:
        return {
            "status": "combined-item-only",
            "request": request,
            "candidates": extras,
            "automatic_download_allowed": False,
            "reason": "The Archive account has matching files only inside a multi-episode extra item. The standard downloader selects the whole item, so it is not launched as a single-episode download.",
        }
    available = sorted({item.get("season") for item in items
                        if item.get("category") == "episode-copy" and item.get("series") == series})
    if series == "TNG":
        extra = next((item for item in items if item.get("category") == "multi-episode-extra"), None)
        return {"status": "no-standalone-item", "request": request,
                "available_seasons": available,
                "partial_episode_codes_in_extra": [
                    f"S{group['season']:02d}E{group['episode']:02d}"
                    for group in (extra or {}).get("episode_groups", [])
                ],
                "extra_item_identifier": extra.get("identifier") if extra else None,
                "automatic_download_allowed": False}
    return {"status": "not-found", "request": request,
            "available_seasons": available, "automatic_download_allowed": False}
