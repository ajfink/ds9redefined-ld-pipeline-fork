> **NOT FOR LIVE WORKFLOW USE.** This is a release-planning/implementation record only. Do
> not cite or apply its contents as episode instructions, measurements, approvals, stage
> arguments, or gates. Active jobs must follow the current README, canonical runbook, and
> released tools.

# V10 implementation record

**Historical implementation record: V10. Current release is V12.** This file records the
accepted V10 scope from the W/Z log review.
Implementation and synthetic/unit validation are complete; no real episode was reprocessed
for this release. The evidence below is not a new default or authorization to modify active
episode data. Existing QC, space, ACL, and approval gates were not relaxed.

## Integrated scope and validation

### 1. Verify generated DVD-retimed audio against contiguous silence spans

**Evidence:** the S01E11 Mac run log records that the first B1 output passed the retime
generator's checks but failed an independent post-generation silence check. At the `-40 dB`
verification threshold, a threshold touch split a single silent run into adjacent intervals;
138 ms of silence remained in the following segment and merged with the inserted gap. The
operator expanded the cut to the full contiguous silent span and reran. B8's 23 ms gap was
below the ordinary detector's resolution and could not be confidently position-matched.

**V10 implementation:** `retime_dvd_assets.py` now scans the generated 5.1 output and writes
`<episode>-retime-audio-verification.json`. It coalesces only overlapping/touching silence
detections, fails on measurable mismatches, and reports sub-50 ms gaps as `LIMIT`. Delivery
QC shows `LIMIT`/missing reports as WARN rather than PASS.

**Validation:** synthetic tests cover
cut/gap timing with the recorded break map and coalesces only detector intervals shown to be
one contiguous silent source span: touching vs separated events, a B1-shaped corrected
output, a leftover silent tail, and a sub-resolution B8-sized gap. No cut is adjusted
automatically. Real-media revalidation remains outstanding.

### 2. Namespace EFM decode artifacts by episode

**Evidence:** the S01E09 LENOVOFLEX log says the runbook's flat `audio/<capture>-efm.wav`
name would collide with an older episode's decode. The operator caught the collision before
writing and used an episode-specific subdirectory for the six captures.

**V10 implementation:** the runbook uses `roots.work/audio/<episode>/<capture>-efm.wav` and
the matching `.wav.txt` report. New `scripts/efm_paths.py` plans those paths and refuses an
existing WAV or report; it does not decode or create/overwrite artifacts.

**Validation:** tests prove identical capture labels map to disjoint episode paths and
existing outputs remain unchanged. The runbook generator includes the new path contract;
no Orc/EFM decode was run for this integration.

### 3. Make PAD_1 audio occupancy evidence reproducible

**Evidence:** the S01E08 analysis found 83 PAD_1 positions with audio energy similar to
matched controls (median -30.56 dBFS versus -30.81 dBFS). That evidence supported the
episode's explicitly approved 44,056 Hz duration/PAD choice. Other estimates were near
44,054.5 Hz, so the finding resolves this episode's PAD-coverage ambiguity; it does not
establish a universal rate or justify automatic rate selection.

**V10 implementation:** `scripts/measure_pad_audio.py` maps frame-map PAD_1 positions through
the selected capture's `audio.pad_frames`, reads only short PCM windows, compares nearby
controls, and writes a hash-bearing report. It never changes audio, the manifest, or the
rate decision.

**Validation:** synthetic fixtures cover audible/silent PADs, WAV boundaries, and too few
controls. S01E08's real-media report has not been regenerated under V10; its 44,056 Hz choice
remains episode-specific evidence, not a default.

### 4. Remove the invalid-escape warning in A/V checker documentation

**Evidence:** the S01E11 retime dry-run emitted a Python `SyntaxWarning` from the
`episodes/<code>.json` path in `scripts/av_check_output.py`'s docstring.

**V10 implementation:** changed the module docstring to a raw string; path and A/V logic are
unchanged.

**Validation:** the module imports with `SyntaxWarning` treated as an error; existing A/V
tests remain unchanged.

### 5. Measure LD black/fade intervals without relying on one global threshold

**Evidence:** the S01E08 delivery report says `pix_th=0.10` failed 5 of 12 LD-black gap
checks, while the episode-specific `pix_th=0.15` measurement passed 12 of 12. S01E11 found
that `blackdetect` understated analog-fade black and used descriptor-grid luma to measure
the longer interval. S01E09 separately found DVD audio fade tails missed at `-45 dB` but
usable at `-40 dB`. These are distinct signals: LD picture-black and DVD audio-silence
thresholds must not be conflated.

**V10 implementation:** report-only `scripts/measure_ld_black.py` lists contiguous descriptor-
luma runs across several caller-visible thresholds and matches them to existing candidate
black intervals. It never edits `ld_black` or chooses a threshold. DVD audio-silence settings
remain distinct.

