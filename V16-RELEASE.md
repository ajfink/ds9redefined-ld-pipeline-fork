# Workflow Version 16 — release record

**Transition: Version 15 -> Version 16.** This release strengthens per-device
workspace resilience, retime/EFM evidence provenance, long-stage supervision, and episode
closeout. It does not relax measurement, approval, or QC requirements. No episode media was
processed for implementation or release validation.

## Implemented

- Release-root writability is an advisory in `doctor.py`, not a processing blocker. Application
  guards still reject release-root and peer-device writes. Shared workspace creation now
  requires a pre-provisioned `devices/` directory; if the device workspace cannot be written,
  the helper uses the device-local fallback. `shared_workspace.py sync-local` copies only
  missing evidence, refuses conflicts, keeps `workspace.json` local to each storage mode,
  checks matching workflow versions, and requires explicit review before syncing logs.
- `efm_report_table.py --episode` validates the report's output path and exact capture inventory
  against the active episode before presenting a selection matrix. `--dir` remains explicitly
  unbound/report-only.
- `validate_episode.py` rejects malformed retime interval schemas and, at retime stage, checks
  the recorded `step` and any non-null `gap_s` against the measured offsets/DVD silence. It also
  verifies the sampled alignment covers the measured delta anchor. `gap_s` is documented as
  `DVD silence duration - (off_after - off_before) / (30000/1001)`.
- `scan_padfixed_black.py` defaults to an existing manifest deliverable, including `pre_upscale`.
- `validate_tfm_run.py` verifies FFmpeg session-log exit/frame evidence against the manifest;
  it does not search the TFM decision log for FFmpeg stats.
- Shared-workspace QC writes a unique device-log closeout linking the detailed QC report and
  recording source/retime file availability at QC time.
- `run_stage.py` uses a device-local subprocess cwd by default in shared mode. The new
  `stack-render` stage checks doctor, inputs, and explicit space budget, refuses an existing
  stack master, uses a separate Orc log, emits heartbeats, and stops only its process group
  after the configured stale-progress timeout.
- `process_utils.py` provides UTF-8-safe console streaming and PID-scoped process-tree cleanup.

## Validation

- Full unit suite: **261 passed, 7 skipped** (268 tests) on Z and W.
- `compileall` and imports with warnings treated as errors passed. Generated runbooks are
  current; regenerated plain-text output matches byte-for-byte. All 8 PowerShell files parsed.
  `git diff --check` and trailing-whitespace checks passed (Git emitted only LF/CRLF
  normalization notices).
- Validation is synthetic/fixture-based only. Existing Z/W episode logs are evidence for the
  proposed fixes, not media reruns performed for V16.

## Boundaries

- No ACL is changed or assumed. The doctor reports write access but does not waive the
  application's device-subtree path guards.
- Local resync never overwrites a different shared file. Manifest conflicts require manual
  reconciliation; logs are not synced unless explicitly reviewed and confirmed.
- Published to GitHub `main` as commit `4aeea60` (the app-version branch was not touched).
- The V16 commit patch applied cleanly to W's working files and W's full suite passed.
  W's stale V7-based Git history and unrelated dirty/untracked files were preserved; no
  reset, rebase, or W commit was performed.
