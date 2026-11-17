# Workflow Version 17 — FromStunToKill app-branch release record

**From Version 16 -> Version 17 on the FromStunToKill app branch only.** This update reduces
per-PAD human review using the motion percentile already emitted by `pad_audit.py`. It does not
change the production `main` branch or W shared tree (both remain V15); it does not process media
or authorize a final encode/remux.

## PAD decision policy

- The existing non-black-triplet motion baseline produces `baseline_percentile` for each
  map-matched repair candidate. A candidate with a finite percentile **below 95**, not previously
  recorded as a cut PAD, and not above the full-raster residual threshold is automatically
  classified non-cut/RIFE.
- Human review is limited to P95-or-higher outliers, missing/non-finite baseline metrics, frames
  already recorded as cut PADs, and above-threshold full-raster residuals. Reviewers approve cuts
  or reject cut-like outliers as non-cut; cut decisions still require the applicable DVD still or
  explicit no-DVD hold policy.
- Prior-cut-to-RIFE corrections still require the exact warning:
  `ONLY generate as an interpolated frame if this pad frame was incorrectly identified as preceding a scene cut.`
- The decision report records which frames were automatic and which required human review. The
  recorder recomputes eligibility against the hashed audit/full-raster inputs and rejects any
  altered classification. Direct RIFE execution also verifies the decision/commit records, source
  hashes, policy, and exact cut inventory before processing. Below-P95 automatic decisions cannot
  overwrite a prior cut classification or bypass residual acceptance.
- A successful PAD recording run automatically builds and previews a fresh RIFE plan. A failed
  recording never continues; final encode/remux remains outside this continuation.

P95 is a screening boundary based on the measured episode baseline, not proof that every lower-
percentile candidate is visually in motion or that no scene cut exists. Missing or exceptional
metrics fail into the human queue rather than being treated as automatic evidence.

## Validation and limits

- Full suite: **371 passed, 7 skipped** (378 tests). Skips require the selected VapourSynth Python
  environment and were not runnable on this Windows host.
- `compileall` for `scripts/` and `tests/`, warnings-as-errors imports, `generate_runbooks.py --check`,
  all **7 PowerShell parsers**, and `git diff --check` passed. Git reported only configured
  LF/CRLF normalization notices.
- Tests use synthetic PAD audit and workflow fixtures. No real-media motion threshold calibration,
  RIFE run, Orc render, Mac runtime, encode, or remux was performed for this update.
- The P95 cutoff reuses the episode's measured baseline percentile; it is a conservative human
  review boundary, not proof that every lower-percentile frame is visually motion. Reconstructed
  episode evidence gaps remain listed in `V16-QUEUE.md`.
