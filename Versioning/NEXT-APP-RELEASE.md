# Next app release stage list

Planning notes for the next app-branch increment after V37. Items are queued here
as they are raised; completed items remain checked for the eventual release record.

## Requested scope

- [x] **Automate EFM capture decoding and handoff.** Added allowlisted
  `decode-efm-captures`; it invokes Orc/EFMSink per available raw EFM, explicitly
  binds each sidecar, preserves `.efm.7z` members under device work storage after
  archive/member and free-space checks, and refuses all output collisions. After the
  approved stack render, the app automatically plans/runs the batch; an Advanced
  action handles already-rendered episodes. Each output is verified against TBC,
  metadata, raw-EFM/archive, generated Orc-project, log, WAV, and decode-report
  evidence, then provenance is added with the existing manifest lock/hash/backup
  writer. The app auto-records only a unique Pareto winner; ambiguous quality
  results open the existing GUI comparison with audio audition. Selection and
  `audio.pad_frames` recording safely ignore captures with no declared raw EFM.
  **Verification:** Orc stage/schema introspection, mocked direct/archive batch
  integration, and 498 tests (7 skipped) passed. No real EFM media decode was run:
  this device workspace's episode downloads directory is empty.
- [x] **Automate audio timing evidence and safe handoff.** `audio.pad_frames` is
  derived from the selected capture's exact verified Orc alignment-map entry.
  Automatically run report-only duration/PAD and primary fade-midpoint diagnostics;
  run raw-provisional picture/audio correlation only against a visually approved,
  hash-bound DVD/LD alignment. Present structured metrics and native-audio audition
  in-app. The runbook has no numeric fit-agreement threshold or exact pad-corrected
  WAV0/intercept convention, so DVD-backed rate/origin and `ld_corrected`/`ld_synced`
  remain uncommitted; `build_ld_synced_plan` refuses to guess `audio.skip_s`.
  The separate no-DVD route now starts automatically after the explicit no-DVD choice
  and measured frame/head-trim prerequisites, while preserving the space check,
  one-frame endpoint gate, manifest lock, and backup. **DVD-backed correction stays
  deliberately blocked pending human review of the V35 A/V samples and reconciliation
  of the exact origin convention.**
  Validation: 509 tests passed, 7 skipped; no real media audio processing was run.
- [x] **Deliver lightweight alignment samples when evidence is ambiguous.** Since
  the runbook defines no numeric rate-confidence threshold, automatically produce
  candidate-only low-resolution clips for fade fit A and the largest-residual pair,
  plus picture/audio windows explicitly flagged as low-correlation or search-edge.
  Preserve each native-WAV/master anchor and preview-only slope; offer DVD fit-B
  alternatives only when the approved, hash-bound alignment maps DVD time to master
  time. Verify preview A/V starts, anchor agreement, stream durations, and output
  hashes; the GUI presents the candidates for visual/audio playback. The entire
  output directory is fingerprinted on resume. Samples are decision aids only and
  never approve measurements or edit the manifest.
  **Validation:** 520 tests passed, 7 skipped, including a real FFmpeg smoke test
  over generated synthetic A/V; no episode media processing or interactive GUI run.
- [x] **Chain DVD/LD alignment into visual boundary adjudication.** After verified
  EFM handoff, automatically extract master/DVD descriptors sequentially and run the
  continuity-constrained helper into unique candidate outputs. Build descriptor-
  matched paired-frame contact sheets and short animated A/B samples at weak matches,
  search edges, large transitions, and measured boundaries; expose source frame-rate
  disagreements and require acknowledgement. An in-app path graph/sample player
  requires an explicit APPROVED or REJECTED decision plus a human note. Approval
  records a lock/hash/backup-bound marker and updates the manifest's selected
  descriptor/alignment paths; rejection writes an audit without changing the
  manifest. Duration/fade diagnostics may run report-only before alignment approval;
  raw picture/audio corroboration is deferred until the approved path is hash-verified.
  The app then gathers the episode-reviewed black/silence parameters,
  builds report-only retime candidates, and opens the existing classifier with
  paired visual context and sequentially decoded DVD-audio audition clips. Only
  globally unique pairs can be batch-selected; ambiguous, collided, uncovered, or
  unmatched candidates remain explicit. No retime offset/step/gap or AudioSkip is
  inferred or written.
  **Validation:** 537 tests passed, 7 skipped; synthetic FFmpeg alignment-frame
  binding and sequential DVD-audio tests passed. No episode media processing or
  interactive GUI session was performed.
- [x] **Automate cadence evidence generation and visual review.** Added a formal
  `video.trim_review` manifest marker and an in-app head/tail evidence window. The
  operator enters already measured 23.976 output-frame trims after reviewing paired
  DVD/LD start/end samples (or the explicit no-DVD source review); no conversion
  formula is inferred. Recording binds the selected master, native audio, PAD, and
  approved alignment/contact assets with a lock/backup/atomic update. The Windows
  cadence plan requires that current trim approval, verifies the source frame count,
  generates a unique `.Trim(audio.pad_frames,0)` TFM AVS/log, and forces a full
  FFmpeg null decode. It accepts only exact, contiguous TFM rows and frame coverage;
  failed/partial logs are preserved with `INCOMPLETE` status and cannot feed the map.
  The chain generates the existing pad-aligned motion trace and deterministic map,
  then parses verified log values into structured period-5, unmatched, combed,
  insert, repair, and review-interval metrics (no console scraping). The app presents
  a period plot and side-by-side raw/TFM GIFs for flagged and representative clean
  intervals. Every sample must be explicitly accepted; unresolved intervals block
  approval. A dedicated recorder confirms manifest/evidence hashes, rechecks the
  current EFM provenance and trim review, locks and backs up the manifest, and writes
  only metrics derived from the verified log. PAD repair and final encode remain
  blocked until cadence approval is current.
  **Validation:** 555 tests passed, 7 skipped; FFmpeg/TFM coverage and N−1 motion
  handling were tested with synthetic inputs. No episode media processing or
  interactive GUI session was performed.
- [ ] **Make encode, remux, and final-QC gates evidence-driven without report
  reading.** Keep the encode and remux authorizations distinct, but make both
  decisions from in-app visual/audio evidence and a concise track/output summary,
  never source code or text/JSON reports. Automatically run the existing pre-encode
  validator and space gate; after the single encode, probe stream properties,
  perform a full decode/frame-count check, and stop before remux on any mismatch.
  Remux only the manifest-declared tracks with stream-copy, then automatically run
  all existing final-QC hard gates. Emit structured gate results for the app. Show
  representative start/middle/end and detector-flagged video/audio samples,
  synchronized playback, and focused evidence for any failed or WARN/LIMIT gate.
  Let a user approve final delivery only after all hard gates pass and required
  visual/audio checks are explicitly accepted. Bind approvals to exact artifact
  fingerprints; retain failures and reports as evidence, but do not ask the user to
  inspect them.
- [ ] **Adopt proven review-player patterns.** Use QCTools as a design reference
  for time-series QC graphs, thumbnail navigation, dual-view playback, and audio
  waveform/loudness views. Evaluate mpv/libmpv for precise playback, frame stepping,
  seeking, and audio/video review controls; do not add a new runtime dependency
  without checking profile support and packaging.
