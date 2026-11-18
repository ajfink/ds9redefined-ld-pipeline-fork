# FromStunToKill app branch - V36 DVD/LD visual alignment adjudication

V36 completes the DVD/LD alignment and visual boundary-adjudication item in
`Versioning/NEXT-APP-RELEASE.md`.

## Changes

- After verified EFM selection, automatically plan and run master/DVD descriptor
  extraction, continuity-constrained alignment, and visual-review asset generation
  when a DVD reference exists. All artifacts use unique probe outputs; DVD video
  decode remains sequential.
- Review assets bind the source videos, descriptors and their metadata, path, TSV,
  manifest, and sample hashes. Exact high-resolution source-frame extracts are
  accepted only when their 48x36 gray descriptors match the corresponding candidate
  descriptor rows. Local-offset GIF samples do not extrapolate outside source or
  descriptor coverage.
- The GUI displays the offset-vs-time path, low-match/range-edge markers, source
  frame-rate metadata, paired contact sheet, and animated samples at weak/edge/
  transition/anchor points. A frame-rate mismatch requires an explicit checkbox.
- Added explicit APPROVED/REJECTED decision recording. Approval updates the selected
  alignment and descriptor paths plus `video.dvd_alignment_review` using a manifest
  lock, exact input/index hashes, atomic replacement, and verified backup. Rejection
  writes an audit record without modifying the manifest.
- After approval, the app reruns eligible report-only audio evidence. The retime
  preparation helper and GUI planner now refuse unapproved, changed, or stale
  alignments/review assets.
- The report-only retime candidate chain now creates short sequentially decoded DVD
  audio auditions around projected intervals and silence matches. The existing
  candidate UI shows structured interval/audio evidence and nearest paired-frame
  context, with in-window GIF playback and DVD audio audition. It can batch-select
  only globally unique pairs after explicit confirmation and a required review note;
  ambiguous, collided, uncovered, and unmatched candidates remain unselected.
  Offset, step, and gap fields remain unset.

## Validation

- **537 passed, 7 skipped** (`python scripts/run_tests.py -q`).
- Compileall, warnings-as-errors imports, generated-runbook/ASCII checks, stage CLI
  checks, and `git diff --check` passed.
- FFmpeg integration tests used generated synthetic video/audio only; they verified
  descriptor-exact frame extraction, paired visual assets, and sequential DVD-audio
  excerpts. Recorder tests covered rejected reviews leaving the manifest unchanged
  and approved hash-bound updates with backup.
- No episode media processing or interactive GUI session was performed.
  Production `main` and W shared code remain V15; no code sync was performed.
