# ADR 0005 — Finished steps are kept across jobs, and after unchanged re-runs

Date: 2026-11-24 · Status: accepted (owner request: "make sure stages completed are being saved / aren't being
rerun when the app is restarted / a session resumes")

## Context
A job's state is saved after every step, and resuming the *same* job keeps every passed step whose input and output
fingerprints still match. Two gaps redid finished work:

1. **New jobs started from nothing.** Run resumed an unfinished job only when its plan matched exactly. Any change —
   an app update adding or removing a step (ADR 0004 removed two), a moved disk estimate, a finished job — made a
   new job that ran every step again (Legion 2026-11-24 13:20: cadence and DVD alignment redone).
2. **A re-run upstream redid everything after it**, even when it produced exactly the same files and values
   (Flex 2026-11-24 14:20: stack-render ran again, gave the identical master, and 32 min of cadence ran again).

## Decision
1. **Carry-over.** A new `intake-…` or `delivery-…` job takes over, step by step, the finished steps of the earlier
   job of the same kind that offers the most, when for each step: same stage and parameters (estimates excluded),
   its upstream steps taken over too, its input and output fingerprints still matching, the stage's own
   `still_current` check passing, its review (if any) still approved for those outputs, every manifest value it
   committed still its own, and every artifact it registered still the one it registered (for older states: the
   manifest's file is one of the step's own fingerprinted outputs). Jobs made to redo something (re-encode, A/V
   round, redo, previews) never take anything over. Stages that fill their own gaps whenever planned
   (`archive-download`, `efm-decode`) opt out (`Stage.carry_over = False`) and are planned again.
2. **Early cut-off.** When a step goes stale, the result its descendants were made from is recorded with them
   (output file contents and committed values). If the re-run gives exactly that back and the descendant's own
   files, checks and review still hold, the descendant is kept (`step-kept`) instead of run.

Anything that cannot be proven runs, as before: the rule only ever skips work it can show is unchanged.

## Consequences
- Updating the app no longer means redoing an episode from the stack onward.
- A step that was running when the app closed still runs again from its start (no mid-step checkpoints); that is
  the remaining cost of an interrupted run.
- Outputs whose file names carry a time stamp (the IVTC scripts) change on every re-run, so early cut-off does not
  apply after such a step re-runs; their descendants run.
