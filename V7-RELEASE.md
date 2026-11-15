# Workflow Version 7 release notes

V7 adds portability/readiness tooling around the V6 episode workflow. It does not
lower V6 audio, video, safety, space, or QC gates.

## Included

- `scripts/doctor.py`: unified, read-only machine report. It calls the established
  Windows or macOS preflight, checks the platform's direct-Python version lock, and
  emits JSON. Linux x86_64 receives common capability/dependency checks but always
  reports `ready_for_episode: false` until an end-to-end Linux run is accepted.
- `dependencies/`: exact direct Python pins for Windows x86_64, macOS arm64, and a
  candidate Linux x86_64 environment. `scripts/bootstrap_deps.py` checks by default;
  `--install` is an explicit user action, uses `pip --no-deps`, and touches only those
  packages in the profile-selected Python interpreter. Native tools/plugins remain
  individually version-gated. The conda recipes pin direct versions; their complete
  transitive build locks are not yet captured.
- `scripts/run_stage.py`: allowlisted stage dispatcher. It prints a plan by default;
  `--execute` is required for execution. Write stages run the doctor and their declared
  manifest validators, then save unique command logs and JSON results. Linux production
  stage execution is blocked.
- `scripts/generate_runbooks.py`: generates the Qwen4B and Qwen4B/Luna runbooks from
  `DS9 LaserDisc OpenCode Runbook.md` plus short model-policy templates. `--check`
  detects stale generated copies.
- Linux x86_64 candidate profile, profile-aware setup documentation, tests, and this
  release record.

## Validation status

- Windows x86_64: the V7 authoring environment's direct Python lock and unified doctor
  pass; stage planning and tests pass. A read-only `S01E07` manifest-validation attempt
  correctly stopped because the configured download folder/source captures were absent.
  V6's episode workflow remains the Windows reference.
- macOS arm64: dependency pins are based on M5 component evidence. No fresh-Mac V7
  end-to-end delivery/QC is claimed.
- Linux x86_64: candidate Python/environment pins only. Native Decode-Orc, frameserver
  plugins, PowerShell integration, and full episode delivery are unvalidated. Do not
  process a production episode or call Linux supported based on lock/bootstrap success.
- Linux ARM64 and Windows ARM64 remain untested.

See `WORKFLOW_VERSIONING.md` and the V7 entry in `W:\ld-pipeline-execution-log.md` for
validation evidence and known limits.
