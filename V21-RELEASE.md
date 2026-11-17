# Workflow Version 21 - DVD-referenced boundary alignment

**From Version 20 to Version 21 on the app branch only.** Production `main` and W remain V15.

## Boundary policy

- With a matching DVD, use it as the reference for the opening and ending visual and audio
  points. Measure DVD picture fades/black and DVD audio fades/envelopes separately; map the
  corresponding LD picture points to the output-frame domain and LD audio points to native
  samples. Preserve the DVD's measured relative picture/audio offset rather than forcing the
  two fades to coincide. Never copy raw DVD frame counts, timestamps, or samples to LD.
- Recompute LD head/tail trims, `video.delta_s`, `AudioSkip`/`audio.skip_s`, endpoint sample
  budgets, frame count/duration, and DVD 5.1/SRT retiming after a boundary change. A source-edition
  mismatch or ambiguous matching point remains a human stop.
- For midpoint commercial-break fades, the editorial candidate is conditional: trim LD excess
  only if the same mapped DVD break is measurably shorter; retain an equal or longer DVD interval.
  **Current pipeline limitation:** it has no internal LD picture/audio cut map. The cadence
  review and runbook now mark a shorter-DVD break as unresolved; they do not apply an LD cut or
  misrepresent `retime.breaks` (which only governs DVD-track timing).
- Added the DVD boundary policy and relevant evidence inputs to the app cadence-plan review.
  No manifest keys or media transforms were added.

## Validation and limits

- Cleanup/report-only boundaries are unchanged from V20.
- Full suite: **403 passed, 7 skipped** (410 tests). Compile/import, generated-runbook, all 7
  PowerShell AST parses, and diff checks passed.
- The app cadence-plan test verifies the DVD boundary policy appears only for DVD-backed episodes
  and that midpoint cuts remain marked unsupported.
- No episode media was processed and no GUI smoke test was performed for V21.
