# FromStunToKill (fstk)

Clean-sheet rebuild of the v38 LaserDisc-to-video pipeline: multiple LD captures → Orc weighted
stack → EFM audio → measured cadence/IVTC with targeted repairs → PAD repair → single x265 encode →
remux → QC, optionally referenced against a DVD. **Output quality is the goal**; every measurement is
evidence-backed, every human decision is bound to the evidence it was made on, and nothing unknown
is ever defaulted.

Status: early development (see `docs/DEVLOG.md`). The v38 tree remains the production reference.

## Install and run
`INSTALL.md` — unzip the folder, start `FromStunToKill.cmd` (Windows), `FromStunToKill.command` (Apple Silicon Mac) or
`FromStunToKill.sh` (Linux); it checks dependencies, asks before installing, and starts the app.

## Start here
- `AGENTS.md` — rules and conventions for anyone continuing the work
- `docs/DEVLOG.md` — what has been built, decisions, and next steps (newest first)
- `docs/REFACTOR-PLAN.md` — plan, invariants, phases, owner decisions
- `docs/findings/` — quality findings about v38 behaviour that need owner decisions
- `docs/v38-inventory.md` — every v38 file → its new home and port status
- `docs/stage-reference.md` — generated reference for the registered stages

## Install (development)
```
python -m pip install -e ".[dev,gui]"      # includes soxr (exact-ratio audio resampling)
ruff check src tests tools
pytest -q
```

## Try it with existing v38 data (read-only)
```
fstk --profile <new-profile.json> profile import "<v38 machine.json>" --data-root <dir> [--media-root <dir>]
fstk --profile <new-profile.json> manifest import "<v38 episode manifest.json>"
fstk --profile <new-profile.json> manifest show DS9:S01E20
fstk --profile <new-profile.json> doctor
fstk-gui --profile <new-profile.json>
```
Imports never modify the v38 files. v38 approvals arrive as *historical* and must be re-confirmed.

### Continue an in-flight v38 episode in fstk
```
fstk --profile <p> manifest import "<…\devices\<DEVICE>\episodes\S01E20\manifest\S01E20.json" ^
     --v38-profile "<…\devices\<DEVICE>\local device config\machine.json"
fstk --profile <p> plan delivery DS9:S01E20        # or the app's "Run to delivery" button
fstk --profile <p> run <the plan it printed>
```
`--v38-profile` points every file the v38 manifest knows (stack master, EFM WAVs, DVD,
descriptors) at its real location on that machine, so fstk continues from them instead of
starting over. The delivery plan runs:

stack render → DVD probe/alignment → cadence → trims/DELTA → DVD-referenced LD audio →
5.1/subtitle retime → encode → remux → QC

It stops for one human review, the A/V check (several 15 s chunks, or rerun with "audio too
early/late"). Steps whose evidence is already current are reused. New runtime dependency:
`pip install soxr`.

## Several machines (e.g. two Windows PCs and a Mac)
Each machine keeps its own profile, state and media. To see each other's episodes and pass an
episode from one machine to another, give every profile the same shared folder (not the v38
release tree) and, for disks several machines mount, the same volume names:
```json
"shared_root": "W:\\fstk-shared",
"volumes": {"media-hdd": "\\\\FILESERVER\\media"}
```
(on the Mac: `"shared_root": "/Volumes/fstk-shared"`, `"volumes": {"media-hdd": "/Volumes/media"}`).
```
fstk share device                                  # this machine's folder in the share
fstk share status [DS9:S01E20]                     # which machines hold which episodes
fstk share handoff DS9:S01E20 --to MAC-1A2B3C4D --note "EFM done; next: alignment"
fstk share adopt DS9:S01E20 --from PC-1-0A1B2C3D    # on the Mac
fstk share reclaim DS9:S01E20                      # undo a handoff not yet adopted
```
The app shows the same in the episode picker ("Other devices", "Adopt from other device") and
offers "Hand off…" in the main window. Design and rules: `docs/adr/0002-shared-multi-machine-workspace.md`.

## Layout
`src/fstk/{core,config,manifest,tools,media,stages,evidence,workflow,workspace,ui,cli}` —
see `docs/adr/0001-architecture.md`.
