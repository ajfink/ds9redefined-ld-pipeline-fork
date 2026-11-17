# Workflow Version 18 — FromStunToKill app-branch release record

**From Version 17 -> Version 18 on the app branch only.** V18 adds a read-only episode gate
dashboard, guided report-only diagnostics, cadence plans, explicit delivery plans, and process
diagnostics. Production `main` and W remain V15; this release does not synchronize either tree.

## App functionality

- **Episode status dashboard:** runs the existing `validate_episode.py` gates on demand and shows
  PASS, WARN/LIMIT, and FAIL separately. It stops an all-gate sweep after a manifest failure, and
  prompts before a full sweep because the audio provenance gate may hash large files. Validation
  itself does not write manifests or media; the dashboard saves unique gate logs/reports (path-
  redacted in shared mode) under the episode logs folder.
- **Audio workbench:** creates immutable report-only plans for the LD/DVD rate diagnostic and PAD
  audio occupancy. It exposes explicit provisional-skip, window, correlation, and selector values;
  report output paths are unique. The reports remain evidence, not rate or source approvals.
- **Cadence workbench:** creates a measured-pad-aligned motion-trace plan and a platform-specific
  cadence-map/source-script plan. Windows requires the current TFM log and motion trace; macOS
  consumes an existing measured deinterlace map and makes the repair choice explicit. The app
  refuses missing trims or other required measured inputs and adds a human review of the generated
  cadence artifacts; it records no guessed cadence values.
- **Delivery workbench:** detects current artifacts and builds QC-only, remux+QC, or encode/remux/QC
  plans. Existing outputs are not overwritten. A full plan includes a distinct review gate before
  final encode, another before remux, and a review of QC. The GUI's Run confirmation still names
  final encode/remux explicitly. QC reports accept a selected new path and refuse overwrites.
- **Long-job monitor:** shows child PID, elapsed time, and time since console output. A 20-minute
  no-output interval displays an advisory; the operator may save a device-log diagnostic snapshot.
  The app never auto-kills or retries a potentially locked render/download.
- Extracted the episode dashboard, job-status policy, and guided plan builders into focused modules
  so future UI changes can be tested without growing the main Tk controller further.

## Validation and limits

- Full suite: **389 passed, 7 skipped** (396 tests); the skipped tests require the selected
  VapourSynth Python environment.
- `compileall`, warnings-as-errors imports, `generate_runbooks.py --check`, all 7 PowerShell AST
  parses, and `git diff --check` passed.
- Validation is unit/fixture and plan-construction based. No real episode gate sweep, cadence or
  audio measurement, encode, remux, full QC decode, Orc render, fresh-Mac GUI test, or workspace
  restore was performed for this release.
- Existing episode evidence gaps remain in `V16-QUEUE.md`; P95 motion classification is screening
  evidence, not a substitute for verifying the threshold on actual episode material.
- The user had already made a setup backup and explicitly canceled a duplicate backup request;
  this release did not create a backup.
