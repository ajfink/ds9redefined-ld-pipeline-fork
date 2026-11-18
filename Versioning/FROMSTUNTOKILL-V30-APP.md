# FromStunToKill app branch - V30 incomplete-job recovery

V30 adds startup recovery for incomplete workflow jobs and shortens an image-review tab label.

## Changes

- Changed the PAD image-review tab label from **Interpolated cut pads** to **Interpolated pads**.
- After the active device profile/template is loaded, scan only that device's episode workspaces
  for nonterminal workflow states with an exact-hash saved plan. Show a radio-select popup with
  episode, state, job, last completed/approved step and timestamp. **Start new** closes the popup;
  **Load selected** is disabled until a listed run is selected and then opens its immutable plan.
  If loaded from this list, the plan is identified as an incomplete recovery and **Resume** stays
  enabled; it does not auto-resume.
- A recovered plan remains inspectable and does not execute on load. Its **Resume** button is
  available; Run/Resume still perform plan and fingerprint checks before continuing.
- Allow older-version handoff only to a final waiting review when all stages have passed and
  plan/profile identity plus latest output/review fingerprints still match. No stages can be
  rerun through this compatibility path.

## Validation

- **459 passed, 7 skipped** (`python scripts/run_tests.py -q`); coverage includes multiple
  incomplete runs, saved-plan matching, last-completed timestamps, and Resume activation after
  recovery-plan loading.
- Compileall and warnings-as-errors imports passed; generated runbook/ASCII checks passed;
  PowerShell AST parsing passed for all 7 script `.ps1` files plus root `preflight.ps1`;
  `git diff --check` passed (only expected LF-to-CRLF working-copy notices).
- No workflow stage or media processing was run for this update.
- Production `main` and W remain V15; this is app-branch only.
