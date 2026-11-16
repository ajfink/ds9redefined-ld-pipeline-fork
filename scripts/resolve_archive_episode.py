#!/usr/bin/env python3
"""Resolve a natural episode request to a Section 31 Archive.org item URL.

Examples:
    python scripts/resolve_archive_episode.py "Download Episode 18 of season 2 of DS9"
    python scripts/resolve_archive_episode.py "Star Trek Voyager S01E01"
    python scripts/resolve_archive_episode.py --series "Deep Space 9" --season 2 --episode 18

This is a read-only catalog lookup. A ``matched`` result identifies a standalone
per-episode copy item; it does not download media. Always run the existing downloader's
``-ListOnly`` gate against live Archive metadata before a transfer. Combined/extra items
are never returned as automatic single-episode downloads.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive_catalog import (CATALOG_SCHEMA, identify_series, load_catalog,
                             parse_episode_request, resolve_episode)  # noqa: E402

DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "archive_catalog" / "section_31_tree.json"


def resolve_cli_request(request: str | None, series: str | None,
                        season: int | None, episode: int | None,
                        catalog: dict) -> dict:
    if request:
        if series is not None or season is not None or episode is not None:
            raise ValueError("use a natural-language request or --series/--season/--episode, not both")
        parsed = parse_episode_request(request)
    else:
        if series is None or season is None or episode is None:
            raise ValueError("supply a request, or all of --series, --season, and --episode")
        code = identify_series(series)
        if code is None:
            raise ValueError(f"unrecognized series alias: {series!r}")
        if season < 1 or episode < 1:
            raise ValueError("season and episode numbers must be positive")
        parsed = {"series": code, "season": season, "episode": episode}
    return resolve_episode(catalog, parsed["series"], parsed["season"], parsed["episode"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="?", help="natural request, e.g. 'Download episode 18 of season 2 of DS9'")
    parser.add_argument("--series", help="series name or alias")
    parser.add_argument("--season", type=int)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="included Archive tree JSON")
    args = parser.parse_args()

    catalog_path = Path(args.catalog).expanduser()
    catalog = load_catalog(catalog_path)
    if catalog.get("schema") != CATALOG_SCHEMA:
        raise ValueError(f"unsupported Archive catalog schema in {catalog_path}")
    result = resolve_cli_request(args.request, args.series, args.season,
                                 args.episode, catalog)
    result["catalog_snapshot"] = {
        "source": catalog.get("source", {}).get("account_url"),
        "generated_at_utc": catalog.get("source", {}).get("generated_at_utc"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "matched" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
