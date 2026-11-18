# FromStunToKill app branch - V29 resume and GUI workflow controls

## Diagnosed test-run failure

The S01E18 reference-selection stage completed successfully, selecting L1 and producing its
project/report. Resume then blocked because the earlier input-validation step fingerprinted the
manifest before the reference stage; the reference stage correctly changed that manifest, and
resume treated this declared downstream write as unexplained input drift. The runner now ignores
an earlier step's fingerprint for paths that a later, already-passed step declares as outputs,
while still verifying the latest writer's output fingerprints and the waiting review evidence.
One-version workflow-code handoff is allowed only for a blocked/waiting job whose stages all
passed and whose final remaining step is the waiting review. This permits recovery of the logged
V28 job without re-running the long reference selection.

## GUI updates

- Moved intake-template, existing-plan, machine-profile, verified-EFM-selection, and retime-report
  controls into a collapsed **Advanced** section. Removed the public Preview button.
- **Run** now previews first and, only after a successful preview, executes the plan in the same
  action; confirmations and human review gates remain intact. **Resume** previews if needed, then
  resumes verified completed work. Failed non-download stages are still not blindly retried.
- Added **Pause** beside Run. It is enabled only while an execution stage is active and suspends
  the active process tree. **Resume** continues that same process; it does not restart the stage.
- After all capture EFM provenance reports verify, the GUI opens the existing human capture
  comparison/selection dialog. It does not choose or rank a capture. Once a retime-report plan
  completes, its exact generated report opens automatically for review; parameters remain
  episode-reviewed.

## Validation

- **455 passed, 7 skipped** (`python scripts/run_tests.py -q`). Regression coverage verifies
  downstream output-aware resume, narrow V28-to-V29 terminal-review recovery, automatic Run
  preview, Pause/Resume control state, and automatic EFM/retime follow-up routing.
- Compileall and warnings-as-errors imports passed; generated runbook/ASCII checks passed;
  PowerShell AST parsing passed for all 7 script `.ps1` files plus root `preflight.ps1`;
  `git diff --check` passed (only expected LF-to-CRLF working-copy notices).
- Windows process-tree pause/resume smoke test passed using a temporary sleeping process. The
  actual S01E18 job was checked read-only: its completed outputs and waiting-review evidence
  match and it is resumable to that review without rerunning Orc.
- No workflow stages or media processing were run while diagnosing or implementing this fix.
- Production `main` and W remain V15; this is app-branch only.
