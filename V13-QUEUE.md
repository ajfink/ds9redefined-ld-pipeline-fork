> **NOT FOR LIVE WORKFLOW USE.** This is release-planning material only. Do not read, cite,
> or apply queue entries as episode instructions, measurements, approvals, stage arguments,
> or gates. Active jobs follow the current released README, runbook, and tools.

# V13 planning queue

**V13 is released.** This planning/review record documents the accepted code and documentation
scope plus episode evidence deliberately held for separate review. Its entries remain
non-operative; live work follows the released README, runbook, and scripts.

## 1. Make `measure_break_edges.py` fail closed on empty clipped windows

### Problem and intent

V10 added neighboring-black clipping to keep content-match windows clear of black intervals
and fades. When a close pair leaves an inverted window, `match_window()` correctly returns
`None`, but the caller dereferences it. Very small windows can instead return a result with
`offset: null`; those must also remain unmeasured. The clipping/fade-clearance and minimum
texture rules must not be weakened to avoid the crash.

### Required behavior

- Represent a clipped-to-empty window explicitly rather than returning a backwards interval.
- Handle both empty-window results and `match_window() is None` before reading match fields.
- Emit a diagnostic row with null measurements and a distinct status/reason such as
  `window-too-small` or `insufficient-texture`; continue processing other breaks.
- Keep the command nonzero when any required edge remains unresolved. Do not update manifest
  offsets from a failed or incomplete run.
- Do not merge separate breaks solely to create larger windows. S01E12's close pairs are
  confirmed as distinct blackouts on the DVD.

### Evidence and acceptance tests

- Synthetic geometry tests cover windows that are empty, inverted, one-frame/tiny, and normally
  clipped; include neighboring gaps below, at, and above `2 * fade_clear`.
- Caller tests cover both `None` from `match_window()` and a non-null result with
  `offset: null`; assert no crash, no manifest mutation, null offsets, useful diagnostics, and
  nonzero exit while unresolved edges remain.
- Preserve the V12 defaults (`fade_clear=2 s`, `min_frames=300`) unless separately justified
  by reviewed evidence and a versioned change.

### V13 implementation status

V13 makes clipped-empty windows explicit, guards the `None` match path, and keeps the command
nonzero for unresolved rows. Synthetic tests cover the geometry, both `None` cases, and the
incomplete-report exit. No S01E12 `off_before`/`off_after` values were generated or recorded by
this release.

## 2. Complete S01E12 close-pair edge adjudication without guessed offsets

The crash report records 17 distinct break candidates: eight measured, two with insufficient
texture, and seven that crashed. A crash guard alone does not unblock retiming. Keep the eight
supported measurements; for each unresolved side, inspect the sampled 29.97-grid DP path and
content-rich frames just outside the relevant black, identify the local plateau, and cross-check
against independent picture evidence and the DVD/LD black-duration step arithmetic. DP or
black-duration arithmetic alone is not sufficient. Record `off_before`/`off_after` only after
reviewed corroboration. If an inner plateau cannot be distinguished because too little content
exists between two genuine cuts, leave it null and keep retiming blocked. Do not reuse another
episode's values or widen/extrapolate outside measured alignment coverage.

## 3. Wire the DVD-backed cut-PAD repair and per-device image record

### Intended behavior

- Repair only a map-matched, full-raster-confirmed single-frame PAD blink.
- For a non-cut/in-action PAD, RIFE-interpolate between the real output-domain neighbors.
- For a cut PAD with a DVD, never RIFE across shots: map the output PAD through the verified
  DVD↔LD picture map, sequentially decode/IVTC the uniquely matched DVD frame, crop/scale to the
  LD raster, and gently color-match to the prior good LD frame.
- Save the reviewed, timestamped still and provenance under
  `devices/<device>-<stable-id>/episodes/<episode>/cut-pad-images/`, then inject that still at the
  exact PAD output frame in the source-derived padfix chain. Keep the no-DVD shared-still/hold
  path separate and explicitly user-approved.
- Preserve frame count; verify replacement identity and both neighbors. Genuine dark content,
  fades, credits, and non-PAD blacks remain untouched.

### V13 implementation and remaining boundary

