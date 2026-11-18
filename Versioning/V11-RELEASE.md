# Workflow Version 11 release notes

V11 distinguishes historical planning notes from live jobs and fixes the shared-mode
episode initialization crash. It does not adopt the dependency-tolerance proposal or LD/DVD
comparator; those remained outside the V11 implementation.

## Included

- Added a prominent non-operative notice to the planning notes then in the repository. README and
  canonical/generated runbooks explicitly separated planning from live episode instructions,
  measurements, approvals, stage arguments, and gates. Those standalone planning files have since
  been retired from this app branch.
- Fixed the shared-mode `init_episode.py` crash: workspace resolution now uses `code`, the
  episode identifier already derived from the verified Archive item, rather than the
  undeclared `args.episode` option.
- Added AST regression tests that verify `args.*` reads are declared and specifically guard
  the shared-workspace call, along with live-document boundary checks.
- The dependency policy and LD/DVD audio comparator remained out of scope. The comparator
  prototype was untracked and was not included in the V11 release.

## Validation and limits

- **111 passed, 7 VapourSynth-only skipped.** Python compilation, generated-runbook checks,
  `git diff --check`, and PowerShell parsing (8 files) passed.
- The S01E12 Archive transfer/initializer validation belongs to its separate device job log
  (§43); no Archive request or real-media processing was performed as part of V11 release
  validation.
- Linux production remains blocked. No new macOS support is claimed; the Mac cadence-dispatch
  issue remains open.

See `WORKFLOW_VERSIONING.md` and section 44 of the authoring-device execution
log.
