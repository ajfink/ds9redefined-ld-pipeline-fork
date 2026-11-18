# Workflow Version 14 — release

**Status:** Release source is `Z:\ld-pipeline` on `main`. V14 source files were synchronized to W
after confirming each overwritten file matched the V13 source baseline.
W's unrelated dirty/untracked files and stale Git metadata were preserved; no reset/rebase was
performed. No media was downloaded or processed.

## Included in this candidate

- `archive_catalog/section_31_tree.json`: a metadata-only snapshot of all 275 public upload
  links on `https://archive.org/details/@section_31`, including each item's Archive file list
  and nested item-relative file tree. No media bytes were downloaded.
- `scripts/build_archive_catalog.py`: refreshes the account upload list and child item file
  metadata; refuses to replace the snapshot unless `--force` is explicit.
- `scripts/resolve_archive_episode.py`: resolves natural show/season/episode requests to an
  exact standalone episode-copy URL. It recognizes DS9/Deep Space Nine/Deep Space 9, VOY/
  Voyager, and TNG/The Next Generation, with optional Star Trek/Trek prefixes.
- The resolver refuses to auto-download missing episodes or a combined Archive item. Every
  normal item still requires the existing downloader's live `-ListOnly` integrity/sidecar/
  space gate before transfer.
- Canonical series identity is `SERIES:SxxExx`. Workflow plans persist `series` plus the legacy
  `episode` code; stage CLIs accept `--series VOY` or a direct qualified episode identity.
- New VOY manifests are stored separately, persist `series`/`identity`, and route all generated
  artifacts into `VOY/` children of configured output roots. Shared VOY workspaces use
  `VOY-<code>` folders. Logs, locks, workflow state, and resume are series-isolated.
- Bare legacy episode codes and manifests without `series` remain DS9. DS9 manifest/workspace/
  artifact paths and existing DS9 workflow plans remain unchanged. Existing DS9 workflow state
  without explicit identity fields is accepted as DS9-only resumable state.
- The README and canonical runbook describe lookup, series-aware initialization/processing,
  destination handling, migration compatibility, and the catalog limitations.

## Archive coverage snapshot

- 124 standalone DS9 episode-copy items (seasons 1–5).
- 120 standalone VOY episode-copy items (seasons 1–5).
- 15 DS9 and 15 VOY season-stack items, kept separate from episode-copy resolution.
- One `TNG-Extra-LaserDisc-Copies` multi-episode item. It has partial file groups for five
  episode codes, no raw EFMs, one unpaired TBC sidecar, and multiple title candidates under
  S04E01. It is never treated as a standalone episode download.
- Some standalone items have one capture or more than the current 2–6 weighted-stack limit;
  the resolver shows those warnings rather than hiding them.

## Validation and limits

- Full suite: **195 tests run, 188 passed, 7 skipped**.
- Python compilation/import checks, generated-runbook checks, diff checks, and PowerShell parser
  checks passed. Mock workflow execution verified same-code DS9/VOY logs, locks, state, and
  resume separation; the VOY initializer test used synthetic verified-file metadata only.
- The snapshot builder fetched Archive metadata only. Resolver examples for DS9 S02E18 and
  VOY S01E01 return their matching item URLs; TNG extra-only cases return a non-downloadable
  status. The live downloader was not run.
- VOY initialization/processing is series-aware and supported. TNG combined-item-only entries
  remain blocked; the generic initializer's Emissary S01E01/E02 restriction applies to DS9 only.
- No automatic migration is performed for any pre-existing Voyager files from earlier
  lookup-only experiments. Operators must review/move such files themselves after choosing the
  canonical VOY namespace. Existing DS9 records are not renamed or rewritten.
- Existing Voyager staging data were not migrated automatically. Future operators must review
  them before applying the series-scoped paths. W's Git branch history still needs a separate
  safe reconciliation if a clean fast-forward is desired.
