> **NOT FOR LIVE WORKFLOW USE.** This is release-planning material only. Do not read, cite,
> or apply queue entries as episode instructions, measurements, approvals, stage arguments,
> or gates. This V14 proposal is not live workflow authority.

# V14 planning record — Archive catalog and series-aware workflow

**V14 release source is Z `main`.** W's source files are synchronized to V14; its existing Git metadata
and unrelated local changes were preserved rather than reset/rebased. This queue is historical
planning material and remains non-operative.

## Objective

Resolve a natural request such as "Download Episode 18 of season 2 of DS9" to an exact
Internet Archive item URL, using an included, auditable tree of Section 31's Archive links.
The resolver must never synthesize an identifier for an absent episode or mistake a season
stack/combined extra item for a standalone episode copy.

## Source snapshot reviewed

`https://archive.org/details/@section_31` is a public account uploads page, not a collection
identifier. Its snapshot reported 275 uploads: 124 DS9 episode-copy items, 120 VOY episode-copy
items, 15 DS9 season-stack items, 15 VOY season-stack items, and one `TNG-Extra-LaserDisc-Copies`
item. `archive_catalog/section_31_tree.json` records child item links and per-item file metadata
from Archive's `/metadata/<identifier>/files` endpoint. The snapshot includes no media bytes.

- Standalone DS9 copy items cover seasons 1–5 (124 episodes); no season 6/7 items were found.
- Standalone VOY copy items cover seasons 1–5 (120 episodes); no season 6/7 items were found.
- Season stacks are retained as separate tree branches, never selected for an episode request.
- No standalone TNG episode-copy items were found. The single TNG extra item contains partial
  file groups for S01E24, S04E01, S04E21, S05E15, and S05E19. S04E01 has conflicting title
  candidates; the item also has a missing TBC JSON sidecar and no raw EFMs. Do not auto-download
  this combined extra for a single-episode prompt.

The file metadata is a snapshot, not a promise of continued availability. Every standalone
match still requires the existing downloader `-ListOnly` check for live files, hashes, paired
sidecars, capture count, and projected free space before any transfer.

## Z authoring candidate implementation

- `scripts/build_archive_catalog.py` enumerates the public account upload listing and child
  file metadata; it is metadata-only, fails closed on incomplete enumeration, and refuses to
  replace a snapshot unless `--force` is explicit.
- `scripts/resolve_archive_episode.py` accepts natural requests or `--series/--season/--episode`
  and normalizes DS9/Deep Space Nine/Deep Space 9, VOY/Voyager, and TNG/The Next Generation
  with optional Star Trek/Trek prefixes.
- The resolver returns a direct item URL only for one exact standalone episode-copy item. It
  reports `combined-item-only`/`not-found` otherwise and never starts a download. VOY matches
  route to a series-aware plan/initializer.
- Canonical identity is `SERIES:SxxExx`; legacy bare codes are DS9. New VOY manifests use
  `episodes/VOY/<code>.json`; shared workspaces use `episodes/VOY-<code>`. VOY artifacts/logs/
  resume are series-isolated. Existing DS9 paths and old DS9 workflow states remain compatible.
- V14 tests cover alias forms, natural/direct episode syntax, unavailable episodes, exact
  resolution, TNG combined-item refusal, file-tree safety, and current snapshot counts.

## Observed boundaries and safety decisions

- The catalog snapshot lists DS9 S01–S05 and VOY S01–S05 as standalone episode-copy items;
  it does not synthesize missing seasons or IDs.
- Some listed episode items have only one capture, or more than the current weighted-stack
  limit of six. The resolver surfaces capture-limit warnings; the usual stage gates still apply.
- TNG has no standalone episode-copy item in this account. The one TNG extra bundle contains
  five partial episode-code groups, has no raw EFM files, and contains a TBC without a paired
  JSON sidecar. Resolver results from it are informational only and non-downloadable by default.
- VOY processing is allowed only through the new series-aware manifest/workspace route. Do not
  feed its URL to a bare legacy DS9 plan or reuse DS9 output roots/manifest paths.
- Archive item slugs are not human-title proof. After resolving the identifier, use the live
  downloader `-ListOnly` result and verify capture filenames/title (and supplied DVD when
  available) before transfer.

## Release gates

- Review a refreshed catalog diff (new/missing item IDs, season coverage, file counts, and
  sidecar/EFM inventory) before publishing any refresh.
- Confirm aliases do not treat generic "Star Trek" alone as a series and ambiguous requests
  stop instead of guessing.
- Keep TNG extra items and stack items non-resolvable as standalone episodes until each has a
  safely scoped file-selection and manifest-initialization path.
- Migration of pre-existing Voyager files still requires operator review. No automatic move or
  relabel is performed by the release.
- No media downloads are part of catalog generation or resolver validation.
