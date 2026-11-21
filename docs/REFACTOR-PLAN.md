# FromStunToKill — clean-sheet refactor plan

Source: `Z:\fromstuntokill-v15-app` (app branch, `WORKFLOW_VERSION` 38; a git worktree of `Z:\ld-pipeline`)
Target: `Z:\FromStunToKill` (empty, new tree)
Status: **approved direction; decisions in §7 recorded 2026-11-20**. Nothing in the source tree or `Z:\ld-pipeline` is modified, and no commits are made by Claude.

---

## 1. Why a clean sheet

V38 works and carries hard-won correctness. The problems are structural, and they now slow every increment:

| Finding (measured on the v38 tree) | Consequence |
|---|---|
| ~43k lines of Python in ~120 flat `scripts/*.py`, plus 1.4k lines of PowerShell | No module boundaries; every script is both library and CLI |
| 75 scripts patch `sys.path`; ~30 launch other scripts as subprocesses | Fragile imports, no shared in-process state, hard to test end to end |
| `workflow_app.py` is 4,706 lines (175 functions, one `WorkflowApp` class); `workflow_workbenches.py` 2,288; `run_workflow.py` 2,001 | UI, orchestration and domain logic are tangled together |
| Manifest is an untyped dict (`episodes/_template.json`, 9 top-level sections, ~80 fields) with validation spread over `validate_episode.py` and per-stage gates | Contract lives in prose and scattered checks; no schema version or migrations |
| Stage registry is a dict in `run_stage.py` (~60 stages) mapping names to script files | Stages declare no inputs, outputs or evidence; the DAG engine infers them from plan JSON |
| Two platform implementations (Windows AviSynth + PowerShell, macOS VapourSynth + Python) for map, encode and remux | Double the surface; Windows-only traps (PS 5.1 stderr, UTF-16 Tee output) |
| Code and device data share one tree: `machine.json`, `devices/<HOST>/…` with logs, plans, manifests, 13 `.bak` manifests in ~1 h for S01E20, and `.tar` snapshots | Releases, backups and git hygiene are all harder; the `_readme` already says machine.json should not live here |
| 92 KB README + 88 KB runbook + 2 LLM-specific runbooks + 47 KB PORTING + 30+ version records | Rules are duplicated in prose and drift from code |

## 2. Invariants the new app must keep (non-negotiable)

These come from the README, runbook, PORTING §5 "Traps already hit" and the V16–V38 release notes. Each becomes an explicit test or a typed guard, not prose.

1. **Measured, not assumed.** Unknown values stay unset (never 0). No value is copied from another episode. No conversion formula is inferred where the runbook defines none (e.g. the DVD fit-agreement threshold or the WAV0 origin convention stays a human decision).
2. **Validate artifacts, not exit codes.** Orc, blackdetect and the downloader can exit non-zero on success, and zero on truncation. Every tool adapter has an artifact validator.
3. **Fail closed.** Parse errors or skipped inputs fail the check; reports state expected and checked counts.
4. **Human approvals are explicit and bound.** APPROVED/REJECTED + note, tied to fingerprints of the evidence, manifest and inputs; stale approvals are invalidated when inputs change.
5. **Manifest writes are atomic, locked, hash-checked and auditable.**
6. **Space gate before every heavy write**, with `min_free_gb` as a reserve; no deletion without specific user authorization; nothing kept in OS Temp across stages.
7. **Media rules:** never `-ss` a DVD with non-monotonic PTS (decode sequentially); `select=` expressions are limited to 32 terms per chunk; no cross-shot RIFE; cut-PAD stills carry provenance; one final x265 encode (CRF 14 / slow / grain / Main 10), with audio fixes remuxed only; Topaz is never launched automatically.
8. **No-DVD mode never claims a DVD-based A/V pass.**
9. **Toolchain and workflow version are frozen per episode**; a mid-job change pauses before the next stage.
10. **Shared-tree redaction** of device paths in anything written to the shared share (the SMB share the Mac also writes to).

## 3. Target architecture

One installable Python package, layered so dependencies only point downward:

```
fstk/
  core/        errors, identity (series/episode), hashing, atomic files, locks, clocks, redaction
  config/      machine profile (typed), job config ("CONFIGURE HERE FIRST" replacement), platform detection
  manifest/    typed schema (dataclasses), schema version + migrations, the single writer
               (lock → verify base hash → write → append-only journal; replaces .bak sprawl)
  tools/       one adapter per external tool: ffmpeg/ffprobe, orc (+EFMSink), vspipe, rife, 7z, archive.org HTTP
               – process runner (stderr capture, encoding/BOM detection, child-tree stop, heartbeats)
               – artifact validators per tool
  media/       pure domain algorithms, no I/O side effects: cadence/TFM parsing, descriptors and DVD↔LD
               alignment, fade/midpoint fit, audio rate/PAD timing, PAD audit/classification, retime schedule,
               QC metrics
  stages/      one module per stage: declares params, inputs, outputs, gates, resource class, review
               requirement; implements plan() → run() → verify(). The registry is built from these declarations.
  evidence/    review-asset builders (contact sheets, A/B clips, GIFs, plots) + approval records
  workflow/    DAG engine, state/resume, fingerprints, resource limits, job lock, progress events
  workspace/   device workspace + shared release tree (manifest sync, cut-pad publish, backups)
  ui/          desktop app; talks only to workflow/ and evidence/ through a small service API
  cli/         single `fstk` command: doctor, init, plan, run, resume, status, review, stage <name>
tests/
  unit/ characterization/ integration/ fixtures/
docs/
  operator-guide.md  stage-reference.md (generated from stage declarations)  adr/  history/
```

