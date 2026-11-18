# Workflow Version 20 - post-run cleanup review

**From Version 19 to Version 20 on the app branch only.** Production `main` and W remain V15.

## User choices

The app adds a post-run storage review with the two primary options:

1. **Keep everything for now** (default and safe dismissal).
2. **Free up space - review downloads and large audio files**.

The second option is offered after a successful workflow containing QC. It requires the current
QC stage to have passed, a report with zero WARN/LIMIT/FAIL, and unchanged workflow fingerprints
for both the QC report and final deliverable. The review lists the exact episode download folder
and manifest-associated audio files at least 100 MiB. Each row can be deselected before a second
confirmation.

If the download folder is selected, small `download-manifest.csv`, TBC sidecar JSON, and text/
metadata files (up to 5 MiB each) are copied into a unique episode `references/post-run-cleanup/`
folder and hash-verified before the source folder is moved. A JSONL cleanup record is written
under episode logs before any move and records outcomes. Logs, plans, manifests, QC reports,
EFM provenance, and delivery outputs are never candidates.

Selected paths move to the OS Recycle Bin/Trash; the implementation has no permanent-delete
fallback. The app does not perform cleanup automatically. Windows cleanup is limited to fixed
local drives with an accessible Recycle Bin; macOS uses Finder Trash. Other platforms disable
the action. No backup is created automatically.

## Validation and limits

- Full suite: **402 passed, 7 skipped** (409 tests). Compile/import, generated-runbook, all 7
  PowerShell AST parse, and diff checks passed.
- Cleanup inventory, clean-QC gates, plan/report/deliverable fingerprints, metadata preservation,
  and no-trash-on-unsupported-platform behavior are covered by unit tests.
- No real episode cleanup, Recycle Bin/Trash mutation, media processing, or GUI smoke test was
  performed for this release.
