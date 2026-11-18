# Workflow Version 22 - prioritize final A/V alignment

**From Version 21 to Version 22 on the app branch only.** Production `main` and W remain V15.

## Decision

The user clarified that final audio alignment is the goal and internal commercial-break length
matching is not necessary. The workflow therefore keeps the LD's internal picture/audio timeline
and uses the existing piecewise DVD 5.1/SRT retimer to map those tracks onto the LD timeline.
Final delivery A/V QC—not equal black/fade durations—is the acceptance criterion.

V21's DVD-reference policy remains for episode head/tail boundaries: measure the DVD's visual and
audio fade points separately, map the corresponding LD picture frames and audio samples, preserve
the measured DVD-relative A/V offset, and remove LD-only front matter/tail only when supported by
the measured common timeline. This can remove copyright intro material and establish the correct
origin without changing internal LD breaks.

The cadence workbench no longer suggests internal midpoint LD cuts. DVD break-duration differences
alone do not trigger a picture/audio edit or change to `retime.breaks`.

## Validation and limits

- Full suite: **403 passed, 7 skipped** (410 tests). Compile/import, generated-runbook, all 7
  PowerShell AST parses, and diff checks passed.
- No real-media alignment, retime, final QC, or GUI smoke test was performed for this clarification.
