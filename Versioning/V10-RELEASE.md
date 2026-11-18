# Workflow Version 10 release notes

V10 adds retime-output verification, report-only measurement diagnostics, safer workspace
compatibility, and safeguards for alignment and measurement evidence. Existing quality,
space, ACL, and approval gates remain in force.

## Included

- `retime_dvd_assets.py` scans the generated DVD 5.1 output against planned gap positions
  and writes a JSON verification report. Touching detector events are coalesced; separated
  silences are not. Measurable mismatches stop the retime stage. Gaps below the 50 ms
  position-resolution floor are `LIMIT` and are surfaced as QC warnings, never as a
  confident PASS. Cuts are not automatically moved.
- Added `scripts/efm_paths.py` and updated the runbook to use episode-scoped EFMSink WAV and
  report paths with existing-output refusal.
- Added report-only `scripts/measure_pad_audio.py` and `scripts/measure_ld_black.py`. They
  produce per-episode evidence without selecting a sample rate/black threshold or editing
  the manifest.
- DVD/LD offset queries outside the sampled alignment range now fail instead of silently
  extrapolating. `measure_dvd_alignment.py` reports its sampled frame range. Break-edge
  content windows are clipped away from neighboring recorded blacks; insufficient windows
  remain unmeasured.
- Legacy workspace mode is inferred in memory when older metadata lacks `storage_mode`,
  without rewriting the original workspace version. Repeated `shared_workspace.py init`
  refuses to make a matching duplicate and recommends `ensure` or an explicit instance root.
- Fixed the invalid-escape `SyntaxWarning` in `av_check_output.py` and added a one-off
  measurement checklist to the canonical and generated runbooks.

## Validation and limits

- **105 passed, 7 VapourSynth-only skipped.** Python compilation, generated-runbook checks,
  `git diff --check`, warning-as-error import of `av_check_output.py`, and PowerShell parsing
  (8 files) passed.
- Synthetic/unit tests cover silence parsing/gap limits, PCM PAD measurements, luma threshold
  sensitivity, alignment range guards, break-window clipping, workspace compatibility, and
  EFM path collisions. No EFM decode, retime, or production episode was run for this release.
- The new retime verifier and measurement reports have not been validated against real
  episode media under V10. S01E08/S01E11 findings remain episode-specific; their values are
  not defaults.
- Linux production remains blocked. No new macOS support or full end-to-end claim is made;
  the Mac cadence-dispatch integration issue remains open. The W ACL blocker and same-episode
  cross-job coordination remain separate prerequisites.

See `WORKFLOW_VERSIONING.md` and section 39 of the authoring-device
execution log.
