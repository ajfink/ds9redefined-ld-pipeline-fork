# FromStunToKill app branch - V27 existing-manifest intake retry

V27 addresses the follow-up UI path after intake has already initialized an episode. Clicking
Preview again could attempt to save a second intake plan targeting the same manifest, which is
correctly rejected to prevent overwrite but did not guide the operator to the next stage.

## Changes

- When intake-plan creation is refused because the manifest already exists, the GUI now explains
  that intake must not be repeated and offers to build a fresh next-stage plan from the existing
  manifest. This only creates/previews an immutable plan; it does not run workflow stages.
- When building that plan from the editable intake form, use its current episode, series, profile,
  manifest, instance, and fingerprint-policy values rather than stale values from an older plan.
- Declining the prompt leaves the manifest untouched and directs the operator to **Build next
  stack plan**.

## Validation

- **447 passed, 7 skipped** (`python scripts/run_tests.py -q`); the new callback regression
  verifies the existing-manifest prompt and that current form values are used instead of stale
  plan values.
- Compileall and warnings-as-errors imports passed; generated runbook/ASCII checks passed;
  PowerShell AST parsing passed for all 7 script `.ps1` files plus root `preflight.ps1`;
  `git diff --check` passed (only expected LF-to-CRLF working-copy notices).
- No media processing, manifest overwrite, or workflow-stage execution was performed.
- Production `main` and W remain V15; this is app-branch only.