V13 requires the exact unique cut-PAD frame list in DVD and no-DVD modes, keeps
RIFE limited to non-cut candidates, requires a per-device reviewed still for every DVD-backed
cut PAD, and records a color-match note in newly published provenance. The no-DVD still/hold
choice remains separately user-approved. Synthetic policy/provenance tests pass.

Remaining: the helper accepts a previously extracted/cropped/color-matched PNG; it does not yet
automatically decode/IVTC a DVD frame or compute the color match. That frame mapping and image
preparation remain operator-reviewed, with no E12 still generated by these unit tests. Keep PAD
repair limited to confirmed map-matched full-raster blinks; never replace genuine dark content.

## Other V13 changes from device-log findings

These changes/tests are included in V13; platform and episode limits are stated below:

- Fixed the generated VapourSynth padfix call to coerce audit/report strings to `Path`; added a
  cross-platform path-contract test. A real generated `.vpy` evaluation still needs the Mac
  VapourSynth environment.
- Improved the OpenCV identity-gate diagnostic to name the platform distribution and preserve
  the original import error. `qc_report` removes `DYLD_LIBRARY_PATH` only for that macOS child
  gate; tests prove other gates and the parent environment are unchanged.
- Ported W's S01E09 act-cut A/V window split. Synthetic tests cover cut splitting and distinct
  lag peaks. The W-local change was also exercised on S01E09: QC run 3 reported 13 pass,
  1 warn (a known sub-resolution retime gap), 0 fail; its cut-straddling 900 s point was split
  into separately anchored windows and the <8 s pre-cut span was logged as dropped. This is
  evidence for W's local patch, not real-media validation of the Z port or its Mac runtime.
- Persisted one stable machine-folder suffix in a machine-local sidecar; new episodes reuse its
  `devices/<device>-<id>/episodes/<code>` tree. A single legacy folder can be adopted; multiple
  or unverifiable legacy folders are not guessed and do not create a third shared folder (the
  existing isolated local fallback remains available). A validated explicit workspace selection
  records the chosen id; an operator can also set `shared.device_instance_id` or
  `LDP_DEVICE_INSTANCE_ID`. W's S01E11 log records that the empty duplicate
  `MAC-OPENCODE-1EF5351C` still exists beside the active `4DD0B985`; neither was moved or deleted.
  Tests cover reuse, legacy adoption, and ambiguous identity. Z's S01E08 device log also
  reported a missing `shared` binding in the local-fallback resolver; the current resolver
  defines it, and workspace tests exercise fallback creation.
- Implemented PEP 440 target-or-newer comparison for direct locked packages, fail-closed on
  malformed/incomparable versions, and retained import failures as blockers. Python runtime
  pins remain exact. Tests cover version ordering and an importable newer OpenCV build.
- Normalized workspace test comparisons for symlinked macOS temp paths. This is test portability,
  not a new macOS support claim.
- Added a prep-checklist template and made transfer-time prep a standing, gated section in the
  V13 README/canonical runbook. It requires a workspace copy, lists independent prep, and keeps
  LD-dependent work blocked until source verification. Generated model runbooks and the plain-
  text companion are produced from the canonical source and checked for drift.
- Reviewed and added synthetic tests for the untracked E12 `fade_midpoint_fit.py` and
  `efm_report_table.py` report-only prototypes. Z's E08 log shows why fade fits must not become
  an approval rule: OLS was distorted by an endpoint/staircase, while Theil-Sen corroborated an
  independent envelope fit; W's E09 log says its music-through breaks made fade pairing unusable.
  Z's E12 audio decision separately closed its audio gate using that episode's measured PAD
  occupancy and duration evidence; that does not resolve any S01E12 break edge. The EFM helper
  stays report-only: E08/E09/E11 reports all used `POOR` despite low post-C2 losses, and the
  selected capture varied by episode. The parser now accepts W's archived
  `S01E11-efm-report-<capture>.txt` names and shows Q-channel continuity; it was checked
  read-only against the five preserved W report logs. No audio media was decoded here.
- Added Orc refresh streaming/heartbeats and completed-pass checkpoints with identity, input,
  project, and log verification on resume. A synthetic CLI interruption test proves it resumes
  at the next pass without repeating the completed pass.
- Added `extract_candidate_frames.py`: explicit unique frame indices only, ≤128 output frames
  per run, ≤32 select terms per decode, a worst-case PNG/reserve space check, and a hash TSV.
  Synthetic tests verify sparse indices cannot turn into a whole-span PNG dump.
