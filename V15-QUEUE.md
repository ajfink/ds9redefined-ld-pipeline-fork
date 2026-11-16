> **NOT FOR LIVE WORKFLOW USE.** This is release-planning material only. Do not read, cite,
> or apply queue entries as episode instructions, measurements, approvals, stage arguments,
> or gates. V15 is released; this queue is historical planning material and remains non-operative.

# V15 planning record — changelog boundary and retime resilience

## Request

1. Make explicit that changelogs are ignored by normal episode workflow execution.
2. Generalize the S01E12 retime/break-candidate fixes into reusable, report-first helpers so
   close-pair/ambiguous episode evidence is surfaced safely without episode-specific scripts
   or AI-authored measurement choices.

## Adopted behavior

- Treat `changelog.txt`, version-history entries, and release/change summaries as retrospective
  records only. The workflow must not consult them to select an episode, infer or copy a title,
  supply measurements, choose stage arguments, grant approval, or alter a gate.
- Follow the current released README, canonical runbook, executable helpers, active series-aware
  manifest, and explicit user decisions for a live job. Historical changelog text is not an
  alternate or fallback instruction source, even when it mentions the same episode or stage.
- If an operational source is missing, contradictory, or ambiguous, stop and request
  clarification; do not use a changelog to resolve it.
- Keep changelogs as release/audit history. This proposal does not delete, rewrite, or migrate
  existing history.

## Break-candidate and retime-verification improvements

### Evidence and boundary

S01E12 exposed two independent break/retime defects:

- `measure_break_edges.py` used to crash when an adjacent-black clip inverted a content
  window. V13 now fails closed, but `match_content_windows()` still returns no measurements
  for either side if only one side is unusable. Close pairs therefore lose valid one-sided
  content evidence and remain blocked for manual adjudication.
- Episode-local `break_candidates_s01e12.py` and `record_breaks_s01e12.py` helped reconstruct
  the 17-break candidate list, but they contain absolute Z paths, episode-specific thresholds
  and a manual interval override. The recorder writes a manifest directly and chooses among
  repeated DVD-black claims by a nearest-gap heuristic. They are evidence scripts, not safe
  reusable workflow tools; do not copy them into `scripts/` unchanged.

The S01E12 retime verification also exposed cumulative `atempo` output-length error and a
quiet-scene merge at the old `-40 dB` output-scan threshold. V15 pins each segment's output
duration (`apad` + `atrim`) and uses a recorded `-50 dB` generated-output scan. Tests cover
duration, unchanged tempo, and the S01E12 failure shapes. The device log reports a real rerun
passing; release validation does not rerun episode media. This retime fix is included in V15.

### Reusable candidate/report and recorder implementation

Prefer a reusable candidate/report stage (or a clear extension of `measure_break_edges.py`)
whose inputs are resolved from the selected `SERIES:SxxExx` manifest and device profile, not
constants in a helper file. Allow explicit parameter overrides for measured detector/window
settings, and record every effective value in the report. The report should include the
manifest identity/digest, input identities, alignment coverage, detection parameters, all raw
candidate intervals, candidate pairings, confidence/status, and the reason each candidate is
accepted, ambiguous, or declined.

- Evaluate each before/after content window independently. Preserve a valid side when the
  opposite side is clipped, inverted, out of alignment coverage, or low-texture; mark the
  missing side with a specific status. Do not invent `off_before`, `off_after`, `step`, or
  `gap_s` from a DP plateau, DVD duration identity, or the valid opposite side.
- Pair LD and DVD black intervals using explicit temporal overlap/coverage and threshold
  evidence. Detect many-to-one claims, repeated candidate matches, close pairs, and threshold
  splits/merges. If more than one pairing is plausible, report `ambiguous` and require review;
  never resolve the collision by selecting the nearest gap or by silently merging breaks.
  Preserve a `merged_music_through`/no-DVD-silence case as an explicit candidate requiring
  reviewed evidence, not a fabricated silence interval.
- Check alignment range before every lookup. Out-of-range means `unmeasured`, not extrapolated.
  Record window frame counts, texture statistics, match score, runner-up, and margin; apply the
  existing minimum-texture and fade-clear rules without relaxing them to make a report pass.
