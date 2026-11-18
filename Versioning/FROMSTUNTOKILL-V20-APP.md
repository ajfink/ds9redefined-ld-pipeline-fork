# FromStunToKill app branch - V20 cleanup integration

V20 adds a post-run review with **Keep everything for now** and **Free up space** as its primary
choices. The free-space review is available only after a passing full workflow containing QC,
requires a clean report and unchanged QC/deliverable fingerprints, and lists exact episode
download/audio paths for user review. Large audio is defined as at least 100 MiB.

Small download-manifest, TBC-sidecar, and text metadata is copied and hash-verified under the
episode references folder before the download directory is moved. Logs, plans, manifests, QC and
provenance records, and deliveries are retained. Selected items go to the OS Recycle Bin/Trash
after an exact-path confirmation; permanent deletion is never used as a fallback. No cleanup is
automatic, and no backup is created.

See `V20-RELEASE.md` for validation and platform limits. Production `main` and W remain V15.
Validation: **402 passed, 7 skipped (409 tests)** plus compile/import, generated-runbook,
PowerShell parser, and diff checks. No actual user files were cleaned.
