"""Episode picker index: the DS9 / VOY / TNG Archive catalog merged with this machine's state.

Sources of truth
* The bundled Section 31 snapshot (``fstk/templates/section_31_tree.json.gz``) or a refreshed
  snapshot named by the profile's ``catalog_path``. It is metadata only: navigation evidence,
  never proof that a download will match (the downloader re-verifies live metadata and MD5s).
* Local state is derived on demand from the profile: an episode workspace (manifest) under
  ``data_root`` and capture files under ``sources.downloads_root``.

Selection rules (v38 ``workflow_app_model.resolve_archive_picker_selection``, unchanged):
* only an exact standalone ``episode-copy`` item is selectable;
* DS9 S01E01/E02 (Emissary) needs its dedicated two-part route — listed, not selectable;
* TNG exists only inside a multi-episode extra item — listed as lookup-only;
* catalog warnings (e.g. a single capture) are shown; later stage gates stay authoritative.

Titles come from capture filenames and are display hints until confirmed against the
captures/DVD. Episodes missing from the snapshot are reported as gaps, never invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files as package_files
from pathlib import Path, PurePosixPath
from typing import Any

from fstk.core.errors import ConfigError
from fstk.core.identity import EpisodeId

from .archive import CATALOG_SCHEMA, load_catalog, parse_episode_request, resolve_episode

PICKER_SERIES = ("DS9", "VOY", "TNG")
PROCESSABLE_SERIES = ("DS9", "VOY")
_CAPTURE_SUFFIX = re.compile(r"-(?:L|M|P)\d+$", re.IGNORECASE)

LOCAL_STATES = ("workspace", "downloaded", "partial", "remote", "lookup-only")


@dataclass(frozen=True)
class CatalogEntry:
    series: str
    season: int
    episode: int
    title: str
    identifier: str
    url: str
    kind: str                         # "episode-copy" | "combined-item"
    capture_labels: tuple[str, ...]
    raw_efm_count: int | None
    selected_bytes: int | None
    selected_files: tuple[dict, ...]  # name/size/md5 of files the downloader would fetch
    warnings: tuple[str, ...]
    selectable: bool
    reason: str = ""

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    @property
    def episode_id(self) -> EpisodeId:
        return EpisodeId.parse(f"{self.series}:{self.code}")

    @property
    def title_hint(self) -> str:
        return self.title or "(title not in catalog)"


@dataclass(frozen=True)
class LocalStatus:
    state: str
    detail: str
    downloads_dir: Path | None = None
    present_files: int = 0
    expected_files: int = 0


@dataclass
class SeriesCoverage:
    series: str
    seasons: dict[int, list[int]] = field(default_factory=dict)  # season -> episode numbers listed
    note: str = ""


def default_catalog_path() -> Path:
    return Path(str(package_files("fstk.templates").joinpath("section_31_tree.json.gz")))


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int) -> dict:
    catalog = load_catalog(Path(path))
    if catalog.get("schema") != CATALOG_SCHEMA:
        raise ConfigError(f"unsupported Archive catalog schema in {path}")
    return catalog


def load(path: str | Path | None = None) -> dict:
    p = Path(path) if path else default_catalog_path()
    if not p.is_file():
        raise ConfigError(f"episode catalog not found: {p}")
    return _load(str(p), p.stat().st_mtime_ns)


def _title_from_files(item: dict, series: str) -> str:
    """v38 ``_archive_catalog_episode_title``: display-only title from a TBC filename."""
    code = f"S{int(item['season']):02d}E{int(item['episode']):02d}"
    prefix = re.compile(rf"^{re.escape(series)}-{code}-(.+)$", re.IGNORECASE)
    for row in item.get("files", []):
        name = PurePosixPath(str(row.get("name", ""))).name
        if not name.casefold().endswith(".tbc"):
            continue
        m = prefix.match(name[:-4])
        if m:
            title = _CAPTURE_SUFFIX.sub("", m.group(1)).strip(" -_")
            if title:
                return title
    return ""


def _selection(catalog: dict, series: str, season: int, episode: int) -> tuple[bool, str, list[str]]:
    result = resolve_episode(catalog, series, season, episode)
    warnings: list[str] = []
    if result.get("status") == "matched":
        warnings = list((result.get("catalog_item") or {}).get("workflow_warnings", []))
    elif result.get("status") == "combined-item-only":
        warnings = [result.get("reason", "This episode is listed only inside a combined Archive item.")]
    route = result.get("workflow_route") or {}
    emissary = series == "DS9" and any("Emissary" in w for w in warnings)
    selectable = (result.get("status") == "matched" and series in PROCESSABLE_SERIES
                  and route.get("supported") is True and not emissary)
    reason = ""
    if not selectable:
        if emissary:
            reason = "Emissary requires its dedicated two-part workflow and cannot use generic episode intake."
        elif result.get("status") == "combined-item-only":
            reason = "Listed only inside a combined item; not routed as a single-episode download."
        elif series == "TNG":
            reason = "TNG is not enabled for single-episode processing; combined-item matches are lookup-only."
        else:
            reason = "No safe standalone episode item is available in this catalog snapshot."
    return selectable, reason, warnings


class CatalogIndex:
    def __init__(self, catalog: dict):
        self.catalog = catalog
        self.entries: list[CatalogEntry] = []
        items = [item for section in catalog.get("children", []) for item in section.get("items", [])]
        for item in items:
            series = item.get("series")
            if series not in PICKER_SERIES:
                continue
            if item.get("category") == "episode-copy":
                season, episode = item.get("season"), item.get("episode")
                if not (isinstance(season, int) and isinstance(episode, int) and season > 0 and episode > 0):
                    continue
                selectable, reason, warnings = _selection(catalog, series, season, episode)
                inv = item.get("download_inventory") or {}
                selected = tuple({"name": r["name"], "size": int(r.get("size") or 0), "md5": r.get("md5")}
                                 for r in item.get("files", []) if r.get("selected_by_download_archive"))
                self.entries.append(CatalogEntry(
                    series, season, episode, _title_from_files(item, series), item["identifier"],
                    item.get("url", ""), "episode-copy", tuple(item.get("capture_labels") or ()),
                    inv.get("raw_efm_count"), inv.get("selected_bytes"), selected, tuple(warnings),
                    selectable, reason))
            elif item.get("category") == "multi-episode-extra":
                for group in item.get("episode_groups", []):
                    season, episode = group.get("season"), group.get("episode")
                    if not (isinstance(season, int) and isinstance(episode, int)):
                        continue
                    selectable, reason, warnings = _selection(catalog, series, season, episode)
                    self.entries.append(CatalogEntry(
                        series, season, episode, " / ".join(group.get("title_candidates") or []),
                        item["identifier"], item.get("url", ""), "combined-item", (), None, None, (),
                        tuple(warnings), selectable, reason))
        self.entries.sort(key=lambda e: (PICKER_SERIES.index(e.series), e.season, e.episode, e.kind))

    @classmethod
    def load(cls, path: str | Path | None = None) -> CatalogIndex:
        return cls(load(path))

    # ---------------------------------------------------------------- queries
    def series(self, series: str) -> list[CatalogEntry]:
        return [e for e in self.entries if e.series == series.upper()]

    def find(self, series: str, season: int, episode: int) -> CatalogEntry | None:
        for e in self.entries:
            if (e.series, e.season, e.episode) == (series.upper(), season, episode) and e.kind == "episode-copy":
                return e
        for e in self.entries:
            if (e.series, e.season, e.episode) == (series.upper(), season, episode):
                return e
        return None

    def search(self, text: str) -> list[CatalogEntry]:
        """Natural request ("DS9 season 2 episode 18", "voyager S01E01") or title words."""
        text = (text or "").strip()
        if not text:
            return list(self.entries)
        try:
            req = parse_episode_request(text)
            hit = self.find(req["series"], req["season"], req["episode"])
            return [hit] if hit else []
        except ValueError:
            pass
        words = [w for w in re.findall(r"[a-z0-9']+", text.casefold())]
        series_tokens = {"ds9": "DS9", "voy": "VOY", "voyager": "VOY", "tng": "TNG"}
        wanted_series = {series_tokens[w] for w in words if w in series_tokens}
        words = [w for w in words if w not in series_tokens]
        out = []
        for e in self.entries:
            if wanted_series and e.series not in wanted_series:
                continue
            hay = f"{e.code} {e.title}".casefold()
            if all(w in hay for w in words):
                out.append(e)
        return out

    def coverage(self) -> dict[str, SeriesCoverage]:
        cov = {s: SeriesCoverage(s) for s in PICKER_SERIES}
        for e in self.entries:
            cov[e.series].seasons.setdefault(e.season, []).append(e.episode)
        for s, c in cov.items():
            seasons = sorted(c.seasons)
            if s == "TNG":
                c.note = "Only a multi-episode extra item exists (lookup-only)."
            elif seasons:
                c.note = (f"Standalone items for seasons {seasons[0]}–{seasons[-1]}; later seasons are "
                          "not in this snapshot.")
        return cov

    def summary(self) -> dict[str, Any]:
        src = self.catalog.get("source") or {}
        return {"generated_at_utc": src.get("generated_at_utc"), "upload_count": src.get("upload_count"),
                "account": src.get("account_identifier"),
                "entries": {s: len(self.series(s)) for s in PICKER_SERIES},
                "selectable": sum(e.selectable for e in self.entries)}


# ------------------------------------------------------------------------ local state
def local_status(entry: CatalogEntry, profile) -> LocalStatus:
    """This machine's state for one catalog entry (stat-only; never hashes or downloads)."""
    if entry.kind != "episode-copy" or not entry.selectable:
        return LocalStatus("lookup-only", entry.reason)
    ep = entry.episode_id
    manifest = profile.episode_state_dir(ep) / "manifest" / "manifest.json"
    if manifest.is_file():
        return LocalStatus("workspace", f"episode workspace exists ({manifest.parent})")
    if not profile.sources.get("downloads_root"):
        return LocalStatus("remote", "sources.downloads_root is not configured")
    folder = profile.downloads_dir(ep)
    expected = [f for f in entry.selected_files if f["name"].casefold().endswith((".tbc", ".tbc.json"))]
    if not folder.is_dir():
        return LocalStatus("remote", "not downloaded", folder, 0, len(expected))
    present = 0
    for f in expected:
        p = folder / PurePosixPath(f["name"]).name
        if p.is_file() and (not f["size"] or p.stat().st_size == f["size"]):
            present += 1
    if expected and present == len(expected):
        return LocalStatus("downloaded", f"all {present} TBC/JSON files present at catalog sizes", folder,
                           present, len(expected))
    return LocalStatus("partial" if present else "remote", f"{present}/{len(expected)} TBC/JSON files present",
                       folder, present, len(expected))