**Validation:** synthetic luma arrays cover threshold sensitivity, short PAD blips, dark
intervals, and candidate matching. No real S01E08/S01E11 media was reprocessed; thresholds
must still be reviewed per episode.

### 6. Refuse trim decisions that extrapolate alignment beyond sampled frames

**Evidence:** the S01E08 delivery log records a head mapping based on an alignment path
starting at master frame 3000. Extrapolating that offset to the head placed the calculated
source head past the DVD silence (1.339 s versus 1.239 s), risking a clipped fade. Re-running
`measure_dvd_alignment.py` with `--first-master-frame 1505` and independently matching the
head corrected the trim.

**V10 implementation:** alignment reports print their sampled master-frame range.
`av_check_output.build_offset_model()` rejects queries outside the sampled alignment range
and directs the operator to rerun with `--first-master-frame` or obtain an independent local
match.

**Validation:** tests cover anchors at both sample bounds and queries before/after them. A
real-media trim rerun is still required before claiming the S01E08 correction itself was
revalidated under V10.

### 7. Keep break-edge content windows clear of neighboring black intervals

**Evidence:** the S01E08 delivery record says the B3 before-window included B2's black and
the B11 after-window included B12's. The contaminated measurements were caught by an
independent gap/black/step identity before the break values were accepted.

**V10 implementation:** `measure_break_edges.py` clips before/after content windows away from
neighboring recorded black intervals and records the clipped bounds. Overlapping manifest
blacks are rejected; windows with too little texture remain `insufficient-texture` and are
not adjudicated.

**Validation:** synthetic cases cover adjacent intervals, clipped windows, insufficient
content, and overlapping blacks. No episode manifest was edited.

### 8. Make workspace initialization safe for legacy metadata and repeated calls

**Evidence:** the S01E11 bug register records `stage_clock.py start` raising `KeyError` on a
V7-era `workspace.json` without `storage_mode`; the operator manually amended that workspace.
It also records a repeated `shared_workspace.py init` creating an empty second
`MAC-OPENCODE` instance. That duplicate made automatic selection ambiguous and the stage
clock fell back locally.

**V10 implementation:** workspace resolution infers missing legacy `storage_mode` from the
validated path/profile in memory, preserving the saved creation version/metadata. Repeated
`shared_workspace.py init` now refuses if a matching workspace exists and points to `ensure`
or explicit `--instance-root`; it never deletes or merges a workspace.

**Validation:** synthetic V7-era shared/local workspace fixtures verify truthful mode and
creation-version preservation. A repeated-init fixture confirms no sibling is created and
ambiguous instances remain unselected.

### 9. Standardize correctness checks for one-off measurement analysis

**Evidence:** the S01E11 reverse-match/cut-PAD investigation caught several scratch-analysis
errors before any affected value reached the manifest: NCC similarity treated as `1-NCC`, a
one-sided rather than absolute delta count, an NCC/frame-index column mix-up, and a reversed
DVD/master offset sign. The device log adopted three controls: state sign/scale conventions,
print raw rows before statistics, and independently cross-check derived results.

**V10 implementation:** the canonical runbook now requires one-off analyses to state units
and sign/scale conventions, show raw rows, verify a known anchor, and cross-check independently
before manifest promotion.

**Validation:** the checklist is present in the canonical and generated runbooks. It does
not certify existing scratch scripts or replace review of their data and assumptions.

## Guardrails and related observations

- The S01E11 and S01E09 records predate V10 and use direct helper calls in places; they are
  not evidence of a complete V10 orchestration run or comparable V10 active-time measurements.
- W has two `MAC-OPENCODE` S01E11 instance IDs; the newer workspace appears to contain only
  scaffolding. Confirm whether the second instance is intentional before either workspace
  receives episode artifacts. Do not merge or delete either workspace automatically.
- The existing shared-root ACL blocker and same-episode cross-job coordination issue remain
  separate prerequisites. A per-job workflow lock does not resolve them.
- The S01E11 `heavy_jobs` 1-to-2 choice was made with 16 GiB RAM, about 129 MiB available,
  swap 97 percent used, and load average near 46. It was a device-local, staged choice—not
  evidence that two heavy jobs are generally safe or faster. The historical hardware-aware
  concurrency proposal remains deferred; do not change the default from this one run.
- These device jobs used direct helpers rather than `run_workflow.py`, in part because of the
  existing stage-runner/ACL blocker. They do not validate V10 orchestration or provide a
  comparable V10 active-time benchmark.
- The S01E09 trim/alignment output is still marked as a candidate pending adjudication; do
  not promote its measured values as defaults.
- No media, manifests, logs, profiles, or episode artifacts are part of this queue file.
