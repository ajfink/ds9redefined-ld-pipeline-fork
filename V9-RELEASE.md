# V9 fork prototype release notes

**Historical status: experimental automation prototype.** V9 introduced the fork-only
orchestrator on `automation-first-batch`. Mainline has since advanced to V10, and the fork
now adapts V10 while retaining V9 orchestration. Do not treat this record as a mainline
release or a general platform-support claim.

## Included

- Added `scripts/run_workflow.py`, an ordered JSON-plan executor that delegates only to
  `run_stage.py` allowlisted stages. It supports dry-run, streaming logs, atomic per-job
  status, an OS advisory single-writer lock, and profile/workflow/plan identity checks.
- Resume skips a completed stage only after checking declared input/output fingerprints.
  Full SHA-256 is used for files up to 64 MiB; larger files use size/mtime and first/last
  block hashes by default. Plans can request full hashes. Only the explicitly resumable
  Archive download step may be retried automatically; other interrupted/failed steps stop
  for inspection.
- Added an intake-plan example and documented explicit choices. The executor does not
  infer episode identity, approve DVD-free mode, choose measurements, or clean files. The
  example separates `_instructions_DO_NOT_EDIT` from `_operator_inputs_EDIT`; the runner
  rejects unresolved placeholder values before executing a plan and accepts UTF-8-BOM JSON
  plans written by PowerShell.
- Decision audit entries automatically record the current local account and workflow-start
  UTC timestamp; operators do not enter approver/time fields.
- Job IDs are generated from episode code, confirmed title, and a UTC timestamp. On resume,
  the runner locates the saved job by plan/profile fingerprint and refuses ambiguous matches.
- Added allowlisted dispatch entries for existing descriptor, alignment, audio-sync, and
  DVD-retime helpers. Corrected pre-encode PAD full-resolution/RIFE runner gates to depend
  on cadence evidence, not the later PAD-fix output they help create.
- Added Orc stack-render artifact verification and cross-platform TFM-log generation.
  The Mac `cadence-analysis` stage creates the measured map/report; after its review gate,
  the separate `cadence-vpy` stage emits the VapourSynth script. TFM logs are checked for
  one consecutive decision per expected frame.
- Added mandatory plan-level human review steps after stack rendering and cadence-analysis stages.
  The workflow pauses with `WAITING_APPROVAL` and resumes only after explicit `--approve`.
- Added `tests/test_intake_workflow.py`, a no-network, fixture-backed intake integration
  test that simulates an interrupted download, resumes it, initializes a test manifest,
  validates the inputs, and confirms a later resume skips only verified completed steps.

## Validation

- Added plan allowlist, fingerprint mutation, safe-resume, and resumable-download tests.
- Current fork suite: **92 passed, 7 VapourSynth-only skipped**; Python compilation,
  generated-runbook check, `git diff --check`, and `motion-probe.ps1` AST parsing passed.
- At the initial V9 prototype validation, no real media stage or Archive transfer had been
  run through `run_workflow.py`. A later supervised S01E08 Windows canary exercised stack,
  TFM, motion, and cadence stages; that narrow evidence is recorded in `V10-RELEASE.md` and
  does not validate DVD retiming or final delivery.
- Windows x86_64 is the current implementation host. macOS/Linux execution of the new
  orchestrator and cross-platform lock behavior still require platform testing. Linux
  production stages remain blocked.
- The supervised Windows canary supplied real Orc/TFM/cadence evidence after the original
  V9 snapshot. The Mac PowerShell/VapourSynth adapter still has only synthetic/unit coverage.

## Known limits

- Plans must be authored with explicit ordered steps and exact artifact paths. The runner is
  orchestration infrastructure, not a complete automatic stage graph or GUI.
- Balanced fingerprints are intended to detect routine changes efficiently; they are not
  full-file cryptographic verification for large media. Use `fingerprint_policy: "full"`
  where full content verification is required, and retain stage/QC gates.
- If a non-download stage is interrupted or fails, inspect its report and artifacts and
  create a reconciled new job plan rather than automatically retrying it.
- A supervised Windows S01E08 pass now supplies narrow real Orc/TFM/cadence evidence, but
  does not validate every adapter or establish end-to-end production readiness. A real Mac
  cadence pass remains open; the review gates remain mandatory.
