# Workflow Version 12 release notes

V12 adds the Episode 12-origin LD/DVD audio comparator as an **optional, report-only
diagnostic**. It supplements, but does not change or replace, the measured audio-rate and
approval workflow.

## Included

- Added `scripts/ld_dvd_audio_compare.py`, a reusable episode-parameterized comparator for
  LD/DVD per-second envelope lag, a bounded aligned envelope-difference summary, and
  whole-source band-energy summaries.
- The tool resolves WAV/DVD inputs from the manifest by default, honors the manifest DVD audio
  selector (with an explicit `0:a:1` fallback), decodes DVD audio sequentially, and supports
  isolated command-line overrides.
- Report schema v3 explicitly records selector provenance, lag sign, band-analysis seconds, and
  the fact that whole-source band means are same-start/unshifted—not lag-corrected. The S01E12
  prototype's earlier v2 labeling correction is retained.
- Reports refuse overwrite, validate shared-mode write destinations, redact known device-local
  paths, and are checked for valid JSON after redaction.
- Added a sanitized numeric E08 regression fixture and 12 tests covering lag sign/alignment,
  historical E08 metrics, synthetic band energy, selector selection, input errors, sequential
  DVD decoding, redaction, no-overwrite output, and report-only/no-manifest-mutation behavior.
- Added optional runbook instructions for invoking the diagnostic after native LD audio and
  DVD inputs are available. It is not a workflow gate and does not make an audio decision.

## Validation and limits

- **116 passed, 7 VapourSynth-only skipped.** The 12 comparator tests, Python compilation,
  warning-as-error import, generated-runbook check, and `git diff --check` passed.
- Tests use synthetic signals and sanitized numeric data only. No E12 media run, real DVD decode,
  or episode processing was performed for this release.
- The current implementation loads full decoded sample arrays. For E08-sized inputs, the two
  retained mono `float32` arrays alone are about 923 MiB; conversion and FFT temporaries add to
  peak use. Peak RSS was not benchmarked. Keep this diagnostic optional and run it only where
  sufficient memory is available; streaming extraction is a possible future improvement.
- LD Dolby Surround and DVD AC-3 are different mixes. Envelope lag is corroboration only—not a
  sample-rate fit. Band means are not lag-aligned. Do not use the report to select `asetrate`,
  approve a rate, edit a manifest, or satisfy a QC gate.
- Linux production remains blocked. V12 makes no new macOS or end-to-end support claim.

See `V12-QUEUE.md`, `WORKFLOW_VERSIONING.md`, `changelog.txt`, and authoring-device execution
log section 45.
