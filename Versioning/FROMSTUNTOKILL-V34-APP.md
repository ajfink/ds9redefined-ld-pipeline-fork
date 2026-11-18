# FromStunToKill app branch - V34 audio evidence and no-DVD handoff

V34 completes the audio-evidence portion of the next-stage list while preserving
the authoritative hold on DVD-backed rate/origin promotion.

## Changes

- After verified EFM capture selection, automatically run all currently eligible,
  report-only audio timing diagnostics: duration/PAD, the primary native-WAV fade
  midpoint fit, and raw-provisional picture/audio correlation only when the
  DVD/LD alignment has a visually approved, hash-bound manifest marker.
- Present structured results and native-audio playback in a Tk evidence window.
  It never asks the user to open a JSON/TSV report and never edits rate/origin fields.
- Added a manifest-bound, no-overwrite plan builder for sample-accurate LD audio sync.
  It refuses to run until a nonnegative `audio.skip_s`, corrected WAV, measured
  `video.frames`, and measured duration exist.
- At `encode-validate`, automatically start the existing no-DVD duration/PAD
  promotion only when the manifest contains the user's explicit no-DVD approval and
  measured frame/head/tail prerequisites. It preserves the existing space and
  ±1-frame endpoint gates; manifest promotion now holds an exclusive lock and keeps
  its hash-checked backup/atomic commit.
- **DVD-backed hold preserved:** the canonical runbook supplies neither a numeric
  fit-agreement threshold nor an exact pad-corrected WAV0/intercept convention.
  The native-WAV fade fit is explicitly not pad-corrected evidence by itself.
  Therefore V34 does not create/promote `ld_corrected`, approve `audio.skip_s`, or
  claim an A/V origin for the DVD path. They remain unset pending explicit human
  review of the V35 A/V samples and reconciliation of the exact origin convention.

## Validation

- **509 passed, 7 skipped** (`python scripts/run_tests.py -q`).
- Compileall, warnings-as-errors imports, generated-runbook/ASCII checks, and
  `git diff --check` passed.
- Regression coverage verifies the report-only evidence bundle, raw-provisional
  geometry default, approved-alignment requirement, structured GUI summary,
  no-DVD promotion planning/dispatch, and refusal to build sample-synced audio
  without an approved measured AudioSkip.
- No real media audio processing or interactive GUI session was performed.
  Production `main` and W shared code remain V15; no code sync was performed.
