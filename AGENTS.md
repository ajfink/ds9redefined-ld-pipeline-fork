# AGENTS.md — handoff guide for anyone (human or agent) continuing this work

Read this file first, then `docs/DEVLOG.md` (newest entry at the top), then `docs/REFACTOR-PLAN.md`.

## What this is

`fstk` (FromStunToKill) is a clean-sheet rebuild of the v38 LaserDisc-to-video pipeline
(Star Trek LD captures → stacked, IVTC'd, PAD-repaired, audio-synced deliverable, optionally
referenced against a DVD). The **goal is output quality**: local CPU time does not matter, and
replacing or improving components (including Orc) is allowed.

## Hard rules

1. **v38 is a frozen, read-only reference** (owner decision 2026-11-21). The reference is commit
   `035ecc6` on branch `fromstuntokill-v15-app` of `Z:\ld-pipeline` (worktree
   `Z:\fromstuntokill-v15-app`), plus the working-tree snapshot
   `.fstk-local/v38-now-20261121.tgz` (sha256 `8a010d58…7dac`). See `docs/v38-reference.md`.
   Never modify, commit to, or attach either tree. Behaviour differences found later are judged
   against that reference, not against whatever the trees contain now.
2. **Commits.** The main repository is `W:\FromStunToKill` (branch `main`; the same folder is what the launchers run
   from, see `INSTALL.md`). `Z:\FromStunToKill` is the **backup copy**: it receives `git push backup main` from W:
   (`receive.denyCurrentBranch=updateInstead` there), so never commit in Z: directly. Agents commit in W: after each
   verified sync, with the session trailers; they never rewrite history and push to GitHub only when the owner has
   said so.
3. **Measured, not assumed.** Unknown values stay unset. Never default, never copy another
   episode's numbers, never infer a convention the runbook does not define. See the ten
   invariants in `docs/REFACTOR-PLAN.md` §2; each must end up enforced by code + a test.
4. **Validate artifacts, not exit codes.** Every tool call is followed by an artifact check.
5. **Fail closed.** Unparseable or skipped input fails the check; report expected vs checked.
6. **No device data in the code tree.** Profiles, manifests, logs, and media live under the
   profile's `data_root` / `media_root`. Test fixtures copied from devices are redacted.
7. **Log your work** in `docs/DEVLOG.md` (see "Logging" below) before you stop.

## Decisions already made (do not reopen without the owner)

- UI: **PySide6** (optional extra `fstk[gui]`), over a service API; the UI never edits manifests directly.
- **App first, CLI for scripting.** OpenCode/Qwen runbooks are retired.
- **VapourSynth on every platform.** AviSynth and the PowerShell scripts are retired.
- In-flight v38 episodes (S01E12/17/18/20) get **imported** after the workflow engine lands;
  v38 approvals come in as historical and must be re-confirmed.

## Layout

```
src/fstk/
  core/       errors, identity, hashing/fingerprints, atomic IO + locks, time, redaction
  config/     machine profile (typed, v38 converter)
  manifest/   field registry, Manifest model, journaled store, v38 import/export
  tools/      process runner + external tool adapters (ffmpeg/ffprobe/orc/vspipe/rife/7z)
  media/      pure algorithms (no I/O side effects)
  stages/     declared stages: inputs, outputs, gates, plan/run/verify
  evidence/   review assets + approval records
  workflow/   DAG engine, state, resume
  workspace/  episode workspace, space gate, shared tree
  ui/         PySide6 app
  cli/        `fstk` entry point
tests/{unit,characterization,integration,fixtures}
tools/v38_inventory.py   → docs/v38-inventory.md (v38 file → new home + status)
docs/  REFACTOR-PLAN.md, DEVLOG.md, adr/, v38-inventory.md
```

## Working conventions

- Python ≥ 3.12, `pip install -e .[dev]` (add `[gui]` for the app).
- Run `ruff check src tests tools` and `pytest -q` before you hand off; both must be clean.
- Characterization tests port v38 tests (or real v38 data) to pin behaviour **before** improving it.
  When you deliberately change v38 behaviour, say so in the test name/docstring and in DEVLOG.
- Every manifest field is declared in `fstk/manifest/registry.py`; never write ad hoc keys.
- Keep `tools/v38_inventory.py` MAP statuses honest (`planned` → `partial` → `ported`), regenerate
  the inventory with `python tools/v38_inventory.py <v38-tree>`, and check with `--check`.
- New architectural decisions get an ADR in `docs/adr/NNNN-title.md`.

## Logging

`docs/DEVLOG.md` is the running development log, newest session first. The newest session's
**"Plan and progress"** table is the current work queue: start there, and update it at every checkpoint. Each session entry has:
date, agent/person, goal, what was done (files/modules), test status (counts), decisions made
(with reasons), known gaps/risks, and **next steps** precise enough for a cold start.
Small decisions go in the DEVLOG; anything structural also gets an ADR.

## Syncing a dev container to the owner's machine

Session 1 developed in a cloud container and synced to the repo (since CP32: `W:\FromStunToKill`) by writing one
tarball to the gitignored `.fstk-local/fstk-sync.tar` and extracting it in place with
`tar --overwrite` (the device bridge cannot delete files, so nothing is ever removed this way;
renamed/deleted files must be cleaned up by the owner). **Always compare the tarball's SHA-256
on the device with the source before extracting** — one sync silently delivered the previous
tarball. `.fstk-local/v38src.tar` is a read-only snapshot of the v38 tree for reference.

## Environment notes (from the first session)

- Development so far ran in a Linux container (Python 3.13, ffmpeg present; no vspipe/orc/Windows).
  Windows-specific behaviour (file sharing violations, process trees, UTF-16 logs) is covered by
  tests that simulate it; real Windows runs are still owed.
- The v38 suite baseline in that container: 512 passed, 11 skipped (13 Tkinter test modules
  cannot load without tkinter).
