# FromStunToKill app branch - V23 tooling and dependency updates

V23 preserves V22's final A/V alignment priority: keep the LD's internal picture/audio timeline,
retime DVD 5.1/SRT to it, and use final A/V QC as the acceptance criterion. Internal black-length
matching does not authorize cutting LD.

## Changes

- Readiness now treats Python and direct-package version differences as informational. Required
  executables, package imports, stage capabilities, plugins, and other functional requirements
  still fail when missing or unusable.
- Added the GUI's **Dependency update** button and pop-up. It compares required Python packages
  with the newest stable release compatible with the selected interpreter and supported
  WinGet/Homebrew-managed executables with their package catalog. Updates are individually
  selectable, default checked when available, rechecked immediately before installation, and
  require confirmation. Only allowlisted packages/tool IDs can be installed.
- Made stack-reference output robust to Windows console encodings; added required-anchor
  coverage checks that automatically include a measured `video.delta_s` without extending into
  unmatched LD-only front matter; and made cadence-map prerequisite errors report all missing
  measurements together.
- Replaced the doctor's stale V12 label with the live workflow version, added a mixed
  UTF-8/UTF-16 log reader, and updated the prep-checklist template to require status refreshes
  as a job advances.

## Validation

- **426 passed, 7 skipped** (`python -m unittest discover -s tests -v`).
- `compileall`, warnings-as-errors imports, generated-runbook check, PowerShell parser check for
  `build_map.ps1`, a real UTF-16 log-reader smoke check, and `git diff --check` passed.
- No desktop GUI display, network version check/install, dependency installation, or media
  processing was run.
- No media was processed and no dependency/tool installation was run for this change.
- Production `main` and the W shared tree remain at V15; this app-branch V23 is not a sync.