Key design choices:

- **In-process stages.** Stages are called as functions by the engine; only external tools are subprocesses. That removes the `sys.path` patching and script-to-script subprocess chains.
- **Stages declare their contract.** Inputs, outputs, gates and evidence are data on the stage class. The engine derives fingerprints, resume safety and invalidation from them, and the stage reference doc is generated from the same declarations.
- **A single platform path in Python + VapourSynth.** The PowerShell scripts (`pipeline.ps1`, `build_map.ps1`, `encode_episode.ps1`, `remux_padfixed.ps1`, `download_archive.ps1`) are ported to Python, as the Mac side already did. Every platform uses VapourSynth (§7.3).
- **A PySide6 UI over a service API.** The UI never touches the manifest or tools directly; it calls `workflow/` and `evidence/` services and subscribes to progress events (§7.1).
- **Data lives outside the code tree.** The machine profile and device workspaces go under a configurable data root (default: a per-user app-data folder), never in `Z:\FromStunToKill`.
- **Dependencies stay small.** Today's runtime is numpy, opencv-python and Pillow. The plan uses stdlib dataclasses for schemas; the only new runtime dependency is PySide6, and it is confined to the optional `fstk[gui]` extra (plus `pytest-qt` for tests). Per-platform lock files are kept (machine profiles may still opt in to newer packages than the lock).

## 4. Migration strategy

This is a strangler-style rebuild, not a big-bang swap:

- **v38 stays usable** until the new app reaches parity, but the in-flight episodes (S01E12/17/18/20) are imported into the new app after Phase 5 rather than finished on v38 (§7.4).
- **Characterization first.** The 109 v38 test files (~520+ tests) are the behavioural spec. Port them first, against v38's code, and then point them at the new modules.
- **Golden fixtures** are taken from existing device data, read-only: S01E18/S01E20 manifests, plans, Orc logs, EFM provenance and the TFM/descriptor reports. They are copied into `tests/fixtures` with paths redacted.
- **Manifest importer:** `fstk manifest import <v38.json>` converts to the new schema with a lossless round-trip check. The new app never edits v38 files in place.
- **Plan parity check:** for the same episode state, new `fstk plan` vs v38 `run_workflow` should produce equivalent stage sequences and gates.

## 5. Phases and exit criteria

| # | Phase | Delivers | Exit criteria |
|---|---|---|---|
| 0 | Scaffold | `pyproject.toml`, package skeleton, ruff + pytest config, CI workflow, per-platform lock files, `docs/adr/0001-architecture.md`, v38 inventory (script → new module map) | `pytest` and lint run green on an empty skeleton on Windows; inventory covers 100% of v38 scripts |
| 1 | Core + config + manifest | Typed profile/job config, manifest schema v1, writer with journal, v38 importer | All existing device manifests import and round-trip; lock/hash/atomic tests ported |
| 2 | Tool adapters + VS spike | Process runner, ffmpeg/ffprobe/orc/vspipe/rife/7z/HTTP adapters, artifact validators, `fstk doctor`; Windows VapourSynth spike (install, plugins, field-match on a short Orc-rendered clip) | Every PORTING §5 trap has a test; doctor output matches v38 `doctor.py` on this machine; the spike confirms every required VS plugin on Windows, or the gap is escalated |
| 3 | Media algorithms | Pure ports of cadence, alignment, fade fit, audio timing, PAD, retime and QC math | The ported v38 characterization tests pass against the new modules |
| 4 | Stages | Stage framework + stages in pipeline order: intake/download → stack → EFM → DVD alignment → trims/cadence → audio → PAD → retime → encode/remux → QC, all on VapourSynth | Each stage passes plan/verify tests on golden fixtures; one synthetic-media smoke run end to end; **Windows VapourSynth TFM/cadence output matches v38 AviSynth output** on a reference clip (period-5 %, unmatched, combed spans, frame count) |
| 5 | Workflow + evidence + import | DAG engine, resume, approvals bound to fingerprints, evidence builders, v38 workspace importer | v38 workflow/recovery/review-gate tests pass; plan parity check on S01E20 state; S01E12/17/18/20 import cleanly, with v38 approvals marked historical |
| 6 | UI (PySide6) | Main window, episode/project picker, progress tracker, and Qt workbenches for every v38 review window (alignment, cadence, audio timing, PAD, retime, delivery) | Every v38 workbench has an equivalent; pytest-qt tests for the approval flows; layout checks pass on Windows and macOS |
| 7 | Docs | Operator guide, generated stage reference, history moved to `docs/history/` | No rule exists only in prose; every invariant links to its test |
| 8 | Parity episode | Finish one imported in-flight episode (S01E20 is the most advanced) on the new app, then one fresh no-DVD episode | QC passes; deliverable compared against the v38 evidence for the same source |

