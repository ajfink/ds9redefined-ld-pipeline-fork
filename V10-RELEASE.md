# FromStunToKill – LaserDisc Workflow: V10 adaptation notes

V10 adds retime-output verification, report-only measurement diagnostics, safer workspace
compatibility, and safeguards for alignment and measurement evidence. Existing quality,
space, ACL, and approval gates remain in force.

This fork ports the mainline V10 changes while preserving the V9 ordered workflow, its
prompt/config intake pattern, review gates, and supervised Windows/macOS cadence adapters.
It remains an application candidate; same-version numbering does not make it authoritative.
The optional local Tk cockpit is a prototype frontend over the existing plan schema and
`run_workflow.py`; the command-line workflows remain available. The intended GitHub project
is <https://github.com/ajfink/ds9redefined-ld-pipeline-fork>; `origin` already points there
and its remote `main` is V10. The source-only `fromstuntokill-app` branch is published; no
pull request was created.

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
- The FromStunToKill GUI now derives episode codes from the Archive item ID or metadata,
  automatically reuses the default per-user machine profile or creates one when absent,
  resolves the manifest path from the selected profile/episode workspace, and provides hover
  help for the intake fields. It does not ask users to type an episode code or locate a
  profile/manifest path.

## Validation and limits

- Mainline V10 reported 105 passed and 7 VapourSynth-only skipped. This fork's expanded suite
  passes **127 tests, with 7 VapourSynth-only skipped**.
- Fork checks passed: Python `compileall`, generated-runbook `--check`, `git diff --check`,
  `av_check_output.py` import with `SyntaxWarning` treated as an error, PowerShell AST parsing
  of 8 scripts, and `run_stage.py --list` with both new diagnostics registered.
- Synthetic/unit tests cover silence parsing/gap limits, PCM PAD measurements, luma threshold
  sensitivity, alignment range guards, break-window clipping, workspace compatibility, and
  EFM path collisions. No EFM decode, retime, or production episode was run for this release.
- The new retime verifier and measurement reports have not been validated against real
  episode media under V10. S01E08/S01E11 findings remain episode-specific; their values are
  not defaults.
- Linux production remains blocked. The fork includes a Mac cadence-dispatch adapter with
  unit coverage, but real Mac execution and the conditional QTGMC repair path remain
  unvalidated. The W ACL blocker and same-episode cross-job coordination remain separate
  prerequisites.

## Fork adoption and supervised canary boundary

- The supervised S01E08 canary completed Orc stack rendering, TFM logging, motion trace, and
  cadence-map generation, and the stack/cadence review gates were explicitly approved. It
  predates this V10 port, so it does not validate the new V10 retime verifier, PAD/black
  diagnostics, EFM path helper, or other changed V10 analysis behavior on media.
- TFM processing exposed a UTF-8-BOM issue and then a report-redaction helper mismatch. Both
  fixes have regression coverage; the retained log was verified at 83,017 consecutive frames.
- No PAD repair, final encoding, retime generation, or remux was run as part of that canary.
- The fork retains the approved prompt-to-configuration intake policy: confirmed URLs, DVD
  selections/paths, and settings may arrive through an AI prompt but must be persisted to
  the designated config/plan and validated, not left only in chat.
- The GUI tests cover real-template immutability, DVD/no-DVD argument generation, Archive
  episode inference, default profile location/creation and legacy-profile reuse, plan
  validation, and overwrite refusal. Field-label tooltips explain the decisions. The desktop
  window itself still needs an interactive user acceptance pass; the V10 plan/runner contract
  is unchanged.
- GitHub preparation check: `main` remains the V10 baseline and `fromstuntokill-app` holds
  the application branch. GitHub CLI is not installed, so no repository settings or pull
  request were created. The local `CONFIGURE HERE FIRST.json` is ignored and excluded; only
  the sanitized example is published.

See `WORKFLOW_VERSIONING.md`, `changelog.txt`, and the mainline V10 implementation record.
