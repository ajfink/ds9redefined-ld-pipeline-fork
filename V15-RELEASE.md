# Workflow Version 15 — release record

**From Version 14 -> Version 15.** V15 treats changelogs as retrospective records and adds a
series-aware, report-first retime-break candidate workflow plus verified retime-output fixes.
The queue is a planning/evidence record, not an operator instruction source. No episode media
was downloaded or processed to implement or validate these changes.

## Live-workflow contract

- `changelog.txt` and version/release summaries are not inputs to an episode job. Live runs use
  the released README/runbook, scripts, active manifest, measured evidence, and explicit user
  decisions. Missing/ambiguous operational information remains a STOP condition.
- `measure_break_edges.py` matches the before and after content windows independently. It
  preserves a valid side as partial evidence when the opposite side is clipped or lacks texture,
  but exits nonzero unless both required edges are measured. It never edits the manifest.
- `measure_ld_black.py` emits series-aware schema-v2 threshold-sensitivity reports with source
  descriptor SHA-256 and effective interval parameters.
- `prepare_retime_breaks.py` consumes that report, the active alignment, and sequentially scanned
  DVD 5.1 silence intervals. It requires explicit episode-specific thresholds/tolerances and
  outputs every possible pairing. It flags alignment gaps/transitions, ambiguous pairings, and
  many-to-one collisions; it does not pick a nearest candidate or write the manifest.
- `record_retime_breaks.py` defaults to preview. Recording requires selected labels, a reviewed
  report digest, exact `SERIES:SxxExx` confirmation, a review note, the current verified-inputs
  gate, and an unchanged manifest. It refuses collisions/ambiguity/existing break records,
  locks the manifest, creates a no-overwrite backup, atomically records interval candidates,
  and runs manifest validation. Offsets/steps/gaps remain null for the separate adjudication
  step. A merged-music entry requires explicit human selection.
- `run_stage.py` allowlists candidate preparation (stack-gated) and candidate recording
  (inputs-gated), retaining doctor and workspace context.
- `retime_dvd_assets.py` pins each post-`atempo` segment to its planned output duration and
  records the V15 generated-output silence-scan threshold in its report. It does not relax gap
  position/duration tolerances; sub-resolution gaps remain LIMIT.

## Validation

- Full unit suite: **224 passed, 7 skipped** (231 tests).
- Candidate tests use synthetic intervals and mocked metadata/FFmpeg process results. The
  retime pin tests include short FFmpeg-generated signals. The pin/threshold fix was also
  exercised on S01E12's existing media during development: output-gap verification
  **17/17 PASS** (3 FAIL before the fix) and `validate --stage retime` 35/0
  (device log section 51, S01E12 episode logs); no media was rerun as part of the
  release-validation suite itself.
- Python compilation passed (`compileall` over `scripts` and `tests`); V15 helper imports passed
  with warnings treated as errors. Generated Qwen runbooks were current, and a regenerated
  temporary plain-text companion matched the checked-in file byte-for-byte. All 7 PowerShell
  scripts parsed successfully. `git diff --check` passed; Git emitted only configured LF/CRLF
  normalization notices.

## Boundaries

- The one-off `break_candidates_s01e12.py` / `record_breaks_s01e12.py` scripts remain local
  evidence and are not shipped as reusable helpers.
- No workflow step automatically selects an episode-specific edge, threshold, offset, or merge
  decision. Ambiguous candidates remain blocked for review.
- Existing DS9/VOY identities and V14 manifests are unchanged; no migration or media operation
  is performed by V15 release validation.
- Device-local, NOT part of V15: S01E12's clip ends on an orphan single-frame black (output
  frame 65366, no map pad, genuine DVD-consistent black ending) that trips the release
  `pad_audit.py` boundary raise before any output is written. The episode used a documented
  local adaptation (`Z:\work\probe\pad_audit_local_boundaryfix.py`; device log section 52,
  episode log `S01E12-pad-audit-boundary.md`), proposed for merge into a future increment.
