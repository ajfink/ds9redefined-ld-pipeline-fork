# Workflow Version 11 release notes

V11 makes release queues explicitly non-operational during live jobs and fixes the shared-mode
episode initialization crash. It does not adopt the V11 queue's dependency-tolerance proposal
or LD/DVD comparator; those remain planning-only proposals.

## Included

- Added a prominent **NOT FOR LIVE WORKFLOW USE** notice to `V9-QUEUE.md`, `V10-QUEUE.md`,
  and `V11-QUEUE.md`. README and canonical/generated runbooks now explicitly forbid reading,
  citing, or applying queue entries as episode instructions, measurements, approvals, stage
  arguments, or gates. Live work must follow the current README/runbook and released scripts.
- Fixed the shared-mode `init_episode.py` crash: workspace resolution now uses `code`, the
  episode identifier already derived from the verified Archive item, rather than the
  undeclared `args.episode` option.
- Added AST regression tests that verify `args.*` reads are declared and specifically guard
  the shared-workspace call. Added queue-boundary tests for all queue files and generated
  runbooks.
- Updated the V11 queue to record the accepted changes while retaining the dependency policy
  and LD/DVD audio comparator as proposals. The comparator prototype remains untracked and
  was not included in the V11 release.

## Validation and limits

- **111 passed, 7 VapourSynth-only skipped.** Python compilation, generated-runbook checks,
  `git diff --check`, and PowerShell parsing (8 files) passed.
- The S01E12 Archive transfer/initializer validation belongs to its separate device job log
  (§43); no Archive request or real-media processing was performed as part of V11 release
  validation.
- Linux production remains blocked. No new macOS support is claimed; the Mac cadence-dispatch
  issue remains open.

See `WORKFLOW_VERSIONING.md`, `changelog.txt`, and section 44 of the authoring-device execution
log.
