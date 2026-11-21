# FromStunToKill (fstk)

Clean-sheet rebuild of the v38 LaserDisc-to-video pipeline: multiple LD captures → Orc weighted
stack → EFM audio → measured cadence/IVTC with targeted repairs → PAD repair → single x265 encode →
remux → QC, optionally referenced against a DVD. **Output quality is the goal**; every measurement is
evidence-backed, every human decision is bound to the evidence it was made on, and nothing unknown
is ever defaulted.

Status: early development (see `docs/DEVLOG.md`). The v38 tree remains the production reference.

## Start here
- `AGENTS.md` — rules and conventions for anyone continuing the work
- `docs/DEVLOG.md` — what has been built, decisions, and next steps (newest first)
- `docs/REFACTOR-PLAN.md` — plan, invariants, phases, owner decisions
- `docs/findings/` — quality findings about v38 behaviour that need owner decisions
- `docs/v38-inventory.md` — every v38 file → its new home and port status
- `docs/stage-reference.md` — generated reference for the registered stages

## Install (development)
```
python -m pip install -e ".[dev,gui]"
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

## Several machines (Legion, Flex, Mac)
Each machine keeps its own profile, state and media. To see each other's episodes and pass an
episode from one machine to another, give every profile the same shared folder (not the v38
release tree) and, for disks several machines mount, the same volume names:
```json
"shared_root": "W:\\fstk-shared",
"volumes": {"flex-e-hdd": "\\\\LENOVOFLEX\\e hdd"}
```
(on the Mac: `"shared_root": "/Volumes/fstk-shared"`, `"volumes": {"flex-e-hdd": "/Volumes/e hdd"}`).
```
fstk share device                                  # this machine's folder in the share
fstk share status [DS9:S01E20]                     # which machines hold which episodes
fstk share handoff DS9:S01E20 --to MAC-M5-1A2B3C4D --note "EFM done; next: alignment"
fstk share adopt DS9:S01E20 --from LENOVO-LEGION-5-D02C1DA9    # on the Mac
fstk share reclaim DS9:S01E20                      # undo a handoff not yet adopted
```
The app shows the same in the episode picker ("Other devices", "Adopt from other device") and
offers "Hand off…" in the main window. Design and rules: `docs/adr/0002-shared-multi-machine-workspace.md`.

## Layout
`src/fstk/{core,config,manifest,tools,media,stages,evidence,workflow,workspace,ui,cli}` —
see `docs/adr/0001-architecture.md`.
