# FromStunToKill app branch - V35 report-bound A/V alignment samples

V35 completes the lightweight alignment-sample item in
`Versioning/NEXT-APP-RELEASE.md`.

## Changes

- Added the allowlisted `alignment-samples` stage and automatically run it after
  the report-only audio evidence suite.
- Generate short, low-resolution decision clips from the runbook-primary fade
  fit's representative and largest-residual native-WAV/master pairs, plus
  picture/audio windows explicitly flagged as low-correlation or at a search edge.
  No confidence threshold was inferred.
- Preserve source anchors, report hashes, candidate slope, preview-only rate
  transform, source/output hashes, and measured A/V timestamps/durations in an
  immutable index. The workflow fingerprints the entire sample directory.
- Use DVD-domain fit-B pairs only when an exact visually approved, hash-bound
  alignment maps those timestamps into master time. Otherwise report the reason
  they were deferred.
- The GUI shows candidate labels and exact WAV/master (and DVD where applicable)
  times with a Play control. These are review aids only: they do not update any
  manifest field or production audio output.

## Validation

- **520 passed, 7 skipped** (`python scripts/run_tests.py -q`).
- Compileall, warnings-as-errors imports, ASCII companion check, stage CLI
  registration, and `git diff --check` passed.
- A real FFmpeg smoke test generated synthetic video/audio, built an A/V clip,
  and verified its streams/timestamps/duration. No episode media processing or
  interactive GUI test was performed.
- Production `main` and W shared code remain V15; no code sync was performed.
