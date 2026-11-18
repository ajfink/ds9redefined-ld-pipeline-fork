# FromStunToKill app branch - V33 automated EFM decode handoff

V33 closes the first item from `NEXT-APP-RELEASE.md`: once the rendered stack is
approved, decode the available native EFM captures with Orc/EFMSink, verify their
provenance, and continue into the existing safe capture-selection flow.

## Changes

- Added the allowlisted `decode-efm-captures` stage and immutable batch-plan builder.
  The app starts it after the stack visual gate is approved and offers the same action
  under Guided workbenches for a previously completed stack.
- Generate a minimal Orc project per capture with the stack project's declared video,
  source, and amplitude formats; explicitly set the TBC source's `.efm` sidecar path
  and an audio-mode EFMSink with decode report enabled. Never invoke arbitrary shell
  text or modify the weighted-stack project.
- Support verified plain `.efm` files and `.efm.7z` archives. For an archive, inspect
  the listing, require exactly one matching safe member, check the configured work
  volume's free-space reserve, stream only that member to a new device-work path, and
  retain it as fingerprinted evidence.
- Refuse existing WAV/report/project/log/provenance/unpacked outputs. Verify native
  WAV format, Orc success and source/output paths, TBC and metadata fingerprints, raw
  EFM/archive fingerprints, the exact generated Orc project, and the EFMSink report.
  Each per-capture provenance commit uses the existing manifest lock/hash/backup
  writer; later captures stop if the manifest changed unexpectedly.
- Preserve an audio-less capture as ineligible rather than inventing an EFM decode.
  Rank only current provenance-verified EFM captures. A unique Pareto winner is
  automatically recorded; ambiguous results use the GUI comparison and offer audio
  audition. `audio.pad_frames` remains derived from the chosen capture's exact
  verified Orc source-order alignment offset.
- Updated README and versioning records. No production `main`/W code was changed or
  synchronized.

## Validation

- **498 passed, 7 skipped** (`python scripts/run_tests.py -q`).
- `compileall` and `git diff --check` passed.
- Introspected the installed Orc `tbc_source`, `EFMSink`, and `efm_audio_decode`
  parameters. Orc loaded the generated project with the EFMSink node green; the
  expected missing-TBC warning came from the schema-only probe's placeholder input.
- Direct-EFM and compressed-archive batch orchestration, explicit-source project
  binding, PCM checks, provenance updates, backups, archive selection/extraction,
  safe overwrite refusal, and automatic stack-approval handoff have regression
  coverage.
- No real media decode or interactive GUI playback was performed: the selected
  device's episode download folder is empty. Production `main` and W remain V15.
