# FromStunToKill app branch - V32 version-tolerant workflow resume

V32 allows an existing workflow to resume after `WORKFLOW_VERSION` changes, without weakening
the checks that protect the saved plan, profile, step sequence, or declared evidence.

## Changes

- Removed workflow-version equality as a resume blocker. Schema, job/episode identity, plan and
  profile digests, fingerprint policy, and ordered step identity/type/stage are still validated.
- On a version mismatch, the workflow records the from/to versions and UTC resume time in the
  durable workflow state and appends a handoff entry to the workflow log, then records the current
  version as active. Failed evidence checks and terminal rejected jobs remain blocked as before.
- Resume from a loaded saved run now starts `--execute --resume` directly, without requiring a
  separate GUI preview. The workflow runner still validates the immutable plan and saved state;
  Run on an unpreviewed plan continues to preview before execution.
- Each stage attempt records pre-attempt output fingerprints. Resume skips and verifies prior PASS
  stages, then retries the interrupted/failed stage only if its inputs and outputs still match the
  saved evidence. Jobs missing that baseline, with changed outputs/inputs, or a changed profile are
  omitted from the recovery picker instead of being offered as resumable options.
- Split the Plan and run controls across two rows. Moved the console to a separate window that
  initially opens beside the main window at the same height; the main-window toggle hides/restores it.
- Loaded the root `fstk icon.jpg` as the GUI window/taskbar icon, with a console diagnostic if the
  image or Pillow/Tk support is unavailable. Pillow is already a pinned app dependency.
- Archive intake now treats the profile download folder as a default suggestion. A user-selected
  writable destination survives episode resolution, is stored as that episode's `roots.downloads`
  override, and is reused by later plans/stages. Manifests, plans, logs, work, and generated outputs
  remain in the normal device/episode workspace; protected parts of the shared release stay unwritable.
- **Run** first resumes the latest eligible job for the selected episode/workspace. If no incomplete
  job remains but a verified Orc reference-selection result exists, it reads the report as workflow
  evidence without opening it for user review. A plan stage copies the exact Orc `alignment_map` and
  the sum of the mutually exclusive 1/2/3+ source-coverage counts into the manifest with a backup;
  the app then automatically builds and runs the stack-render plan using a measured-frame-based
  space budget. The rendered stack still pauses for its separate quality review. If the saved Orc
  report is unusable, the app restarts reference selection with a timestamped backup; conflicting
  manifest measurements or unverified outputs remain protected. The explicit restart action remains.
- Retired the moved `V*-QUEUE.md` planning files and `Versioning/changelog.txt` from the app
  branch. Kept the runbooks: they remain the technical operating procedure, while the generator,
  ASCII companion generator, README, and optional OpenCode help still use them during development.

## Validation

- **488 passed, 7 skipped** (`python scripts/run_tests.py -q`), including safe retry from the last
  confirmed step, non-resumable recovery-state, completed reference/render discovery, restart
  overwrite backups, direct GUI Resume, console placement, and the end-to-end V31-to-V32 handoff.
- Compileall, warnings-as-errors imports, generated runbook/ASCII companion checks, PowerShell AST
  parsing for all 7 script `.ps1` files plus root `preflight.ps1`, and `git diff --check` passed.
- The final rerun confirmed **488 passed, 7 skipped**; automatic Orc evidence recording/continuation,
  latest-job auto-resume, selectable download destinations, explicit restart, and retired-file
  boundary tests are included.
  No queue/changelog references remain in live docs or generated runbooks.
- UI transition tests are automated; the windows were not launched for an interactive visual check.
- No media processing or workflow stages were run. Production `main` and W remain V15; this is
  app-branch only.
