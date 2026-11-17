# FromStunToKill desktop app — V16 branch integration record

## Scope and publication boundary

This branch extends the V15 desktop-app integration with V16 workflow safety changes. The
`WORKFLOW_VERSION` value 16 applies to the FromStunToKill app branch; production `main` and the
W shared tree remain at V15 and are not changed by this publication. The device `devices/`
tree is ignored and not part of the app branch commit.

## Integrated behavior

- Added a visual PAD classification window with source flanks, full-raster metrics, explicit
  cut/RIFE decisions, cut still/hold policy, residual acceptance, and exact RIFE warning
  acknowledgement. The recorder requires immutable decision evidence and a human gate before
  the app automatically builds an isolated RIFE-only plan.
- Added EFM per-capture log/source/WAV/report verification and a separate human comparison and
  selection UI. Selection requires provenance for every capture, stores a rationale, checks
  refreshed Orc source order/alignment counts, backs up the manifest, and is followed by a human
  review gate. Existing reconstructed cross-episode logs fail closed.
- Added unique RIFE retry folders, exact source-index neighbor extraction, unique audio-rate
  report pairs, and advisory Orc progress/stall heartbeats without process termination.
- Added episode/device workspace archive, checksum verification, and no-merge dry-run-first
  restore helpers.
- Expanded device shared-write allowlists only for the current episode's `plans/` and `reviews/`
  folders, which the GUI uses for immutable plans and evidence.

## Validation and platform limits

Release validation: **365 passed, 7 skipped (372 tests)**; Python compilation, warnings-as-errors
imports, generated-runbook check, seven PowerShell parser checks, and diff check passed. Tests are
fixture-based and do not claim real-media EFM/RIFE/Orc execution, Mac runtime validation, restore
of a real device workspace, or final encode/remux. Open episode
evidence remains documented in `V16-QUEUE.md`; no reconstructed provenance gap is represented as
resolved. See `V16-RELEASE.md` for the validation boundary and production-branch status.
