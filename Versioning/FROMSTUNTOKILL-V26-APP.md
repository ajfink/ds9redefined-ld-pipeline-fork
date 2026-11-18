# FromStunToKill app branch - V26 stack-reference report path fix

V26 fixes a mismatch between the stage-console plan and the stack-reference selector. The plan
already supplied `--report-out` and fingerprinted that report, but
`scripts/select_stack_reference.py` did not recognize the option, so execution stopped in
argument parsing before reference selection began.

## Changes

- Added `--report-out` to the selector CLI and used the requested path for its selection report;
  existing direct invocations still default to a uniquely named report under the episode/device
  logs folder.
- Restricted requested reports to new, non-symlink files directly inside the configured probe
  folder and applied the shared device-write-path guard.
- Added regression coverage for CLI parsing, safe report-path validation, and the generated
  stack-reference plan's report output.

## Validation

- **446 passed, 7 skipped** (`python scripts/run_tests.py -q`); focused stack-console, selector,
  and workflow-runner tests passed.
- Compileall and warnings-as-errors imports passed; generated runbook/ASCII checks passed;
  PowerShell AST parsing passed for all 7 script `.ps1` files plus root `preflight.ps1`;
  `git diff --check` passed (only expected LF-to-CRLF working-copy notices).
- The reported invocation stopped at argparse before selection ran; no Orc selection or media
  processing was run for this fix.
- No media processing, dependency installation, or changes to stack-selection or review gates.
- Production `main` and W remain V15; this is an app-branch-only fix.
