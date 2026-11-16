> **NOT FOR LIVE WORKFLOW USE.** This is a historical release-planning record only. Do not read,
> cite, or apply its contents as episode instructions, measurements, approvals, stage
> arguments, or gates. Active jobs follow the current released README, runbook, and tools.

# V12 planning record

**Current released version is V12.** This record is not live workflow input. The comparator
proposal was accepted for V12 as an optional report-only diagnostic; it does not select a
sample rate, approve an audio decision, edit a manifest, or become a QC gate. See
`V12-RELEASE.md` for scope and validation.

## 1. S01E12 comparator proposal — ACCEPTED IN V12

### Evidence and current preparation

- The parameterized prototype and its E08 regression originated during the S01E12 transfer.
  The S01E12 helper record says the planned E12 invocation was blocked until its DVD and LD
  audio artifacts were available. The E08 synthetic/numeric regression is not an E12 media test.
- V12 incorporates the v2 correction: same-start band means are labeled unshifted/not
  lag-corrected. The v3 report records DVD selector provenance, the lag-sign convention, and
  the number of band-analysis seconds. A sanitized E08 fixture is used; no media, local paths,
  one-off script, or episode report is part of the release.
- Report output is report-only, no-overwrite, shared-path-redacted, and checked against the
  active shared-workspace writable-path boundary. DVD decoding remains sequential.

### Remaining review note

The helper loads full decoded sample arrays while computing RMS/FFT features in blocks. Peak
memory was not benchmarked. The helper remains optional and must only be run where sufficient
memory is available; a future memory benchmark or streaming implementation may be considered
separately. V12 makes no quantified memory-bound or cross-platform support claim.

### V12 validation record

- Twelve comparator unit/synthetic tests cover the sanitized E08 numeric regression, positive
  and negative lag signs, aligned-window pairing, band labels/means, synthetic band-energy
  assignment, selector precedence, missing manifest audio input, unsupported WAV format,
  sequential DVD decode/no `-ss`, malformed PCM, report redaction/no-overwrite, and a CLI-level
  report-only/no-manifest-mutation check.
- Full suite, compilation, warning-as-error import, generated-runbook check, and diff checks
  passed. See `V12-RELEASE.md` for final totals.
- No E12 media was used. Do not infer macOS/Linux end-to-end support from these tests; a real
  optional diagnostic run remains operator-invoked and report-only.

### Exclusions

- Do not copy E08/E12 media, audio, episode reports, or machine-local paths into the repository.
- Do not add rate selection, manifest mutation, approvals, automatic execution, or QC-gate
  behavior to the comparator.
