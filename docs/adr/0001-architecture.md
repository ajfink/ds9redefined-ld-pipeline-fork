# ADR 0001 — Layered package, declared stages, journaled manifest

Date: 2026-11-21 · Status: accepted (owner approved the plan direction and §7 decisions)

## Context
v38 is ~43k lines in ~120 flat scripts that import each other through `sys.path` edits and
call each other as subprocesses. The manifest contract is prose plus scattered checks; device
data shares the code tree; Windows and macOS have separate implementations of the media path.

## Decision
1. One installable package `fstk` with layers whose imports only point downward:
   `core → config → manifest → tools → media → stages/evidence → workflow → ui/cli`.
   `media/` is pure (arrays and numbers in, results out) so it can be tested without tools.
2. Stages are classes that **declare** inputs, outputs, prerequisite gates, resource class and
   review requirement. The registry, the DAG engine's fingerprints/invalidation, and the stage
   reference docs are all derived from those declarations.
3. Stages run in-process; only external tools are subprocesses, all through one process runner
   that captures output safely, decodes BOM/UTF-16, stops whole process trees, and returns
   results that callers must validate as artifacts.
4. Manifest values are declared fields with provenance; writes go through one store that locks,
   checks the base version, writes atomically, and appends a journal line.
5. All device data lives outside the code tree (profile `data_root`/`media_root`).
6. VapourSynth everywhere; PySide6 UI over a service API; CLI for scripting.

## Consequences
- Porting a v38 script means splitting it: pure math → `media/`, tool calls → `tools/`,
  orchestration and manifest writes → a stage, review UI → `ui/`.
- v38 tests are ported as characterization tests first, so behaviour changes are explicit.
- Windows-specific behaviour needs real Windows runs before Phase 4 exit.
