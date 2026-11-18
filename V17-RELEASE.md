# Workflow Version 17 — release record

**Transition: Version 16 -> Version 17.** V17 is a targeted defect fix accepted from another
device's log entry. It changes no measurement, approval, QC, or safety gate, and it processes
no episode media.

## Source of the change

- `MAC-OPENCODE-4DD0B985` (M5 Mac), S01E16 device log section 30, standalone record
  `devices/MAC-OPENCODE-4DD0B985/episodes/S01E16/logs/S01E16-doctor-instance-root-defect-20261117.md`.
- The Mac operator logged the defect read-only, applied a shell workaround
  (`export LDP_INSTANCE_ROOT=...`), and proposed the fix for a future version without editing
  release files.

## Defect

`scripts/doctor.py --instance-root <path>` copied the flag only into the **child-process**
environment (`env["LDP_INSTANCE_ROOT"]`), while the doctor's own workspace resolution calls
`ldcommon.shared_instance_root()`, which reads the **parent** `os.environ`. Used without an
exported `LDP_INSTANCE_ROOT`, a shared-mode doctor run aborted before producing a report:

```text
ldcommon.PipelineError: shared mode has no active workspace; episode helpers create one
when an episode code is known
```

Independent validation in this release:

- Reproduced against the published V16 tree with a real profile and the `S01E18` instance.
- Reproduced again in a synthetic shared bundle (the basis of the new regression test).
- Confirmed the log's impact analysis: stage-gated doctor runs were unaffected because
  `run_stage.py` passes the whole workspace environment (including `LDP_INSTANCE_ROOT`) to the
  child process, so only manual `--instance-root` invocations failed.
- Checked the related `--report-out` path: it is **not** affected by shared path redaction,
  because the report-out location check uses the live `shared_report` object while redaction
  applies only to the emitted copy.

## Fix

- `doctor.publish_instance_root()` publishes the flag to **both** the calling process
  environment and the child environment before `shared_instance_root()` resolves the workspace.
- An exported `LDP_INSTANCE_ROOT` that disagrees with `--instance-root` now fails closed with
  a clear `LockError` (reported as a doctor error, exit 2) instead of silently resolving a
  different instance than the flag named. Previously the exported value silently won.
- The child-environment behavior and the read-only character of the doctor are unchanged: no
  workspace is created, no file is written unless `--report-out` is supplied.

## Validation

- Full unit suite: **265 passed, 7 skipped** (272 tests), including four new tests:
  flag publication to the calling process, fail-closed conflict handling, an end-to-end
  `doctor.main()` run from the flag alone that writes its report, and the conflict error path.
- The end-to-end test was verified to fail with the exact reported `PipelineError` when the fix
  is reverted.
- `compileall` and imports with warnings treated as errors passed; generated runbooks are
  current; regenerated plain-text output matches byte-for-byte; all 8 PowerShell files parsed;
  `git diff --check` produced only LF/CRLF normalization notices.
- Validation is synthetic/fixture-based. No episode media was downloaded, processed, or
  re-encoded for V17.

## Boundaries

- No gate, threshold, or approval requirement changed. The doctor remains read-only.
- V16's policy is unchanged: release-root ACL status stays advisory, while application
  device-subtree path guards remain enforced.
- W's stale V7-based Git metadata and unrelated dirty/untracked files are preserved.

## Publication

Pending. V17 is authored and validated in the Z authoring worktree; GitHub `main` still points
at the V16 publication (`83c9646`) until this record is updated on publish.
