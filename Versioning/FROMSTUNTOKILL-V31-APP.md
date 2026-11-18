# FromStunToKill app branch - V31 detailed-report review policy

V31 prevents repeat human approvals for identical evidence and skips detailed report-only review
gates while retaining the visual/operator choices that consume those reports.

## Changes

- A human-approved review can be reused by a new workflow job only when the series-qualified
  episode, profile digest, review step ID, prompt digest, and exact input fingerprints match.
  The new state records the source job and original approver. Different evidence or prompt still
  requires review. Previously passed reviews in the same job continue to be fingerprint-verified
  and skipped.
- Automatically skip `review-reference-selection` and `review-retime-break-candidates`, recording
  policy reasons and `reviewed_by_human: false`. The retime report still opens automatically in
  its dedicated UI for classification; rendered-stack, PAD classification, and delivery approvals
  remain explicit human gates.
- The stack-reference skip does not infer `alignment_map` or `stack.frames`; they remain required
  measured manifest values before rendering. Retiming candidates still open in the dedicated
  review UI, where episode-specific classifications/recording remain operator-controlled.
- The GUI watches for stable EFMSink WAV/report output and a matching success log, then creates and
  runs the provenance-verification plan. Once every capture verifies, it auto-selects only a
  unique Pareto-dominant report across the recorded error/loss metrics; ties or missing metrics
  use the manual fallback selector.
- A successful full-resolution PAD check opens cut classification; RIFE completion opens the
  interpolated-frame visual review. After visual approval, padfix and non-encoding source
  validation start automatically, then the delivery workbench opens for its explicit space/mode
  inputs.

## Validation

- **467 passed, 7 skipped** (`python scripts/run_tests.py -q`); tests cover report-gate auto-skips,
  exact-evidence approval reuse, EFM output watching/ranking, and automatic visual PAD follow-up.
- Compileall and warnings-as-errors imports, generated runbook/ASCII companion checks, PowerShell
  AST parsing for all 7 script `.ps1` files plus root `preflight.ps1`, and `git diff --check` passed
  (only expected LF-to-CRLF working-copy notices).
- No media processing or workflow stages were run for this update. Callback tests exercise the
  recovery and automatic transition logic; an interactive GUI startup/visual review test remains
  pending because no desktop GUI test was available.
- Production `main` and W remain V15; this is app-branch only.
