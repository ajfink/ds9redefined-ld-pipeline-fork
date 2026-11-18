# Workflow Version 16 — FromStunToKill app-branch release record

**From Version 15 -> Version 16 on the FromStunToKill app branch only.** This release hardens
human PAD classification, EFM source/output provenance, Orc render progress reporting, and
device-workspace backup/restore. It does not publish a new production `main` release and does
not sync or alter the W shared tree. No episode media was processed; no final encode or remux
was run for implementation or release validation.

## Live-workflow contract added by this branch

- Every confirmed PAD candidate must receive an explicit human visual classification using the
  two exact source-frame flanks and the full-raster evidence before any RIFE plan can be built.
  The exact warning remains: `ONLY generate as an interpolated frame if this pad frame was
  incorrectly identified as preceding a scene cut.` Correcting a prior cut classification
  requires an explicit confirmation and acknowledgement. DVD-backed cuts still require a
  reviewed/provenanced still; no-DVD cuts retain the explicit hold/still policy and approval.
- PAD decisions are saved as immutable evidence. The manifest recorder checks the exact candidate
  set, input hashes, unchanged manifest, current DVD/no-DVD policy, cut stills, residual acceptance,
  review note, and required warning before creating a no-overwrite backup and atomically recording
  cut/RIFE lists. A human review gate remains between recording those decisions and RIFE.
- RIFE retries use unique per-run directories. AviSynth flank extraction uses exact `Trim` frame
  indices (the VapourSynth path uses exact start/end indices), rather than a rounded timestamp
  seek. Map-matched boundary PADs still fail closed; unmatched boundary orphan black frames are
  reported with blank neighbor metrics and excluded from flank scoring.
- EFM verification independently binds a selected manifest capture's TBC and JSON metadata to the
  Orc log, decoded WAV, and EFMSink report. It checks current source hashes/fingerprints and stores
  immutable per-capture evidence. A separate human capture-selection plan requires every capture
  to have current verified evidence, compares reports without auto-ranking, records a rationale,
  maps `audio.pad_frames` from refreshed Orc input order, and backs up the manifest before update.
  RIFE refuses to run without the hash-checked human decision and committed plan files in the
  active device workspace, even when invoked directly rather than through the GUI. The audio
  validator rechecks provenance and current file hashes. Existing reconstructed logs
  that name another episode are rejected, not relabeled.
- Audio-rate reports choose fresh paired names on rerun. Orc render monitoring emits PID, elapsed
  time, output size, and no-progress heartbeats; prolonged stalls are advisory and never trigger
  automatic kill/retry.
- Device/episode backup supports checksum-verified immutable archives outside the release tree,
  plus dry-run restore. Restore refuses an existing destination and never merges or overwrites a
  workspace. Device backups include scoped episode evidence and selected device change/config
  records; media roots are excluded.

## Validation and limits

- Full suite: **365 passed, 7 skipped** (372 tests). The seven skips require the selected
  VapourSynth Python environment and were not runnable on this Windows host.
- `compileall` for `scripts/` and `tests/`, warnings-as-errors imports of changed Python modules,
  `generate_runbooks.py --check`, all **7 PowerShell parsers**, and `git diff --check` passed.
  Git reported only configured LF/CRLF normalization notices.
- PAD, EFM, and backup tests use synthetic fixtures; no real media decode, RIFE invocation, full
  workflow, Mac runtime, or device restore was performed as part of release validation.
- S01E11 and S01E13 reconstructed EFM logs remain quarantined because they name S01E10 inputs and
  are byte-identical across those folders. Missing authentic reports were not recreated. See the
  device recovery notes in authoring execution-log section 55.
- S01E11 B2 exclusion rationale/B8 `LIMIT` and S01E15 render completion/root cause remain
  unresolved. The existing S01E12 output and QC report were found, but this release did not
  independently replay the delivery stages; see authoring execution-log section 55.
- Production `main` and the W shared tree remain V15. This app-branch version marker must not be
  treated as a production sync or a fresh-Mac/Linux support claim.
