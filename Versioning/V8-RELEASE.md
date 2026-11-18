# Workflow Version 8 release notes

V8 is an incremental workflow/logging update over V7. `WORKFLOW_VERSION` is 8; no V7
quality gate was relaxed.

## Included

- Per-device episode workspaces are anchored to the folder containing the README used for
  the episode, regardless of whether that README is on W:, another share, or another
  `ld-pipeline` copy. The preferred layout is
  `devices/<device>-<instance-id>/episodes/<code>/` with separate manifest, logs,
  references, and cut-PAD folders.
- Workspace creation/reuse is required procedure but not a workflow gate. `run_stage.py`,
  Python manifest helpers, and PowerShell episode helpers attempt to resolve/create the
  README-folder workspace. If it is unavailable, the run warns and uses an isolated
  device-local fallback under `roots.logs` or `roots.work`; it never falls back to writing
  elsewhere in the README folder. Shared-root ACL checks remain a separate readiness gate.
- Each workspace creates `logs/README.md` with the logging/evidence contract. Stage logs and
  JSON reports keep the command, workflow version, gates, complete output, exit status, and
  active elapsed time.
- `run_stage.py` records active elapsed, gate, action, and post-action seconds per invocation.
  Time outside the invocation, including waiting for user gate input, is excluded. Added
  `scripts/stage_clock.py` for manual stages, with pause/resume to exclude human wait from
  active time.
- Updated README, PORTING, canonical/Qwen/ASCII runbooks, profile examples, release notes,
  and changelog for README-relative workspace placement, fallback behavior, ACL ownership,
  and timing records.
- Fixed the device-local fallback candidate resolver defect reported by the Lenovo Legion
  instance; regression tests cover fallback creation and local cut-PAD provenance.

## Validation status

- Full suite: **63 passed, 7 Mac/VapourSynth-only skipped**.
- Python compilation, PowerShell parsing, generated-runbook checks, stage-clock timing tests,
  and early-download planning passed.
- macOS arm64 end-to-end success on the M5 S01E10 workflow remains the V7 evidence; fresh-Mac
  installation coverage is still not implied.
- W's release-root ACL remains unchanged and writable to the connected account. Shared
  processing remains blocked until an administrator makes the README tree read-only and
  grants each device writes only to its own `devices/<device-instance>/` subtree.

See `WORKFLOW_VERSIONING.md` and the V8 entry in
`W:\ld-pipeline-execution-log.md` for the full history and publication record.