Increments within each phase stay small and reviewable. You commit; I'll hand over each increment with a summary of what changed and how it was validated.

## 6. Retired or replaced

- `run_stage.py` / `run_stage_task.py` / `process_args.ps1` → stage registry + engine
- `.cmd` / `.sh` / `.lnk` / `.desktop` / `.app` launcher sprawl → one `fstk` entry point, with thin platform launchers generated at install
- `.bak-*` manifest copies and ad-hoc `.tar` snapshots → manifest journal + an explicit `fstk workspace backup`
- `generate_runbooks.py` / `regen_plain_text.py`, `opencode_help.py`, the four OpenCode/Qwen runbooks and `templates/runbooks/` → generated stage reference only (§7.2)
- AviSynth: `build_padfix_avs.py`, the AVS parts of `build_map.ps1`/`pipeline.ps1`, `avisynth_plugin_dir`/`avisynth_loader` profile keys (§7.3)
- Tkinter app (`workflow_app*.py`, `workflow_workbenches.py`, `review_popup.py`, `*_window.py`) → PySide6 `ui/` (§7.1)
- Device check scripts in `devices/<HOST>/` (`*-check.cmd`, `startup_diagnostic.py`) → `fstk doctor` subcommands
- `Versioning/` → `docs/history/` (read-only); the new project starts its own changelog at 1.0.0-dev

## 7. Decisions (recorded 2026-11-20)

1. **UI toolkit: Qt / PySide6.** `ui/` is a PySide6 app. Review windows use Qt Multimedia for A/B clip and audio audition, and an embedded plot widget for cadence and alignment graphs. PySide6 is an optional extra (`fstk[gui]`), so the CLI and engine install without it.
2. **Interface: app first, CLI for scripting.** The hand-written OpenCode/Qwen runbooks (main, Qwen4B, Qwen4B-Luna, plain-text) are retired. The generated stage reference is the only procedural doc. The CLI keeps machine-readable output (`--json`) for scripting, but there is no bounded-step LLM protocol.
3. **Frame-server: VapourSynth on every platform.** AviSynth, the `.avs` builders (`build_padfix_avs.py`, AVS paths in `build_map.ps1`/`pipeline.ps1`) and the AviSynth plugin/loader profile keys are dropped. The macOS `.vpy` path (`build_map_vpy.py`, `build_padfix_vpy.py`, `build_motion_trace.py`, `vspipe_util.py`) is the base. **The Windows cadence/TFM path must be re-validated on VapourSynth** (Phase 4 exit criterion), including the full-decode TFM coverage check and the Windows `.Trim(audio.pad_frames,0)` equivalent.
4. **In-flight episodes are imported later.** S01E12/17/18/20 are not finished on v38 by default. Once Phase 5 lands, they are imported with `fstk manifest import` plus a workspace importer that also brings plans, approvals, EFM provenance, Orc projects and evidence. Approvals made under v38 are carried as *historical* and must be re-confirmed in the new app before they gate a downstream stage, because their fingerprint binding differs.

## 8. Risks

- **Silent behavioural drift in measurement code.** Mitigation: characterization tests and golden fixtures before any rewrite of `media/`.
- **Windows-specific process behaviour** (stderr, encodings, child-tree kill). Mitigation: port these into the process runner with explicit tests.
- **Scope creep from new features during the rebuild.** Mitigation: feature freeze for the new tree until Phase 5; v38 keeps taking fixes, which are mirrored as tests.
- **VapourSynth on Windows** is new ground for this pipeline: plugin availability (TFM-equivalent field matching, the source filter for Orc output), `vspipe` behaviour and performance. Mitigation: a Windows VapourSynth spike in Phase 2, before any stage depends on it; if a required plugin is missing, stop and decide rather than quietly keeping AviSynth.
- **PySide6 packaging** (Qt Multimedia codecs differ by platform). Mitigation: review clips are generated in a codec Qt plays everywhere (H.264/AAC in MP4), with a fallback that opens them in the system player.
- **Importing approvals made under v38.** Mitigation: they are imported as historical and must be re-confirmed (§7.4).
- **Shared-tree compatibility with the Mac.** Mitigation: the new app reads and writes the shared tree only through `workspace/`, with v38-compatible manifests until both machines move.
