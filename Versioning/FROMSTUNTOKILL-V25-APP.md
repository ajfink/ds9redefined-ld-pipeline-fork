# FromStunToKill app branch - V25 incremental maintainability and validation

V25 preserves the V24 app and workflow behavior while implementing the selected cleanup and
validation recommendations. Python/package version differences remain informational; no
runtime-version blocker or Python pin was added. The root `WORKFLOW_VERSION` remains at the
project root for existing scripts and preflight contracts.

## Changes

- Added `scripts/run_tests.py`, which configures import paths and resolves the cached temp root
  before unittest discovery. Added `tests/support.py` for normalized temp-directory fixtures
  and Windows-path assertions that preserve foreign-platform spelling.
- Added Windows/macOS GitHub Actions validation for the unittest suite, compilation, generated
  Markdown/ASCII docs, diff whitespace, and PowerShell syntax.
- Added `regen_plain_text.py --check`; it compares exact generated CRLF ASCII bytes without
  modifying the companion.
- Centralized streaming file hashes in `fileutil.py`, while keeping call-site names, chunk sizes,
  algorithms, and run-workflow heartbeat callbacks. Centralized lock/backup/atomic-replace
  mechanics in `manifest_io.py`, retaining record-module wrappers and their error messages.
- Shared the byte-identical normalization and console/line-reader helpers. Extracted the GUI's
  workflow process-control methods into `workflow_app_process.py`; `WorkflowApp` remains the
  composition root.
- Moved historical release, app-branch, workflow-versioning, planning-queue, and changelog records
  under `Versioning/`. The standalone queue/changelog files were retired from the branch later;
  the root `WORKFLOW_VERSION` marker remains in place.

## Validation

- Windows authoring checkout: **444 passed, 7 skipped** (`python scripts/run_tests.py -q`).
- `compileall` for scripts/tests and warnings-as-errors imports of the refactored modules passed.
- Generated runbook and ASCII companion checks passed; PowerShell AST parsing passed for all
  7 script `.ps1` files plus root `preflight.ps1`; `git diff --check` passed (Git emitted only
  expected LF-to-CRLF working-copy notices).
- macOS CI and interactive GUI testing remain unrun locally; GitHub Actions will exercise the
  Windows/macOS matrix after the branch is pushed.
- No media processing, dependency installation, or Python-version pin change was performed.
- Production `main` and the W shared workflow remain V15; V25 is app-branch only.
