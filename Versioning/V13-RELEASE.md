# Workflow Version 13 release notes

V13 is an authoring-side core workflow release from V12. `WORKFLOW_VERSION` is 13.
Episode-specific evidence remains in the authoring-device workflow log, not in this release
record, and is not live measurement or instruction data.

## Included

- Made `measure_break_edges.py` fail closed for empty clipped windows and `None` match
  results. It reports unresolved edges and exits nonzero; no retime offsets are inferred.
- Enforced exact, unique cut-PAD frame inventories. DVD-backed cut PADs require a reviewed,
  per-device DVD still with mapping, source-DVD hash, and color-match provenance; RIFE and
  hold behavior remain separated by cut/no-DVD policy.
- Stabilized the per-machine device workspace suffix outside the release tree, with explicit
  handling for ambiguous/unverifiable legacy folders and no automatic moves/deletions.
- Accepted importable PEP 440 target-or-newer direct package versions while keeping Python
  runtime pins exact and package import failures blocking.
- Improved OpenCV failure diagnostics and remove `DYLD_LIBRARY_PATH` only for the macOS QC
  replacement-identity child process.
- Added cut-aware A/V windows, Orc output streaming/heartbeats and verified checkpoint resume,
  and bounded exact-index frame extraction for analysis output.
- Added and tested report-only fade-midpoint and EFMSink table helpers. Added the work-ahead
  checklist and made transfer-time prep requirements explicit in the live README/runbook.

## Validation

- **165 passed, 7 skipped** (172 tests total).
- Python compilation, warning-as-error imports, generated-runbook checks, and `git diff --check`
  passed.
- The EFMSink table reader was also exercised read-only against W's five preserved S01E11
  text reports. No audio/video media was opened or decoded for that check.
- W's device log documents its S01E09 act-cut patch passing QC run 3 (13 pass, 1 known warning,
  0 fail). The V13 implementation was only synthetically tested here; it was not run on episode
  media.

## Limits and held evidence

- No real episode media was processed for V13 validation. S01E12 break offsets remain null and
  retiming remains blocked pending independent edge evidence.
- W's S01E13 logs record a verified transfer and stack render, while its manifest also contains
  non-null retime entries. Their supporting edge/adjudication artifacts were not verified in
  this release; see the authoring-device workflow log. No S01E13 values were copied or changed.
- DVD still extraction/cropping/color matching remains operator-prepared. A generated Mac `.vpy`
  was not evaluated with the target VapourSynth runtime. V13 makes no new macOS/Linux production
  support claim; Linux remains unsupported.
- `fullres_check.py` repeated-decode optimization remains deferred.

See `WORKFLOW_VERSIONING.md` and the authoring-device
workflow record. The V13 queue remains non-operative for live episode work.
