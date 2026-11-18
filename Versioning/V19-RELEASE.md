# Workflow Version 19 - app-branch command-line progress

**From Version 18 to Version 19 on the app branch only.** The production `main` and W tree
remain V15; this change is not a production sync.

## Progress display

Every executed plan step now appears in an ASCII progress bar, including review steps. Updates
show completed/total plan steps, percentage, the current stage/review, elapsed time, seconds since
child output, and total workflow time. The bar emits on step transitions and periodically during a
quiet or long-running child stage. Interactive terminals update more frequently; redirected
output gets lower-frequency heartbeat lines.

This percentage is **plan-step completion only**, not a claimed percentage inside FFmpeg, Orc,
RIFE, or another child process. It does not change stage execution, cancellation, retry, or review
behavior. Review waits remain visibly paused until the user resumes/approves.

## Validation and limits

- Full suite: **393 passed, 7 skipped** (400 tests). Compile/import checks, generated runbook
  check, all 7 PowerShell AST parses, and `git diff --check` passed.
- Tests cover progress formatting/timing, per-stage output, and human-review wait status.
- No real-media processing or GUI smoke test was performed for this version.
