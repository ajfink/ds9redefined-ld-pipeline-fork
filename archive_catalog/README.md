# Section 31 Archive.org item tree

`section_31_tree.json` is a metadata-only snapshot of the public uploads linked from
<https://archive.org/details/@section_31>. It records the account's child items and each
item's file/directory listing; the builder did **not** download TBC, EFM, PCM, or other media
bytes. File sizes, names, formats, and hashes are Archive metadata, not proof that a future
download will still match.

## Snapshot contents

The captured account page reported 275 upload items:

| Item group | Count | Prompt resolver use |
|---|---:|---|
| DS9 episode `LaserDisc-Copies` items | 124 | Standalone episode lookup; seasons 1–5 are present |
| VOY episode `LaserDisc-Copies` items | 120 | Standalone episode lookup; seasons 1–5 are present |
| DS9 season-stack items | 15 | Indexed for reference, never used as a single-episode match |
| VOY season-stack items | 15 | Indexed for reference, never used as a single-episode match |
| `TNG-Extra-LaserDisc-Copies` | 1 | Multi-episode extra only; not safe for automatic single-episode downloads |

The TNG extra item contains files for S01E24, S04E01, S04E21, S05E15, and S05E19. S04E01
has two distinct filename title candidates, and the item has no raw EFM files plus a TBC
without a paired JSON sidecar. It is not a substitute for a standalone, complete TNG episode
item. The resolver reports these as combined-item candidates and refuses to trigger a download.

DS9 and VOY episode-copy items cover seasons 1–5 only. Seasons 6–7 are absent from this
account snapshot; the catalog never fabricates identifiers for missing items. Some present
episode items have only one capture or more than the current 2–6 weighted-stack limit; those
items are still correctly linked, but the resolver surfaces a workflow warning and the later
stage gates remain authoritative.

## Natural-language resolution

The local helper accepts requests such as:

```text
python scripts/resolve_archive_episode.py "Download Episode 18 of season 2 of DS9"
python scripts/resolve_archive_episode.py "Star Trek Voyager episode 1 season 1"
python scripts/resolve_archive_episode.py "Trek Next Generation S04E21"
```

It recognizes these aliases:

- DS9: `DS9`, `Deep Space Nine`, `Deep Space 9`, optionally prefixed with `Star Trek` or `Trek`.
- VOY: `VOY`, `Voyager`, optionally prefixed with `Star Trek` or `Trek`.
- TNG: `TNG`, `The Next Generation`, `Next Generation`, optionally prefixed with `Star Trek` or `Trek`.

Only an exact standalone episode-copy item produces `status: matched` and a regular item URL.
The helper performs no download. For every match, run
`download_archive.ps1 -Url <resolved-url> -Destination <profile path> -ListOnly` and verify
the live item title, selected files, paired sidecars, MD5s, and space gate before any transfer.
Confirm the human episode title from capture filenames and the supplied DVD when available;
Archive item titles may be slugs.

## Refreshing the included snapshot

The refresh tool uses the public account-upload listing and each child item's Archive metadata
files endpoint. It fails rather than writing a partial tree. It refuses to overwrite the
included snapshot unless explicitly asked:

```text
python scripts/build_archive_catalog.py
python scripts/build_archive_catalog.py --force
```

Refreshes are **core catalog changes**: review count changes, missing episode gaps, item IDs,
per-item file lists, and the TNG-extra safety status; rerun catalog/resolver tests and include
the result in the next workflow release. A catalog match is navigation evidence only—the live
downloader's ListOnly metadata/integrity/space checks remain mandatory.
