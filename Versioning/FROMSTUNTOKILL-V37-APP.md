# FromStunToKill app branch - V37 verified cadence evidence and visual review

V37 completes the cadence evidence and visual-review item in
`Versioning/NEXT-APP-RELEASE.md`.

## Trim prerequisite

- Formalized `video.trim_review` in the episode manifest template.
- Added an in-app head/tail review window. For DVD episodes it shows paired,
  hash-bound samples at the measured DVD-origin and content-end neighborhoods;
  for explicit no-DVD episodes it opens the verified LD master. The operator
  enters already adjudicated head/tail values in the 23.976 output-frame domain
  and confirms independent picture/audio review. The app does not convert source
  frame numbers, DVD timestamps, or audio samples into trim values.
- `record_video_trim_review.py` requires current selected-EFM provenance, measured
  PAD, exact source/evidence hashes, a human note, and explicit no-DVD approval
  where relevant. It records the trim marker with a manifest lock, backup, and
  atomic replacement.

## Cadence chain and review

- Added an allowlisted Windows stage that emits a unique `.Trim(audio.pad_frames,0)`
  AviSynth TFM script using the configured loader, requests a new full-side TFM log,
  and fully decodes the result through FFmpeg's null muxer.
- Native source frames are measured with FFprobe and must match `stack.frames`.
  FFmpeg decode count and TFM rows must both equal the exact pad-trimmed count; the
  TFM frame indices must be contiguous and zero-based. Incomplete logs, decode logs,
  AVS files, and an `INCOMPLETE` report are preserved but never accepted as evidence.
- The existing motion helper now has a sequential FFmpeg Windows backend. It
  measures mean absolute frame differences in source bit-depth units, emits exactly
  N−1 values after the measured PAD trim, and verifies the source count/hash.
  `build_map.ps1` remains the deterministic cadence-map emitter and receives only
  the measured pad/head/tail values and verified TFM/motion artifacts.
- `cadence_analysis.py` emits structured period-5 agreement, unmatched/combed
  counts/spans, insert-density regions, repair-set size, motion windows, and flagged
  review intervals directly from the complete TFM log. No console scraping or
  guessed confidence cutoff is used.
- The review UI displays metrics and a period plot, plus side-by-side raw/TFM
  playback samples for every flagged interval and representative clean intervals.
  Frame-rate disagreements require explicit acknowledgement. Approval requires
  every generated sample to be marked acceptable; any unresolved sample blocks
  cadence recording.
- `record_cadence_measurements.py` rechecks trim review, EFM provenance, TFM coverage,
  motion, map, report, and visual asset hashes; it takes a manifest lock, preserves
  a backup, and atomically writes only metrics derived from the verified TFM log.
  PAD repair/final encode validators require the current cadence approval.

## Validation

- **555 passed, 7 skipped** (`python scripts/run_tests.py -q`).
- Compileall, warnings-as-errors imports, ASCII companion check, stage CLI checks,
  and `git diff --check` passed.
- Synthetic tests cover TFM parsing/coverage, sequential FFmpeg motion values,
  trim/cadence recorders, and visual-index integrity. No episode media was processed.
- The Windows AviSynth/TFM production path and interactive GUI were not exercised.
  Production `main` and W shared code remain V15; no code sync was performed.
