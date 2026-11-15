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
- Added shared-release mode for multiple read-only consumers: each device/OpenCode
  instance initializes a unique `devices/<device>-<random>/episodes/<code>/` workspace
  containing its manifest, logs, references, and cut-PAD inbox. Shared-mode scripts require
  an external local profile and route manifest writes/logs into that instance; curated
  release files remain read-only. No ACLs are changed automatically; administrators must
  provision `devices/` and grant write access only there. The W share's current ACL was not
  changed per operator direction, so it still requires administrator enforcement for an
  OS-level read-only guarantee.
- Added mandatory indexed-seek audio QC for every audio track in each final master: four
  quarter-timeline sample points, comparing fast input seek with output-seek control. Both
  platform remuxers set Matroska `-max_interleave_delta 0`, the M5-verified fix for
  audio-less clusters caused by the default 10-second interleave flush. Final QC uses
  unique absolute-A/V report names so reruns do not fail on existing artifacts.
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
- macOS arm64: M5 completed S01E10 end to end through final multi-track remux. The corrected
  master had 0/1067 audio-less clusters; all six previously failing seek windows decoded
  fully and all four packet-identity checks passed. This validates the tested M5 setup; a
  fresh Mac installation is not independently validated.
- Linux x86_64: candidate Python/environment pins only. Native Decode-Orc, frameserver
  plugins, PowerShell integration, and full episode delivery are unvalidated. Do not
  process a production episode or call Linux supported based on lock/bootstrap success.
- Linux ARM64 and Windows ARM64 remain untested.
- The shared-drive application paths avoid writing to the release root, but V7 does not
  provision/modify filesystem ACLs. If the share grants write access at the root, an
  administrator must correct that ACL before calling it OS-enforced read-only.
- The follow-up tests shared-workspace initialization/provenance and
  per-instance manifest/write-path guards. Full suite: 53 passed, 7 Mac/VapourSynth-only
  skipped; Windows doctor passes in local mode. The active W account still has root write
  access, so W shared-mode doctor correctly blocks stage execution pending an ACL change.

See `WORKFLOW_VERSIONING.md` and the V7 entry in `W:\ld-pipeline-execution-log.md` for
validation evidence and known limits.
