# FromStunToKill app branch - V28 one-click existing-manifest retry

V28 refines the existing-manifest recovery path: a single Preview click now saves and previews
the fresh next-stage plan, removing the extra confirmation and second Preview click.

## Changes

- Existing-manifest intake refusal is handled inline without an alert popup. The app builds a
  fresh next-stage plan using the current form values and immediately previews it. Preview does
  not execute; the operator must still choose Run.
- If a prior plan for the same series-qualified episode contains the `stack-reference` stage,
  the new immutable plan is saved under the next unused filename in the same plans folder and
  includes: `Note - existing reference plan for this episode already exists at <LOCATION AND FILENAME>.`
- Existing manifests and plans remain untouched. New plans record the full path to the detected
  prior reference plan.

## Validation

- **448 passed, 7 skipped** (`python scripts/run_tests.py -q`); regression coverage verifies
  immediate plan preview, current-form values, prior-plan annotation, and series/stage matching.
- Compileall and warnings-as-errors imports passed; generated runbook/ASCII checks passed;
  PowerShell AST parsing passed for all 7 script `.ps1` files plus root `preflight.ps1`;
  `git diff --check` passed (only expected LF-to-CRLF working-copy notices).
- No manifest overwrite, media processing, or workflow-stage execution was performed.
- Production `main` and W remain V15; this is app-branch only.