- W's S01E11 analysis log also proposes a reusable verification discipline for scratch analyses:
  state sign/scale units, print raw rows, and independently cross-check a result before it enters
  a manifest. Carry this as a review requirement for future helpers; do not mistake it for new
  measurement evidence. The same Mac log notes RIFE's RGB24/8-bit output from RGB48/10-bit
  source frames; preserve this disclosed precision limit and validate the actual color-conversion
  path rather than claiming the generated still restores 10-bit source detail.

The W act-cut patch has been ported into the V13 source. W's S01E09 QC run is real-media evidence
for the W-local patch; the V13 Z implementation was validated synthetically, not run against
episode media.
`fullres_check.py` still repeats full-source decoding across chunks; performance optimization is
deferred. The 38-GiB incident came from an untracked scratch script; the new exact-index extractor
is a bounded alternative, not a copy of that script.

## Held episode evidence

- **S01E12:** Z's device logs record the separate audio decision/gate as complete (16 pass, 0
  fail). The break-edge log accounts for 17 candidates (8 returned matches, 2 insufficient-
  texture, 7 crashes); the manifest still has all offsets null. The crash guard and closed audio
  gate do not adjudicate the close pairs; retiming stays blocked until every required edge has
  independently reviewed evidence.
- **S01E13:** W's prep/DV logs record thresholds `blackdetect=d=0:pix_th=0.10:pic_th=0.98` and
  `silencedetect=noise=-40dB:d=0.20`, 7 DVD cut candidates, and 5 DVD music-through merge
  candidates. Since the earlier prep note, W logs show the download verified 15/15, Orc selected
  M1 after two passes, and the stack render wrote 83,239 frames. However, the device manifest
  now contains non-null LD break/offset and merge entries, while the `S01E13` logs/references
  reviewed here do not contain their supporting edge/adjudication report. The render log's
  successful frame write is not itself a `validate --stage stack` report. This review did not
  verify external probe artifacts or edit the manifest: treat these as pending provenance
  review, and do not copy, endorse, or reject the values based on DVD prep alone.

### Device-log sources reviewed

- Z: S01E08 `device-workflow-change.md`, `S01E08-prep.md`, `S01E08-fade-midpoint-fit.md`,
  `S01E08-asetrate-decision.md`, and `S01E08-efm-report-comparison.md`; S01E12
  `S01E12-prep.md`, `S01E12-asetrate-decision.md`, `S01E12-efm-capture-decision.md`,
  `S01E12-stack.md`, `S01E12-cadence.md`, and `S01E12-bug-measure-break-edges-crash.md`.
- W: S01E09 `S01E09-job-log.md` (Orc timeout, bounded-probe incident, cut-straddling A/V and
  QC run 3); S01E11 `S01E11-run-log.md` (analysis bug register, Mac padfix/OpenCV defects,
  workspace duplicate, and disclosed limits); S01E13 `S01E13-prep.md` and
  `S01E13-dvd-structure.md`.
- The device logs are evidence/context only. No values were copied into another episode or W
  manifest. The W source bundle was synchronized to V13 while preserving its device data and
  W-only untracked files.

## Validation snapshot

- Latest full suite: **165 passed, 7 skipped** (172 tests total), including synthetic tests for
  clipped break windows, exact-index extraction bounds, cut-PAD policy, act-cut A/V splitting,
  stable device identity/legacy ambiguity, target-or-newer package handling, Orc checkpoint
  resume, and both E12 helper prototypes. The EFM report reader was also read-only checked
  against W's five preserved S01E11 text reports; no media was opened or decoded.
- Python compilation, warning-as-error imports, and generated-runbook checks passed for V13.
  This release performed no media processing, no S01E12 break-edge
  adjudication, and no new S01E13 measurements; the Mac VapourSynth runtime was unavailable.
- V13 makes no new macOS/Linux production-support claim. No episode media or manifest values were
  part of release validation.

## Guardrails

- No PAD, break edge, offset, rate, or approval is inferred from another episode.
- No update lookup/package mutation, media copy, or real-episode reprocessing is part of unit
  acceptance.
- Future releases must document platform validation and limitations; synthetic tests alone do
  not establish macOS/Linux production support.
