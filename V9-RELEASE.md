# Workflow Version 9 release notes

V9 adds a dry-run-first, ordered workflow orchestrator and a configurable intake-plan
example. This is orchestration infrastructure, not a fully unattended episode graph.
Existing stage gates remain authoritative; no production gate was relaxed.

## Included

- Added `scripts/run_workflow.py`. Plans contain ordered stage names from the
  `run_stage.py` allowlist and argument vectors; arbitrary shell text is never evaluated.
- Added durable per-job status and combined logs, an OS advisory lock per job, workflow/
  profile/plan identity checks, SHA-256 or balanced input/output fingerprints, and
  conservative resume. Completed steps are skipped only if declared artifacts still match.
  Only a download step explicitly marked `resume_policy: "download"` can be retried after
  interruption; other failed/interrupted steps stop for inspection.
- Job IDs are generated from episode code, confirmed-title slug, and UTC timestamp.
  Decision records receive `approved_by` and `approved_at_utc` automatically; operators do
  not supply those fields.
- Added sanitized root `CONFIGURE HERE FIRST.json`. It keeps read-only instructions
  separate from editable operator values and retains per-setting `_comment` descriptions.
  All paths are placeholders; it contains no machine profile or user-specific output path.
  This sample is a no-DVD intake ending after input validation; DVD-backed intake needs a
  separate plan with the exact matching DVD path.
- Added allowlisted descriptor, motion-trace, DVD-alignment, break-edge, audio-rate,
  sample-accurate LD-sync, and DVD-retime helpers. Fixed the full-resolution and RIFE PAD
  review prerequisites to use cadence evidence rather than the later PAD-fix output. Fixed
  descriptor source arguments so callers cannot override the allowlisted master/DVD source.
- Added workflow-plan/resume tests and a fixture-backed intake integration test using a fake
  stage runner and local Archive metadata/files. It is network-free and exercises interrupted
  download resume, initialization, input validation, and verified completed-step skipping.

## Validation and limits

- Acceptance suite: **81 passed, 7 VapourSynth-only skipped**.
- Python compilation, generated-runbook check, and `git diff --check` are required release
  checks. No intake plan was executed against a real profile; no Archive transfer, real-media
  stage, or full end-to-end episode is claimed.
- Linux production stages remain blocked. These tests do not claim full end-to-end or new
  macOS support. The existing Mac cadence-dispatch integration issue remains open.
- The lock is per job ID; it is not a same-episode cross-job or multi-device claim mechanism.
  Balanced fingerprints are not full-content cryptographic hashes for large files; select
  `fingerprint_policy: "full"` where complete hashing is required.

See `WORKFLOW_VERSIONING.md`, `changelog.txt`, and section 37 of the authoring-device
execution log.