- Dry-run/report-only is the default and must not modify the manifest. A separate explicit
  apply/record operation may write only after human review, unique candidate labels, unique
  unambiguous pairings, all required input checks, and a re-check that the manifest digest has
  not changed since review. Any offset/step fields stay null until the existing content-edge
  measurement/adjudication stage produces them.
- Before an authorized manifest write, create a no-overwrite backup, serialize to a temporary
  file in the same directory, flush/fsync, and atomically replace. Refuse symlinks, unrelated
  manifest identities, existing adjudicated offsets, duplicate labels, missing/changed inputs,
  and any failed or ambiguous candidate. Re-run `validate_episode` immediately after the write.
- Keep diagnosis deterministic: emit stable status codes and exact next actions. The tool may
  automatically retry a failed read-only probe only under a bounded policy; it must not
  automatically choose an edge, relax a threshold, change a manifest, kill an unrelated
  process, or convert `LIMIT`/`unmeasured` into `PASS`.

Implemented in V15:

- `measure_ld_black.py` emits a series-aware v2 report with the descriptor SHA-256 and the
  interval-duration/threshold parameters used.
- `prepare_retime_breaks.py` accepts that report plus explicit threshold, silence, tolerance,
  and alignment-spread parameters. It scans the unique DVD 5.1 stream sequentially and emits
  every plausible pairing with stable status codes. Its default is stdout/report-only; a saved
  report is new and no-overwrite. An explicit DVD audio selector is required if stream
  metadata does not identify exactly one six-channel stream.
- `measure_break_edges.py` now matches before/after independently. A valid side is retained as
  partial evidence when its partner is clipped or low-texture; exit remains nonzero until both
  sides are measured.
- `record_retime_breaks.py` is a separate preview/apply operation. Apply requires selected
  unique-pair labels or explicitly reviewed `--merged-through` labels, exact episode identity,
  the reviewed report SHA-256, a review note, verified inputs, and an unchanged manifest.
  It locks the manifest, backs it up, atomically records interval candidates, then validates the
  manifest. It never sets offsets/steps/gaps.
- `run_stage.py` allowlists prepare (stack-gated) and record (inputs-gated) stages; both keep
  the normal doctor/context protections.

### Regression and acceptance fixtures

Use synthetic descriptor/interval fixtures and short generated audio/video signals; no episode
media or network access is needed for unit tests. Cover:

- ordinary unique pairs, close double-black pairs, multiple LD candidates near one DVD black,
  one DVD candidate near multiple LD blacks, an explicit merge/music-through case, and no match;
- thresholds that expand, split, merge, or remove an interval, including the S01E12-style
  quiet-scene ambience and S01E08-style analog fade growth;
- before-only and after-only valid content windows, empty/inverted windows, low texture, a
  clipped alignment boundary, and a zero-candidate report (which must not be a false PASS);
- bad/nonzero FFmpeg execution, malformed/non-finite intervals, stale input/manifest digest,
  duplicate labels, symlink targets, pre-existing adjudicated values, and two concurrent apply
  attempts;
- default dry-run leaves manifest bytes unchanged; apply creates exactly one verified backup
  and an atomic valid JSON update; simulated write failure leaves the original intact;
- the retime duration pin preserves the measured tempo ratio and sample count, while the
  generated-output verifier reports `PASS`, `LIMIT`, or `FAIL` honestly at the existing
  position/duration tolerances.

Do not accept the S01E12 one-off recorder or any replacement based only on those hand-entered
constants. Every fixture must be episode-agnostic and every applied value must remain
traceable to the candidate report and explicit review.

## V15 validation record

- Live README and canonical runbook explicitly state that changelogs are retrospective, not
  live workflow inputs; generated model/plain-text instructions include the boundary.
- Regression coverage checks that live docs state the changelog boundary and that this queue
  remains non-operative.
- Synthetic tests cover unique/ambiguous/colliding pairings, offset transitions, uncovered
  alignment, no-silence/merged handling, explicit stream selection, partial edge evidence,
  empty-candidate nonzero status, no-mutation preview, review/hash gates, lock conflicts,
  backup/atomic replacement, stale inputs, and retime duration pinning.
- The full unit suite, compile/import checks, generated-document checks, PowerShell parsing, and
  dry-run/mock workflow tests are recorded in `V15-RELEASE.md`. No real media was used for V15
  validation; no download, episode processing, encoding, or remuxing was run for this release.

V15 is the released workflow. This queue does not add live instructions beyond the released
README, runbook, and helpers.
