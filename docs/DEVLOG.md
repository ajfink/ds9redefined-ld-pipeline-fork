# Development log

Newest session first. Format and rules: see `AGENTS.md` → "Logging".

---

## 2026-11-21 — Session 3 (Claude; owner reviewing the session-2 report)

**Owner decisions on the review's questions** (verbatim intent; these override older notes):
- **Quality first, everywhere.** Accepted v38 values are not assumed ideal; where fstk can
  determine a better value automatically it does (no-DVD rate, EFM decode, Orc version).
- **What "done" means:** the final LaserDisc video with time-aligned LD stereo (always the default
  stereo track), plus time-aligned DVD 5.1 and DVD subtitles when a DVD exists. Without a DVD:
  the final video with time-aligned LD audio.
- **Human reviews:** the A/V time-alignment check across several 15-second chunks is a human
  check. **EFM choice is automatic** — when no capture wins on every measure, fstk picks the
  least-bad one by a declared rule. **Encode needs no review** (x265 Main 10, CRF 14, grain tune).
- **DVD picture alignment:** automate as much as possible.
- **Porting order** accepted: alignment → DVD audio rate (centre-referenced origin) → retime →
  PAD audit/repair; import S01E12/17/18/20 once alignment exists; per-capture EFM; head+tail
  output fingerprints; H.264 proxy only as a fallback. Validation data: S01E20 and S01E19
  (captures and DVD rips on the owner's drives).
- Space ledger lives beside the machine profile; catalog refreshes when older than 30 days;
  v38 frozen now (`docs/v38-reference.md`); `Z:\FromStunToKill` is a git repo.

### Plan and progress (living; update at every checkpoint — other agents start here)
Owner's order: alignment → DVD audio rate/origin → retime → PAD; only human review = A/V check.

| # | Item | Status |
|---|---|---|
| P1 | DVD picture alignment, automatic acceptance (`dvd-alignment`) | **done**, validated on S01E20 (CP3–CP4) |
| P2 | A/V check as the only review + self-correcting reruns (`av-correction`) | **done** (CP5); DVD episodes need P5 |
| P3 | Exact-ratio resampling: python-soxr VHQ, 24-bit FLAC, one pass (`media/resample.py`, `stages/ldaudio_build.py`) | **done** (CP6) |
| P4 | `dvd-free-audio` on the exact resampler (retire the 0.5 Hz `asetrate` quantisation); QC accepts 24-bit | **done** (CP6) |
| P5 | `dvd-ld-audio`: DVD-referenced LD rate + origin (windows of LD vs DVD stereo, mapped to master through the act curves, centre-referenced robust fit, cross-checks vs duration model + picture slope), builds corrected/synced, applies A/V corrections | **done** (CP7), validated on S01E20 M1+P2 |
| P6 | `video-trim` (DVD episodes): head at the DVD programme's fade-up, tail only for LD-only picture, exact `video.delta_s` via a tagged IVTC probe, verified encode script | **done** (CP9) — needs a first real run on Windows (VapourSynth) |
| P7 | Rerun plan for DVD episodes (A/V check → av-correction → dvd-ld-audio → remux → QC) | **done** (CP7) |
| P8 | Retime DVD 5.1 + SRT onto the LD timeline by **audio** (`dvd-retime`, `media/retime_map.py`, `media/retime_render.py`) | **done** (CP8), map verified on real S01E20 audio |
| P9 | PAD audit/repair port | todo |
| P10 | `cadence-evidence` accepted automatically inside the v38 ranges (12 episodes), review only outside | **done** (CP9) |
| P6b | No-DVD head/tail trims: fade-up after the LD front matter inside the v38 prior (45–58 s; 12 episodes 49.5–52.4 s) | **done** (CP11); finds S01E20's DVD-measured start (1515) |
| P13 | `delivery_plan` + "Run to delivery" button + `fstk plan delivery` (stack render → … → QC for either route) | **done** (CP9) |
| P11 | Import the v38 episodes: all 13 real device manifests import + round-trip; `--v38-profile` resolves their files to absolute paths so they continue in fstk | **done** (CP10); first real continuation run on Windows still owed |
| P12 | EFM: every capture decoded even if one fails, failures recorded + excluded from the automatic choice, Orc identity (version if reported + exe SHA-256) in provenance | **done** (CP12) |
| P14 | Catalog refresh when the snapshot is > 30 days old (`fstk catalog refresh`, startup check) | **done** (CP13) |
| P15 | H.264 review-proxy fallback in the A/V check window | **done** (CP15) |
| P9b | PAD audit/repair port (v38 `audit_pads` / cut-PAD stills) | **done** (CP14; not yet run on Windows VapourSynth/RIFE) |

Validation data on the owner's Legion: `Z:\ld-pipeline-device\{probe,prores,audio}` (S01E20 descriptors,
stack master, EFM WAVs), DVDs in `Z:\Video\DS9S1D5`, captures on `X:\`. Windows-only tools
(Orc, VapourSynth) cannot run in the Linux device VM: P6 and anything after encode can only be
validated by the owner running the app.

### Checkpoints
- **S3-CP1 — quick decisions.** Ledger beside the profile (old `data_root` ledger migrated once);
  stage outputs ≥ 1 GiB fingerprinted head+tail (`stages.base.output_scope_for`); v38 freeze
  recorded (`docs/v38-reference.md`, AGENTS rules 1–2); inventory statuses refreshed for the
  session-2 ports. 277 tests.
- **S3-CP2 — encode unreviewed; EFM choice automatic.** `encode-final` has no review; `remux`
  needs only the verified `padfixed_video` artifact. `efm-select` falls back to
  `least-audible-damage-v1` (finding 0004): Pareto front, then fewest silenced samples →
  missing sections → interpolated samples → post-C2 → C2 → C1. On real S01E20 data the front is
  M1/P2/P6 and the rule picks **M1** (v38 manual pick was P2). Finding 0004 also proposes
  multi-capture repair using the decoder's flagged regions. 279 tests.
- **S3-CP3 — DVD picture alignment (`dvd-alignment`, `media/alignment.py`).** Ports v38
  descriptors + Viterbi alignment, with three changes:
  - an *unmatched* state, so every master frame is sampled from frame 0 and the DVD origin is
    measured (v38 skipped the first 100 s);
  - an exact linear-time Viterbi (distance transform), identical to v38's matrix form (tested);
  - automatic acceptance. Each segment is confirmed frame by frame, a reverse path must agree,
    and the stage fails a run that hits a search edge, has short non-jitter segments, or has
    > 10 s of interior LD-only picture.

  On failure it is held for the `dvd-alignment` review (the engine now supports
  `StepOutput.needs_review=False`). A wrong DVD blocks. Descriptors of unchanged sources are
  reused. `tools/validate_v38_alignment.py` compares against v38's approved S01E20 path, but
  can't run yet: access to `Z:\ld-pipeline-device` and `Z:\Video\DS9S1D5` was refused by the
  session's safety layer. 287 tests.
- **S3-CP4 — alignment validated on real S01E20; act model (finding 0005).** The owner granted
  `Z:\` and `X:\`. On S01E20's real descriptors the DVD−LD offset drifts within acts (common
  slope −988 ppm ≈ 1000:1001, plus wander), and soft-telecine duplicates tie neighbouring
  offsets. Alignment now fits a continuous curve per act (30 s knots, robust, smoothed) between
  refined breaks. Acceptance is the frame-by-frame consistency of the curves plus the reverse
  path.

  S01E20 is **AUTO-ACCEPTED**: consistency 99.69%, reverse 96.5%, origin master frame 1515
  (= v38's 50.5505 s anchor), 97.5% within ±1 of the approved v38 path, and v38's 38-frame
  excursion at 347–350 s avoided. It stays accepted when the DVD descriptors come from another
  FFmpeg build (duplicates placed differently in 13% of rows). Real-sample fixture
  `tests/fixtures/s01e20-alignment-samples.npz`; FFmpeg version recorded in descriptor
  metadata. 289 tests.
- **S3-CP5 — one human review: the A/V check (owner, 2026-11-21).** The owner confirmed the
  automatic EFM rule. `stack-render` and `remux` have no review any more. The delivery review is
  now the A/V check. `qc-report` offers 8 × 15 s chunks across the episode, avoiding black
  (`qc.gates.av_check_chunks`). The reviewer marks each chunk in sync / audio too early / audio
  too late.

  **Approve** needs every chunk in sync. **Rerun with corrections** records the marks as a
  rejected delivery and starts av-correction → dvd-free-audio → remux → qc-report.
  `media/av_correction.py` solves the extra delay D(t) = offset + drift·t from every round's
  marks, with asymmetric detectability windows (45 ms early, 125 ms late). The feasible region
  is bisected inside a trust box that doubles while the marks still push against it. Drift comes
  into play when no offset alone fits. In simulation it converges in 1–6 reruns for offsets up to
  0.9 s and drifts up to ±200 ppm; contradictory marks are reported, not averaged.

  The no-DVD audio applies the correction: rate ÷ (1 + drift), skip × (1 + drift) − offset. The
  QC DVD-free gate checks it against the manifest. Remux reruns write `-r2`, `-r3` … versions.
  DVD-episode reruns wait for the DVD-referenced LD audio stage. 301 tests.
- **S3-CP6 — exact-ratio LD audio (P3, P4).** `media/resample.py` resamples the native EFM WAV
  from the measured rate to 44.1 kHz with python-soxr VHQ at the exact float ratio, in one
  streaming pass. It writes the whole-side `corrected` and the sample-exact `synced` (lead/tail
  silence as needed; a negative origin is allowed) as 24-bit FLAC with deterministic TPDF dither,
  so rebuilds are byte-identical.

  This retires v38's `aresample 88200 → asetrate round(2×rate)` chain, which quantised the rate
  to 0.5 Hz (≈ ±5.7 ppm, ≈ 16 ms over an episode). Test: a 44144.37 Hz clock is restored to
  within 10 mHz at 1 kHz. `dvd-free-audio` uses it through `stages/ldaudio_build.py`, and so
  will `dvd-ld-audio`. `audio.intermediate_asetrate_hz` is no longer written, and
  `audio.asetrate` holds the exact applied rate. QC accepts 16- or 24-bit LD FLAC and checks the
  applied rate = duration rate ÷ (1 + drift correction). New runtime dependency: `soxr`
  (pyproject; `fstk doctor` requires it). **Owner: `pip install soxr` on each machine.** 303 tests.
- **S3-CP7 — DVD-referenced LD audio (P5, P7).** `media/dvd_audio.py` matches 20 s windows of
  the native EFM audio (every 12 s) against the DVD stereo. Matching is two-stage: 50 ms
  envelopes within ±8 s, then 5 ms envelopes with a parabolic peak. The search is bootstrapped
  by one 60 s window against the whole DVD, so no origin is guessed. Each match is mapped to
  master time through the accepted act curves (`ActMap`), and master = intercept + slope·wav is
  fitted centre-referenced and robustly (Huber; outliers beyond max(4·MAD, 20 ms) dropped).
  Windows over act breaks, near-silent windows and weak peaks are rejected. A bug fix along the
  way: window starts are snapped to the envelope grids; `int()` truncation had biased the rate
  by 10 ppm in the synthetic test.

  **Validated on S01E20.** M1 measures 44,055.966 Hz (−998.5 ppm ±1.6) with WAV0 at master frame
  18.89 ±0.08. P2 measures 44,055.980 Hz (−998.2 ppm) with WAV0 at 118.99. The two captures agree
  within 0.3 ppm, the pad difference is reproduced (100.10 vs 100 frames), and the rate is
  1000:1001 within 1.5 ppm. Both origins sit +0.9–1.0 frame from the plain capture-pad geometry:
  either v38's known one-frame origin convention or the DVD's own A/V offset, which a single DVD
  cannot separate. The DVD measurement is primary and the A/V check is the safety net. Per-window
  residual MAD is 16 ms and autocorrelated (0.78). That is picture-map error, not audio: one act
  (1803–1953 s) sits ≈ −33 ms off, so P8 must retime by audio matching.

  v38's duration/PAD model does **not** hold for captures with a nonzero pad (its P2 rate
  43,993.6 Hz and −118-frame endpoint residual are that pad), so on S01E20 `dvd-free-audio` would
  correctly refuse.

  `stages/dvdaudio.py` (`dvd-ld-audio`) accepts automatically when there are ≥ 40 windows, ≥ 80%
  coverage, origin SE ≤ 5 ms, rate SE ≤ 5 ppm and a leave-one-out spread ≤ 5 ppm; otherwise it
  is blocked with the numbers. It builds the 24-bit exact-rate track with skip = delta −
  intercept (+ A/V correction) and records cross-checks (origin − pad geometry, rate − 1000:1001)
  and per-act residuals. The A/V-check rerun plan now uses `dvd-ld-audio` for DVD episodes.
- **S3-CP8 — audio-precise retime (P8).** `dvd-retime` replaces v38's two-review retime chain
  (prepare breaks → review → measure edges → review → `retime_dvd_assets`).

  `media/retime_map.py` works in LD WAV time, the output's clock. The windows `dvd-ld-audio`
  matched are split into constant-relation segments wherever `dvd − β·wav` steps by > 15 ms
  (confirmed by the next window). Each edit is located by a fine scan: 3 s windows every 0.25 s,
  checking which segment line explains the audio; ties go to the quietest LD point. DVD-only
  material is dropped; LD-only material becomes LD fill.

  **S01E20, real audio:** 7 edits found, including a 100 ms DVD-only trim at LD 348 s that the
  picture model had folded into an act, and −67 ms / +65 ms LD- and DVD-only bits at 236 s and
  2711 s. Act breaks are +235, −100, +534 and +501 ms. LD fill totals 0.19 s. Verified by
  matching the mapped DVD stereo against the LD audio on 368 ten-second chunks: median −2.5 ms,
  MAD 1.9 ms, max 6.3 ms (v38's picture-based path had ±1-frame = 33 ms local error).

  `media/retime_render.py` renders the 5.1 in one sequential DVD decode. Each DVD piece is
  resampled at its exact ratio with soxr VHQ (no `atempo` WSOLA artifacts; 1.7-cent pitch shift).
  LD-fill pieces copy the time-aligned LD stereo into FL/FR, joins get 2 ms fades, and the length
  is exactly frames × 2002 samples at 48 kHz, written as 24-bit FLAC. Subtitles come from the
  `srt_source` artifact or the DVD's English text stream; cues on DVD-only material are reported.
  The stage then checks the rendered 5.1 against the delivered LD audio (median ≤ 10 ms, ≥ 90%
  within 20 ms, both sides of every edit) and writes `reports/retime-audio-verification.json` for
  QC's "retime output gaps" gate. It sets `deliverable.mux_tracks` = ld, 51, srt.

  The full 5.1 render was tested on synthetic media only; the real S01E20 5.1 render needs
  `video.delta_s` (P6). Bug caught: `stages/__init__.py` had been reflowed by ruff, so the one-line
  import edit for `dvdaudio` silently failed and `dvd-ld-audio` registered only in tests. Added a
  test that every stage module registers through the package import. 310 tests.
- **S3-CP9 — trims/DELTA, cadence automation, one-button delivery (P6, P10, P13).**

  `video-trim` replaces v38's visual trim review and the hand-set `delta_s`. It writes the IVTC
  script untrimmed in **probe mode** (`cadence_map.ivtc_vpy_script(probe_json=…)`): every
  source frame is tagged `FstkN` before TFM/TDecimate, and evaluating it (`vspipe --info`)
  dumps the exact master frame of the head/tail output frames. Inferring TDecimate's choices
  from descriptors would be ambiguous by ±1 frame, i.e. 33 ms of A/V error.

  `media/trim.decide`:
  - **Head:** the alignment origin, walked back ≤ 15 frames to the first fade-up frame, keeping
    2 black master frames before it when they are black.
  - **Tail:** none (as in all 12 v38 episodes) unless ≥ 5 s of textured picture after the last
    act has no DVD match.
  - **`delta_s`** = the exact master frame of output frame 0 / FPS.

  Then it writes the final trimmed script with an asserted frame count and confirms it with
  `vspipe --info`. v38 priors across 12 episodes: `delta_s` 49.5–52.4 s, `tail_trim` 0,
  `head_trim` 1175–1253. S01E15's `delta_s` is exactly master frame 1515, the same as fstk's
  S01E20 origin.

  Untested on real VapourSynth. Check on the first Windows run that TIVTC keeps frame
  properties: the probe fails loudly if `FstkN` is lost.

  `cadence-evidence` is accepted automatically when period-5 ≥ 85%, unmatched = 0, repair ≤ 6%
  and inserts ≤ 12. Those ranges are calibrated on the 12 v38 episodes (89.9–93.1%, 0,
  2.2–4.6%, 2–8). `workflow.plans.delivery_plan` chains everything after intake for the
  episode's route, with disk estimates from real sizes (master 287 KB/frame, delivery
  ≈ 72 KB/frame, both with margin). It is available as `fstk plan delivery <ep>` and the app's
  **Run to delivery** button. The no-DVD route still needs trims recorded (P6b).
- **S3-CP10 — v38 episodes import and can continue (P11).** All 13 real device manifests
  (S01E08–S02E02 on Legion, Flex, Mac) import and round-trip. Fixed: two registry limits real data
  breaks (S01E18 `retime.max_gap_vs_black_s` −0.72; S02E02 `cut_pad_images_choice`
  "not-available") and a BOM in the CLI import.

  `fstk manifest import … --v38-profile <that device's machine.json>` resolves every
  `{root, name}` file to an absolute path on that machine. v38's flat `probe/` and per-episode
  `audio/S01E20/` can't be expressed as fstk root overrides. The original references are kept
  (`legacy.v38_files_original`) and exported back, so the v38 view still round-trips. README
  documents continuing an episode; S01E20 resolves to the real files on `Z:`.

  Caveats for the first continuation run:
  - descriptors in v38's metadata format are not reused (fstk re-decodes them);
  - fstk's `efm-select` needs fstk-format EFM provenance, so it may re-decode;
  - S01E20 will switch to M1 by the automatic rule.
- **S3-CP11 — no-DVD trims (P6b).** `video-trim` now serves both routes.
  - **No DVD:** it reads the first two minutes of the master (`gray_thumbnails(max_frames=…)`,
    sequential) and takes the fade-up from black (≥ 0.5 s black, then ≥ 80% textured) in
    45–58 s. If there are none or several, it widens to 30–90 s, takes the one nearest 50.6 s,
    and records a note.
  - **Real-data check:** on S01E20's master the only candidate in the window is frame 1515
    (50.55 s), exactly the DVD-measured programme start. The others are front matter (2.1 s) and
    the main titles (109.6 s).
  - `delivery_plan`'s no-DVD route now includes `video-trim`, so both routes run start to finish.
- **S3-CP12 — EFM robustness (P12).** `efm-decode` now attempts every capture.
  - **A failure** (an Orc error, an MD5 mismatch, a missing WAV, …) is recorded in
    `audio.efm_decode_failures` (run, reason, source size) with a FAILED provenance JSON, and
    the other captures carry on. The step fails only if nothing decodes.
  - **Selection:** `efm-select` excludes captures whose *current* source failed to decode, and
    still blocks on any capture never attempted. A later successful decode clears the failure.
  - **Orc identity:** provenance records `{version, exe, exe_sha256}`. The version is taken only
    from `orc-cli --version` output that contains a version number; doctor notes Orc may print
    none, and the exe hash identifies the build regardless.
  - **Version policy** (owner: best output): newer Orc is allowed, because the report parser
    fails closed. 321 tests.
- **S3-CP13 — Catalog refresh (P14).** New `catalog/build.py` (port of v38
  `build_archive_catalog.py`, metadata only, injectable fetcher so tests run offline).
  - Snapshots are dated files `<data_root>/catalog/section_31_tree-<UTC stamp>[-n].json.gz`, written
    atomically and never overwritten. Resolution order: profile `catalog_path` (explicit pin) >
    newest refreshed snapshot > the packaged one.
  - Stale = `source.generated_at_utc` older than 30 days (or unreadable).
  - Any listing/item failure aborts the whole refresh; no partial tree is saved.
  - CLI: `fstk catalog status`, `fstk catalog refresh [--force]` (no-op unless stale).
  - GUI: a background check 1.5 s after launch refreshes if stale; offline failures are logged and the
    existing snapshot stays in use.
  - Not exercised against live Archive.org (the container's network is allowlisted): the first run
    on the owner's machine is the real-network check. Next: P15, P9b (both now done; see CP14/CP15).

- **S3-CP14 — PAD audit and repair (P9b).** Owner rules (2026-11-21): RIFE midpoint for a PAD
  that is not on a shot cut; on a cut with a DVD the correct frame is **borrowed from the DVD**,
  scaled to the LD raster and colour-matched to the neighbouring LD frame; on a cut without a
  DVD the previous frame is held one more frame. New: `media/pads.py` (audit math: blackdetect
  blips ≤ 0.050 s, v38 `(m−pad)*4//5−head` projection, ±2-frame greedy matching, full accounting
  trimmed/inside-break/never-rendered/orphans, flank score `1−NCC(i−1,i+1)` on 48×36 descriptors,
  cut threshold 0.45 with the ambiguous band 0.30–0.75 **treated as a cut**, full-raster
  near-black check), `media/pad_repair.py` (method choice, DVD flank matching — both flanks must
  be found uniquely and exactly 2 DVD frames apart — ECC affine registration, robust per-channel
  gain/offset on stable pixels, post-fit NCC gate, padfix script generator, replacement TSV),
  `tools/rife.py` (v38 recipe: CPU fp32 + TTA), `tools/vapoursynth.py` `serve/scan/frames`
  (one decode serves blackdetect and thumbnails; v38 did two), `stages/pads.py` (`pad-audit`,
  `pad-repair`). `pad-repair` writes `padfix_script` (the base IVTC script plus a Splice; payload
  is a lossless RGB ffv1 clip; holds use `out[i-1]` in the chain's own 10-bit format; frame count
  asserted), `pad_repair_frames` (v38 list read by the QC identity gate), `pad_replacements`
  (TSV), `pad_repair_report`. `encode-final` encodes `padfix_script` when present and **refuses a
  padfix built from an older base script** (SHA recorded in the script header). Both delivery
  routes run `pad-audit → pad-repair` before `encode-final`.
  - Deliberate differences from v38: no curated "Cut Pad Images" stills (owner rule is hold);
    the arithmetic frame map with ±2 tolerance instead of the FstkN tag map; a DVD borrow that
    cannot be proven (no unique flank match, registration/colour fit fails the NCC gate) falls
    back to hold with the reason recorded, never to a guess; the DVD geometry/colour estimate is
    new (v38 did it by hand per episode) and is **unvalidated on real material**.
  - Tests: synthetic cut/DVD scenes (borrow error < 6/255 after tint correction), audit
    accounting, padfix script text, stale-padfix refusal, stage chain with fake VapourSynth/RIFE.
- **S3-CP15 — H.264 review proxies (P15).** `qc-report` now cuts each A/V-check chunk into an
  H.264 8-bit + AAC excerpt (`FFmpeg.av_proxy`, best effort, atomic) under `reports/av-proxy-<run>/`;
  `DeliveryReview.play_chunk` plays the proxy when the chunk has one, else seeks in the
  deliverable. The deliverable itself is unchanged.

---

## 2026-11-21 — Session 2 (Claude; owner present at start, then away)

**Goal:** apply the owner-approved recommendations for findings 0001/0002; build the episode
picker over the DS9/VOY/TNG catalog, intake, EFM decode, QC report, and review windows.

**State at hand-off:** 275 tests pass, ruff clean, synced to `Z:\FromStunToKill` (checksum-verified).
Intake plan is now download → inventory → (stack-reference ∥ efm-decode) → efm-select; `qc-report`
and the two review windows exist. Nothing committed; v38 untouched.

**Needs the owner:**
1. Apply `docs/v38-patches/2026-11-21-findings-0001-0002/` to v38 yourself if you keep that line
   (`build_map.ps1` change is untested on Windows).
2. Finding 0003: run `grep -n "issing" <capture>-efm.wav.txt` on any real EFMSink report so the
   missing-sections label can be confirmed.
3. First real run on Windows: `efm-decode` against an episode with EFM (Orc's log wording is
   matched by v38's regexes; confirm on current Orc).
4. QC cannot pass yet for DVD-free episodes (DVD-free sync not ported); DVD episodes need `dvd` and
   `align_npy` artifacts (present on imported v38 episodes).

### Checkpoints
- **S2-CP1 — findings applied.**
  - fstk: `media/audio_origin.py` — window-centre convention, centre-referenced fit, and the
    cross-method origin agreement gate; `ORIGIN_AGREEMENT_S` = ¼ output frame ≈ 10.4 ms,
    **provisional until calibrated** on S01E20 saved windows + E07 (no saved audio-rate TSV exists
    on the device yet). Findings 0001/0002 updated with the decisions.
  - v38: patch prepared in `docs/v38-patches/2026-11-21-findings-0001-0002/` (centre-referenced
    block in `measure_audio_rate.py` reports + explicit tail-trim/assert in `build_map.ps1` + a new
    v38 test + a `Versioning/` change note). Verified: applies cleanly; v38 non-GUI suite 515 passed
    (512 + 3 new). **Writing into the live v38 tree was denied by the session's safety layer**, so
    the owner applies it (`git apply`). `build_map.ps1` change inspected only (no PowerShell here;
    fetching PowerShell to test it was also denied).
  - Catalog: `fstk/catalog/archive.py` (v38 `archive_catalog.py` verbatim; 7 v38 tests + 2
    hand-ported snapshot tests), bundled snapshot `templates/section_31_tree.json.gz` (275 items,
    3.4 MB → 0.5 MB), `fstk/catalog/index.py` picker index (titles from capture filenames,
    selectability rules from v38, natural-language/title search, coverage gaps, per-machine local
    state: workspace / downloaded / partial / remote / lookup-only).
- **S2-CP2 — intake + episode picker.**
  - `tools/archive_http.py`: Python port of `download_archive.ps1` (https archive.org only; TBC/JSON/EFM
    selection; size+MD5 required; TBC↔JSON pairing; never deletes/overwrites; Range resume with the MD5
    continued over streamed bytes; retry on dropped connections/5xx/429, immediate failure on other 4xx;
    v38-compatible `download-manifest.csv`, `download-verified.csv`, `download-status.json`,
    `SOURCE-ITEM.txt`). Tested against a local fake Archive incl. a mid-stream disconnect. Tightened the
    identifier rule (`..` matched v38's regex; .NET had normalised it away).
  - `workspace/local_source.py` (v38 verbatim), `workspace/episodes.py` (workspace from a catalog entry or
    a local folder), stages `archive-download` (space gate on bytes still missing; new `SpaceNeed.target`)
    and `capture-inventory` (identity check, download-manifest cross-check, MD5s carried into the
    manifest's captures); engine: stages may propose declared `sources` updates.
  - `workflow/plans.py` intake template; CLI `fstk catalog search|coverage`, `fstk episode create
    --catalog|--local`, `fstk plan intake`; profile `catalog_path` (optional refreshed snapshot).
  - PySide6 `ui/picker.py` (Series→Season→Episode tree, search, filter, local-state colours, details,
    create/open/local-folder) wired into the main window with "Episodes…" and "Run intake".
    Screenshot `docs/screenshots/episode-picker.png`.
- **S2-CP3 — EFM audio decode + capture selection.**
  - `tools/orc_efm.py`: v38 decode-project builder (byte-identical, characterization-hashed), 7-Zip
    member selection (+ Windows drive paths refused), Orc-log path binding, single report parser,
    comparison table, PCM WAV check, unique-Pareto rule, alignment-map → pad frames.
    `tools/sevenzip.py` (extraction keeps a `.partial` on failure instead of deleting it);
    `Orc.decode_efm` (v38 command line with Orc's own `--log-file`).
  - Stage `efm-decode`: per-capture source checks (size; Archive MD5 for JSON/EFM; EFM MD5+SHA-256
    before/after), `.efm.7z` → `work/efm-decodes/`, project under `references/`, log/WAV/report
    validation, provenance JSON under `reports/`, `audio.efm_provenance` history. Space estimate from
    each capture's own field count. New run folder each time; verified decodes are kept on rerun.
  - Stage `efm-select`: re-hashes every latest WAV/report; unique Pareto winner auto-selected (note
    says so); otherwise blocked with the comparison table until an operator passes `capture` + a
    note (≥ 8 chars). Records `audio.capture`, `audio.pad_frames` (from `stack.alignment_map` in the
    saved project order), `audio.capture_selection` (+ history) and artifact `ld_native`.
  - Intake plan now: inventory → (stack-reference ∥ efm-decode) → efm-select; `--no-efm`; EFM steps
    are left out when the inventory/catalog says the episode has no raw EFM.
  - **Finding 0003**: v38's provenance parser and its report table read "missing sections" from
    different labels, so v38's auto-selection likely never fires on real reports (fail-safe). fstk
    reads both; owner to confirm the label from one real report.
  - Not ported (deliberately): v38 `efm_provisional.py`/`bind_efm_provisional.py` (decode while the
    download continues). The engine already runs `efm-decode` as soon as inventory passes; a
    download-overlapped decode can be added as a separate stage if the time saving matters.
- **S2-CP4 — delivery QC report.**
  - `qc/gates.py` (pure): PAD-aligned single-black rule, stream inventory, four-point input/output
    seek test, SRT timeline, retime-gap status (LIMIT never PASS), allowed-skip/unverified summary,
    review intervals, Markdown. `FFmpeg.blackdetect_decode` (one sequential decode: frame count from
    `-progress`, decoder errors from FFmpeg's own `[error]` level tags) and
    `FFmpeg.audio_window_seconds`.
  - Stage `qc-report` (review `delivery`): writes `reports/qc-<run>.json|.md` every time, then fails
    unless all hard gates pass and nothing required is unverified, so a delivery approval cannot be
    recorded on a failing report. Measured `qc.*` values commit only after approval. New registry key
    `qc.delivered_bytes` (v38 had it; it was in `legacy`).
  - **Not ported yet (reported SKIP → unverified, so QC cannot pass):** replacement identity
    (`verify_pad_identities`), absolute A/V (`av_check_output`), DVD-free sync
    (`ld_only_sync_check`), between-track sync (`sync_check`). Tested against a real
    x265 Main10/FLAC Matroska built by FFmpeg (clean file: every ported gate PASS; a black frame at a
    PAD: FAIL + review interval).
- **S2-CP5 — review windows (PySide6, `ui/reviews.py`).**
  - `EfmCaptureReview`: every verified decode's metrics (best value per Pareto metric in green bold,
    ★ on the automatic pick, current choice marked), pad frames from the stack alignment map, A/B
    audition that keeps the playback position when you switch rows, "Go to" time, required note.
    "Choose selected capture" / "Accept automatic choice" write an `efm-select` plan that the main
    window runs (the stage re-verifies every WAV/report before recording anything).
  - `DeliveryReview`: QC report picker, colour-coded gates, flagged intervals (double-click plays the
    deliverable from that point), standing warnings; Approve only for a QC report the engine is
    holding for review *and* with no failing/unverified gate; Reject whenever one is pending.
  - Main window: "EFM capture…" and "Delivery review…" buttons; delivery decisions in the Reviews tab
    route to the delivery window. QtMultimedia is created lazily (windows open without a media
    backend). Screenshots: `docs/screenshots/efm-capture-review.png`, `delivery-review.png`.
- **S2-CP6 — three more QC gates ported (only DVD-free sync remains).**
  - `qc/identity.py`: replacement identity for **every** repaired PAD (v38 sampled three) from one
    sequential 48×36 grey thumbnail decode (`FFmpeg.gray_thumbnails`, exact frame index, no seeking);
    v38 descriptors/thresholds (OpenCV INTER_AREA for the images, own ≥ 0.99, separation ≥ 0.10);
    reads v38's `rife-pad-replacements-local.tsv` layout (or artifact `pad_replacements`). Failing PADs
    become review intervals.
  - `qc/sync.py`: between-track drift (v38 `sync_check`) and absolute A/V through the picture map
    (v38 `av_check_output`: act-cut splitting, content-only offset model, no extrapolation beyond the
    sampled alignment path). DVD audio is decoded sequentially (never seeked); only fstk's own
    deliverable is window-seeked (`FFmpeg.pcm`). v38's act-cut tests ported; synthetic checks recover
    known lags/offsets and reject 400 ppm drift. Wrote down the A/V sign precisely (v38's "positive =
    audio content later (LATE)" reads both ways: positive = the sound belongs to a later DVD moment
    than the picture).
  - Registry: `qc.dvd_audio_map` (setting), `retime.end_black_ld` (v38 had both; they were in `legacy`).
  - QC integration fixture now uses a per-frame-distinct source (testsrc2 ⊕ cellauto) with
    replacement PNGs taken from the pre-encode source; swapped replacements fail identity.
  - Still not ported: DVD-free sync (needs the DVD-free audio timing stage that writes the report it
    verifies). Absolute A/V and between-track are unit/synthetic-tested only — no fstk stage yet
    produces `align_npy`/retimed 5.1, so they run end-to-end only on imported v38 episodes.
- **S2-CP7 — v38 commit, real-Orc evidence, DVD-free audio (owner session, 2026-11-21 evening).**
  - **v38 commit** (owner asked): `035ecc6` on branch `fromstuntokill-v15-app` (worktree of
    `Z:\ld-pipeline`), exactly the four findings-0001/0002 files; the rest of the owner's uncommitted
    V38 work was left untouched; `WORKFLOW_VERSION` not changed. Committed from the Linux VM with
    `core.autocrlf=true` (matches the index: LF blobs) and `core.createObject=rename`; the two lock
    files git could not unlink were removed after the owner granted deletion in `Z:\ld-pipeline`.
    v38 suite (non-GUI, container): 508 passed vs 505 on the same tree without the patch; the same 7
    environment failures (missing `dependencies/`, tkinter) in both.
  - **Real Orc evidence** (`tools/validate_v38_efm_evidence.py`, reads a v38 device episode folder;
    nothing copied into the tree): S01E20, Orc 2.11.1, 12 captures — rebuilt project byte-identical,
    log path binding passes, all six metrics equal v38's; unique-Pareto says "operator must choose"
    (the owner chose P2). Mac logs from S01E11/S01E13 (the quarantined cross-episode S01E10 evidence)
    parse fully and are **rejected** by fstk's log binding when presented as S01E11/S01E13 evidence.
    **Finding 0003 withdrawn**: real reports print both labels with the same value. Parser now prefers
    Part A's `Sections missing` (leading integer). A fresh Orc run could not be done from here: the
    capture share (X:/W:/`\\LENOVOFLEX\e hdd`) and `Z:\ld-pipeline-device` could not be granted to the
    session, and the device shell is a Linux VM (Windows `orc-cli.exe` cannot run there).
  - **`dvd-free-audio` stage** (`stages/dvdfree.py`, v38 `audio_timing_diagnostic --promote-dvd-free`):
    requires the recorded no-DVD choice and LD-only tracks; duration rate, ±1-frame PAD endpoint gate
    (blocks before writing), capture-pad AudioSkip, rate-corrected + exactly-sample-budgeted synced FLACs
    (new names per run), timing report; records the audio.* recipe, `deliverable.*` = LD only, artifacts
    `ld_corrected`/`ld_synced`/`audio_timing_report`.
  - **DVD-free QC gate** (`qc/dvdfree.py`, v38 `ld_only_sync_check`): wired into `qc-report`. With it,
    every QC gate is ported. New end-to-end test: no-DVD episode → dvd-free-audio → x265/FLAC deliverable
    → QC all PASS → awaiting-review → approval → `qc.*` committed.
  - Full review for the owner (built / pitfalls + questions / undefined / suggestions):
    claude.ai doc "FromStunToKill refactor review" (https://claude.ai/artifact/Ci9BWHzrisYntogaxoK4b2).
    Open question raised there worth checking first: EFMSink reports list pipeline warm-up/drain samples
    "carrying no disc data"; if they are in the WAV, the no-DVD duration model is off by ~1.3–2.5 frames.
- **S2-CP8 — shared multi-machine workspace (ADR 0002, owner asked while reviewing).**
  - Studied v38 `shared_workspace.py`: per-device folders `devices/<DEVICE>-<8hex>/` in the release
    tree, machine-local identity, "write only your own folder", explicit no-overwrite publications;
    no way to move an episode between machines.
  - fstk keeps state local and publishes to `profile.shared_root` (never the v38 release tree — refused;
    an existing device folder without an fstk marker is never written). `workspace/device.py`
    (identity beside the profile, ownership marker), `workspace/portable.py` (path tokens
    `{data_root}` `{media_root}` `{root:x}` `{source:x}` `{volume:x}`; new profile field `volumes`
    for disks several machines mount, e.g. the Flex's `e hdd`), `workspace/shared.py` (content-addressed
    snapshots + `status.json`, presence, handoff with media inventory, adopt with hash-verified
    snapshot + fresh journal + source journal kept + media check, fork, reclaim).
  - Engine: refuses to run a handed-off workspace; publishes after every run (best effort, event
    `shared-publish`). Episode creation refuses an episode another device has active unless
    `--parallel`. An unreachable share never blocks local work.
  - Approvals travel and stay valid: reports/references travel byte for byte, approval evidence paths
    are re-pointed; manifest/journal/approval paths are tokenised.
  - CLI `fstk share device|status|publish|handoff|adopt|reclaim`; doctor shows the share read-only;
    picker "Other devices" column + "Adopt from other device"; main window "Hand off…" and a
    read-only banner when handed off. Screenshot `docs/screenshots/picker-shared.png`. README section.
  - Tests: two (and three) simulated machines sharing one folder incl. a volume mounted at different
    paths, tampered snapshot, existing workspace, media missing/different, v38 tree guard, CLI, GUI.
  - Not done: importing v38 `devices/` workspaces (the in-flight import task); real two-machine run
    over SMB (Windows ↔ macOS) is untested.

---

## 2026-11-21 — Session 1 summary (read this first)

**State at hand-off:** 191 tests pass, ruff clean. Developed in a Linux container (Python 3.13,
ffmpeg with libx265; no vspipe/Orc/Windows), synced to `Z:\FromStunToKill` with checksum
verification. v38 untouched. No git repository was initialised and nothing was committed.

**Built:** core (identity, fingerprints compatible with v38, atomic IO, locks, redaction) · typed
machine profile + v38 converter · declared manifest registry, journaled store, lossless v38
import/export (all 19 real manifests) · process runner · ffmpeg/Orc/VapourSynth adapters ·
space gate · stage framework · approvals bound to evidence · workflow engine (DAG, resources,
verify-before-commit, deferred commit for reviewed stages, fingerprint-verified resume) ·
stages `dvd-probe`, `stack-reference`, `stack-render`, `cadence-evidence`, `encode-final`, `remux` ·
ported math (cadence, cadence repair map, fade fit, audio timing, retime candidates, PAD audit) ·
CLI · PySide6 shell.

**Needs the owner:**
1. `docs/findings/0001` — v38 audio-rate intercepts carry a drift-proportional origin bias
   (≈ +26 ms at 864 ppm) from window-start referencing; decide the convention.
2. `docs/findings/0002` — v38 Windows AVS `Trim(start, -tail)` keeps only *tail* frames; a
   one-line v38 fix is suggested if you keep that line.
3. Run `cadence-evidence` on Windows VapourSynth against a v38 AviSynth TFM log (Phase 4 exit).
4. `git init` + first commit of `Z:\FromStunToKill` when you are happy with the layout
   (`.gitignore` already excludes `.fstk-local/`, device data and media).
5. Housekeeping: `.fstk-local/` holds the v38 snapshot tar and the last sync tar (safe to delete).

---

## 2026-11-21 — Session 1 (Claude, unattended overnight build)

**Goal:** begin the clean-sheet refactor per `docs/REFACTOR-PLAN.md`; build as much as possible
without owner input. v38 treated as read-only reference.

### Setup
- v38 snapshot (code, tests, docs, small device data; no media, no `.git`) taken to
  `.fstk-local/v38src.tar` (gitignored) and unpacked in the dev container for reference.
- v38 suite baseline in the container: **512 passed, 11 skipped**; the 13 Tkinter test modules
  cannot be collected without tkinter.

### Done
- **Phase 0 scaffold:** `pyproject.toml` (src layout, `fstk` + `fstk-gui` entry points,
  extras `gui`/`dev`), ruff + pytest config, `.gitignore` (no device data/media in tree),
  `AGENTS.md`, this log, `tools/v38_inventory.py` → `docs/v38-inventory.md` (all 121 v38
  Python/PowerShell files mapped; `--check` mode fails on unmapped/stale entries).
- **core/**: `errors`, `identity` (series registry DS9/VOY/TNG; DS9 keeps v38 un-namespaced
  paths), `hashing` (full + head/tail fingerprints, **byte-compatible with v38
  `local_source.fingerprint_file`** so imported capture fingerprints can be re-verified),
  `fsio` (atomic write with Windows sharing-violation retry from v38 `manifest_io`, lost-update
  guard, exclusive create, canonical JSON, BOM/UTF-16 aware decoding, owner-reporting lock
  files that are never broken automatically), `timeutil`, `redaction`.
- **config/profile.py**: typed machine profile `fstk-machine-profile-v1`; per-episode layout
  (`data_root/episodes/<SERIES>/<CODE>/{manifest,logs,plans,reports,evidence,...}` and
  `media_root/<slug>/{stack,master,audio,subs,out,probe,scripts,...}`) with per-root volume
  overrides; v38 root names kept (`avs` → `scripts`); v38 `machine.json` converter (drops
  AviSynth keys, maps `darwin`, explains every change).
- **manifest/**: declared field registry (every v38 field with type/unit/kind/minimum;
  `None` and empty strings are never values; tracks rule: `ld` present and first), `Manifest`
  model with per-value provenance (actor, stage, evidence fingerprints, origin), artifact
  specs that cannot escape their root, journaled `ManifestStore` (lock → verify base →
  atomic write → append `journal.jsonl` with old/new per field; `expect_sha256` refuses
  decisions made against a stale manifest; `verify()` detects edits outside the store),
  lossless v38 import/export.
- **Tests:** 73 passing (unit + characterization). All 19 real v38 manifests (S01E12, S01E18,
  S01E20 and their 16 `.bak` copies, plus the template) round-trip; fixtures are redacted.

### Decisions
- Manifest = declared values with provenance + journal instead of free-form sections + `.bak`
  files. Reason: every measurement must be traceable to evidence so approvals can be
  invalidated when inputs change, and history must not depend on snapshot sprawl.
- v38 null/""/[]/{} placeholders import as **unset**. Equivalence for round trips is defined
  modulo those placeholders.
- Profile stays JSON (stdlib read/write); TOML was considered but needs a writer dependency.

### Progress checkpoints
- **CP1** — Phase 0/1 done (73 tests). Owner asked mid-session that the development flow be
  logged in the project for handoff → added `AGENTS.md` + this log; synced to `Z:\FromStunToKill`
  via `.fstk-local/fstk-sync.tar` extracted with `--overwrite` (nothing on the device is deleted).
- **CP2** — Phase 2 foundations (114 tests):
  - `tools/process.py`: single subprocess runner (own process group; \n and \r line splitting
    for ffmpeg progress; redacted logs; timeout/cancel stop the whole tree; `vspipe | ffmpeg`
    pipelines with per-stage return codes). Replaces v38 `_run_child`, `streamutil`,
    `Invoke-LdFfmpeg`.
  - `tools/ffmpeg.py`: probe, decode-counted frames, full null decode with error capture,
    **packet scan + seek refusal for non-monotonic sources** (the DVD `-ss` trap is now enforced
    by code), `frame_selection_chunks` (v38 tests ported verbatim, 6 pass).
  - `tools/orc.py`: v38 refresh-report parsing, Orc reference decision, render-evidence
    validation ported verbatim via AST extraction; v38 tests ported (9 pass); thin `Orc` wrapper
    (`refresh` never renders; `process_project`).
  - `workspace/space.py`: space gate + cross-process reservation ledger (v38 logic) with a
    context-manager reservation.
- Device VM note: the device-side Linux VM has Python 3.10 only, so tests run in the dev
  container, not on the device. Real Windows runs are still owed.

- **CP3** — architecture core (135 tests):
  - `stages/base.py`: `Stage` with declared `requires` (ValueSet, ValueIs, ArtifactExists,
    CapturesAtLeast, ApprovalCurrent, Custom), `writes`, `writes_artifacts`, `resource`,
    `review`; `plan → run → verify`; stages *propose* measurements, never write the manifest.
  - `evidence/approvals.py`: immutable APPROVED/REJECTED records with required note, bound to
    evidence fingerprints + manifest values; `status()` → approved/rejected/stale/missing/historical.
  - `workflow/engine.py`: DAG validation (cycles, unknown deps/stages), per-resource
    concurrency, space reservation per step, **commit only after verify passes** (one
    journaled transaction with input/output fingerprints as evidence), review gating of
    dependents, fingerprint-verified resume with downstream invalidation, refusal to reuse a job
    id for a different plan, durable `state.json`, event stream for UI/CLI.
  - First real stage `dvd-probe` (streams, packet order → seek policy, decode-counted frames,
    SHA-256) with an end-to-end engine test on synthetic MPEG-2/AC-3.
  - `cli/main.py`: `fstk profile init|import|show`, `fstk manifest import|show|verify|history`,
    `fstk stages`, `fstk docs stages` (→ `docs/stage-reference.md`), `fstk run`, `fstk status`,
    `fstk doctor` (new cross-platform read-only doctor in `tools/doctor.py`).
  - Verified by hand: real S01E20 manifest imports through the CLI (18 values, 58 measurements
    correctly unset).

- **CP4** — media algorithms, first batch (153 tests):
  - `tools/port_v38.py`: mechanical, provenance-stamped porter for pure modules and their
    unittest methods (keeps ported logic verbatim; only lint-safe fixes, e.g. `zip(strict=True)`).
  - `media/cadence.py` (v38 `cadence_analysis.py`, 3 v38 tests), `media/fade_fit.py` (fit and
    pairing verbatim; detectors split into pure log parsers; caught and fixed my own
    `blackdetect d=` mismatch against v38 with a test), `media/audio_timing.py` (duration/PAD
    model, resampler chains, envelope correlation, Theil–Sen, sample plan; all 8 v38 tests).
  - **Finding 0001** (`docs/findings/0001-audio-window-reference-bias.md`): an end-to-end
    synthetic validation of the audio-rate method showed that v38's window-*start* referencing
    biases the fitted A/V origin by ≈ drift × 25 s (+26 ms at E07-like 864 ppm); window-centre
    referencing removes it. Slope unaffected. Not applied to ported code — needs owner decision;
    relevant to v38's blocked "origin convention".

- **CP5** — more ports + UI shell (170 tests):
  - `media/retime.py` (v38 `retime_break_candidates.py` minus its manifest-marker check, which
    approvals replace; 10 v38 tests), `media/pads.py` + `tools/orc_project.py` (v38 `pad_audit`
    pure parts; 4 v38 tests + parser tests on the real S01E18 `.orcprj`).
  - `ui/services.py` (UI-agnostic API: episodes, manifest rows with provenance, jobs, pending
    reviews, `decide()` bound to the exact evidence listed, `run_plan`) and `ui/app.py`
    (PySide6 shell: episode list, Manifest/Jobs/Reviews tabs, plan runs on a worker QThread with
    live engine events and cancel, approve/reject dialogs requiring a note). Screenshot:
    `docs/screenshots/app-shell.png`. pytest-qt smoke test runs offscreen.
  - Engine now writes `plans/<job>/plan.json` and records each step's stage id in state.
  - Sync procedure hardened after a stale-read incident (the device received the previous
    tarball once): every sync is now checksum-verified on the device before extraction.

- **CP6** — Orc stack stages (178 tests):
  - `tools/orc_project.build_stack_project` (v38 builder made pure; template shipped as package
    data; 2/6/12-capture generation tested and parsed back).
  - `stages/stack.py`: `stack-reference` (refresh loop ≤3 passes, Orc `suggestedReference` only,
    refuses to silently replace a recorded reference or frame count; records reference,
    alignment map, `stack.frames` = coverage sum, fit stats — v38 had to re-parse its own
    Markdown report for these) and `stack-render` (requires an explicit space estimate + basis,
    refuses to overwrite a master, validates Orc evidence against an independent decode count,
    human review gate `stack-render`). Tested end to end through the engine with a fake Orc that
    exits non-zero on success, an unstable-recommendation case, and a frame-count mismatch.
  - Sync: a second stale delivery was caught by the checksum guard; each sync now stages a
    uniquely named tarball.

- **CP7** — cadence map (182 tests):
  - `media/cadence_map.py`: v38 `build_map.ps1` repair-set rules ported to Python (they were the
    Windows measurement authority), cross-checked against `media/cadence.analyze_tfm_rows` on a
    synthetic log with an interlaced insert, the 65–80 mic band and an unmatched frame;
    `ivtc_vpy_script` = v38 `build_map_vpy.render` with an optional in-script assertion against a
    *measured* frame count. I deliberately did **not** add a TDecimate frame-count formula
    (partial-cycle behaviour unverified → would be an invented convention).
  - **Finding 0002** (`docs/findings/0002-avisynth-negative-trim.md`): v38's Windows AVS line
    `Trim(start, -tail)` returns *tail* frames (AviSynth negative = frame count, confirmed in the
    AviSynth+ docs), so the Windows path cannot complete an episode with `tail_trim > 0` (QC gates
    would catch it). Includes a one-line fix suggestion for the owner's v38 line.

- **CP8** — cadence stage (187 tests):
  - Engine: values proposed by a stage that declares a `review` are now held in job state and
    committed only when the approval is current, with the approval record attached as evidence
    (v38 parity: cadence/trim metrics were recorded only after visual approval).
  - `tools/vapoursynth.py` (vspipe→FFmpeg full decode via the shared runner; macOS DYLD env;
    v38 TFM-log script) and `stages/cadence.py` `cadence-evidence`: full-decode count must equal
    `stack.frames − audio.pad_frames`, TFM log must have exactly that many contiguous rows, both
    repair-set implementations must agree; stale TFM logs are moved aside (TFM appends), never
    deleted; the stage creates the probe folder TFM itself would not. Review `cadence-review`.

- **CP9** — final encode (189 tests):
  - `media/encode.py` (v38 x265 recipe verbatim, `-aspect` DAR from SAR, encoded-stream checks;
    expected raster now taken from the stack master instead of v38's hard-coded 768×484) and
    `stages/encode.py` `encode-final`: script frame count must equal `video.frames`; encode to
    `.tmp.mkv`, full verification (profile, pix_fmt, rate, raster, decode-counted frames, clean
    full decode, duration ±2 ms) **before** moving into place; never overwrites; `encode` review.
    Integration test runs the real libx265 recipe on a synthetic y4m and passes every check.
  - `tools/vapoursynth.py`: `info()` (`vspipe --info` parser) and `encode()` (vspipe→FFmpeg).

- **CP10** — remux (191 tests): `media/remux.py` + `stages/remux.py` (v38 recipe: stream copy,
  LD stereo default, 5.1/SRT non-default from their own retimed files, `language=eng`,
  `-max_interleave_delta 0`, packet-MD5 identity per copied stream, write to `.tmp` and move
  only on pass; requires the `encode` approval; `remux` review). Real-media test with
  H.264 + FLAC + AC-3 5.1 + SRT, plus a test proving an over-long audio track (cut by `-t`) is
  caught by the identity gate.

### Next steps
(updated at each checkpoint)
0. Cadence follow-ups: side-by-side raw/TFM review GIFs (v38 `build_cadence_review_assets.py`),
   motion trace stage (v38 `build_motion_trace.py`), IVTC encode-script stage using
   `ivtc_vpy_script` with `expected_frames = video.frames`. **Owner/Windows:** run
   `cadence-evidence` on a real episode on Windows VapourSynth and compare against v38's AviSynth
   TFM log (period-5 %, unmatched, combed spans, frame count) — REFACTOR-PLAN Phase 4 exit.
1. EFM decode stage (v38 `efm_decode.py`, `decode_efm_captures.py`, `verify_efm_capture.py`) and
   capture selection as a review.
2. VapourSynth cadence/map stage (v38 `build_map_vpy.py`, `build_motion_trace.py`) — this is
   where the Windows VapourSynth re-validation (REFACTOR-PLAN §7.3) happens.
3. Encode/remux/QC stages (v38 `encode_episode_mac.py`, `remux_padfixed_mac.py`, `qc_report.py`).
4. Episode intake stages (local source inventory, Archive download in Python).
2. Wrap them as stages (intake/local-source, stack reference/render via Orc, EFM decode,
   cadence map on VapourSynth, encode/remux, QC).
3. PySide6 shell over the engine's event stream.

## CP16 — 2026-11-22 S01E20 smoke run (unattended)
Full no-DVD chain passed on a 100 s real excerpt (4:2:2 10-bit HEVC out). Fixed: PAD payload fps made output variable-rate
(real bug); QTempGaussMC option spelling is version dependent; smoke auto-approves review gates (labelled) and gained
`--repair pp2` / `--rife-standin`. Missing on the device VM: `resize2` plugin, RIFE. See `docs/smoke/SMOKE-2026-11-22-S01E20.md`.

## CP17 — 2026-11-22 tool requirements: ranges + probes, not exact pins
`fstk.tools.requirements`: minimum (FAIL below), tested_up_to (WARN above), capability probes (ffmpeg libx265 + yuv422p10le,
VapourSynth plugins incl. `resize2`, QTempGaussMC API), pinned drift for Orc / RIFE model / x265. Every engine run writes
`reports/tool-versions.json` (history extends only on change) and emits `tool-version-drift`. Shown in `fstk doctor`.
No binaries are vendored (size, GPL redistribution for x265 builds, per-OS builds); a fetch/lock step is still open.

## CP18 — 2026-11-22 quality layer (video-quality, audio-quality, episode-report)
Owner list implemented under the rule *grain is paramount, DVD is never a pixel source (only cut-PAD frames, crop/aspect locking)*.
- `video-quality` (after pad-repair; encode follows): levels vs DVD (black/white/mid) + clipping, active-area crop and SAR/DAR lock,
  chroma-noise / cross-colour metrics with **metric-gated chroma-only denoise** (luma bit-identical, proved), dropout map with
  concealment **only from LD neighbour frames** (real pixel patches, integer even MVs, accepted only if ring match ≤ grain-expected
  cost and patch/ring grain ratio in (0.7, 1.4)), hybrid-cadence plan (advisory; timecodes file when video-origin stretches exist).
  Output: `finish_script` (stale-guarded) which `encode-final` prefers; `quality.dar` becomes the encode `-aspect`.
- `audio-quality` (before remux): click/gap detection per channel, **short** defects only filled by least-squares LPC (order 32),
  longer/ambiguous reported; bit-identical elsewhere (`ld_audio_finished` FLAC only if repaired); channel/phase/LFE/swap checks,
  R128 loudness + true peak (reported, never normalised), DVD 5.1 lossless-path record, subtitle timing vs speech envelope.
- `episode-report` (after qc-report): one JSON/markdown report (verdict CLEAN / REVIEW / FAIL), written next to the deliverable
  (`<name>.quality.md`) and to `reports/`, plus a contact sheet of the worst moments (leftover dropouts, QC intervals, concealed
  dropouts, audio defects left). Shown in the delivery review dialog as the "Episode quality report" tab.
- Not done: hybrid *delivery* (59.94p/29.97p segments + Matroska timecodes) — needs mkvmerge and Windows validation; QC gates assume CFR.
  Encoded-stream colour-tag check (`geometry.check_colour_tags` on the delivered file) is not yet wired into QC.

## CP19 — 2026-11-22 lossless stack master
- The Orc stack master was ProRes HQ (`mov-prores`). It is not a QA-only copy: cadence, trim, PAD repair, quality and
  the final encode all read it, so it was a lossy generation ahead of the final x265 encode. The generated Orc project
  now forces `ffmpeg_format: mkv-ffv1` (decode-orc's lossless FFV1/Matroska sink; an old ProRes template is corrected
  and its `prores_profile` dropped), the master is `<code>-stack-master.mkv`, and `stack-render` fails when the
  rendered master's codec is not FFV1. `episode-report` flags an episode whose master is still lossy (re-render).
- Not verified here: the installed Orc build's acceptance of `mkv-ffv1` (Windows only); the render check fails closed.

## CP20 — 2026-11-22 video-origin stretches at constant 23.976p, operator frame fixes, DVD as opt-in
Owner decisions: one constant output rate (the 23.976 film rate); video-origin stretches use the nearest real
frames by default with interpolation as a per-stretch choice; frame fixes chosen from the contact sheet; the DVD
only as a reviewed, opt-in source with side-by-side and in-context previews.
- `fstk.media.hybrid` rewritten for CFR (no VFR/timecodes, no mkvmerge). `cadence-evidence` classifies cycles,
  runs a field-difference pass on candidate stretches and keeps film plates under video overlays as film
  (telecine field repeats measured on quarter-size fields so grain/noise does not swamp motion; checked on the
  S01E20 master, a synthetic plate+overlay, telecined film and true 60i). `hybrid_plan` artifact.
- IVTC script: after TDecimate, video cycles are replaced by bob frames 0,2,5,7 of every 10 (nearest, real
  pixels; QTGMC bob when QTGMC repair is used, znedi3 otherwise), source frames 0,1,2,4 of every 5 for native
  29.97p, or MVTools FlowFPS frames (interpolate). Always 4 per cycle; verified on real VapourSynth (exact frame
  identity, film cycles untouched, probe tags). `video.hybrid_choices` per stretch; `stretch-preview` renders
  side-by-side clips; GUI tab "Video-origin stretches" records the choice and plans a fresh encode.
- `frame-fixes` (after pad-repair; chain = encode script → PAD splice → operator fixes → finishing):
  interpolate (1–3 frames, refused across a cut), ld-patch (region from the neighbouring clean LD frame), dvd
  (whole frame or region; PAD-borrow registration + colour match), leave. Grain of rebuilt frames is measured
  and flagged. `fix-preview` renders captured/fixed strips (DVD conformed beside LD) and, up to 2 s, in-context
  clips. GUI tab "Frame fixes". Verified on real VapourSynth (untouched frames identical, region bounds exact).
- `encode-final` numbers re-encodes (`-r2`, …) instead of refusing; nothing is overwritten.
- Bug fixed: the IVTC tail trim kept one frame too many (`std.Trim` last is inclusive). video-trim's count check
  refused it, so nothing wrong was encoded; every v38 episode had tail_trim 0.
- Not built: re-stacking a range without the damaged capture (needs Orc on Windows); RIFE for frame fixes and
  stretches (MVTools is used; RIFE remains the PAD engine).

## CP21 — 2026-11-22 ready for the first real Windows run
- `docs/WINDOWS-RUN.md`: the owner's runbook for DS9:S01E20 tonight (setup → smoke → create → intake → delivery
  → review, and what to send back per failure). `tools/windows/setup-fstk.ps1` + `make_profile.py`: Python 3.12
  venv outside Z:, `pip install -e .[gui,media]`, profile from the v38 machine.json with the venv's vspipe, doctor.
- `media` extra: VapourSynth R82 and every plugin from PyPI wheels (all exist for win_amd64, checked, resize2
  included).
- `stack-render` always renders the lossless master to `<code>-stack-master.mkv` from a copy of the stack project
  with the sink forced to FFV1; an imported v38 ProRes master is left untouched; an existing lossless master with
  the right frame count is reused instead of re-rendered. Master estimate 500 KB/frame (FFV1 measured 415 KB on
  S01E20; ProRes was 287).
- `fstk episode dvd <ep> --find | --file | --none`: a fresh episode can now record its DVD (there was no way).
- Archive downloader: a complete file unchanged since a matching *verified* download-manifest row is accepted
  without re-hashing (a 12-capture item is ~1 TB of MD5); changed files are still hashed.
- `fstk smoke` takes tool paths from the machine profile first (RIFE/Orc are not on PATH on Windows).
- Parked: the residual-damage map + masked RIFE fill. Real S02E01 TBC JSON: dropouts in 3641 of 166048 fields per
  capture, median 17 samples; mapping capture fields to stack frames needs Orc's frame-map + alignment semantics,
  to be confirmed on a real render first.

## CP22 — working folders off X:, setup script Python lookup
- fstk writes only to local, owner-chosen folders: default `~/FromStunToKill/{data,work}` (`FSTK_HOME` overrides),
  per-kind override (`roots.out` for finished files). `fstk profile folders`, app **Folders…**, `setup-fstk.ps1
  -DataRoot/-MediaRoot/-OutRoot`. `change_folders` refuses to move a root that already holds episode work (force overrides).
- `fstk smoke` sandbox defaults to `~/FromStunToKill/smoke`. X: and Z:\Video are read-only inputs.
- setup-fstk.ps1: first Windows run failed (`py` not on PATH right after the winget install); now locates a real
  CPython 3.12 by full path (py launcher, then the standard install folders, PATH refreshed), and uses it for the venv.
- 414 tests, ruff clean.

## CP23 — From local folder… reaches shares
- Picker's "From local folder…" is now a dialog: type/paste any path (mapped drive, `\\server\share`), Browse, or start from
  `sources.downloads_root`; a folder of episodes lists the capture folders (≤2 levels) under it to choose from.
  `find_capture_folders` in `workspace/local_source.py`. Validation itself never blocked network paths.

## CP24 — progress, pause/resume
- `workflow/progress.py`: `ProgressTracker` (per-step %, weighted overall %, ETA only with a basis: the step's own rate,
  recorded history, or speed measured on this run), `Timings` (median of the last 9 durations per stage, `<data>/timings.json`),
  `describe_job_state`. Weights default to rough per-stage seconds until real timings exist.
- GUI: **Progress** tab (overall bar, per-step checklist with bars/time/message, indeterminate "working…" where no honest %
  exists), window title "% — step", Windows taskbar progress (ctypes ITaskbarList3; yellow paused, red failed),
  desktop notifications (tray) for review-needed / failed / complete (toggle), saved-state status line so progress survives
  closing the app.
- **Pause/Resume**: `tools/process.PAUSE` suspends running tool trees (psutil: SIGSTOP / NtSuspendProcess) and Python stages
  wait at `ctx.report`/`check_cancel`; the engine starts no new step while paused; paused time is excluded from elapsed/ETA;
  cancel while paused releases and stops. `psutil` is now a dependency.
- Bug fixed: `PlanWorker.engine` was only set after the run finished, so Cancel never reached a running plan. `run_plan(on_engine=)`.
- Real fractions: `tools/progress_lines.LineProgress` parses vspipe `Frame: n/total` (vspipe now run with `-p`; format confirmed on
  the device VM), ffmpeg `frame=`/`time=`/`out_time_us` and generic `NN%`; wired into encode, encode verification,
  cadence TFM + film-plate, QC decode, stack render and EFM decode (Orc's own output format is unconfirmed: best effort, otherwise
  the bar stays indeterminate).

## CP25 — Remove episode
- Left of the episode list: **Remove…** → dialog (None = project only / Just the project files / Everything), then an
  "Are you sure?" box (Remove / Cancel, Cancel default). `workspace/removal.py`: `plan_removal`, `remove_episode`.
  Project record goes last; every path is checked to sit inside its base (data_root, media_root or the output override,
  downloads_root) before anything is deleted. Captures chosen with "From local folder…" and the DVD are never deleted
  (Everything only removes a download fstk made in downloads_root). Refused while a plan runs or the episode is handed off.

## CP26 — disk-space Refresh
- Progress table: a Refresh icon (between Time and "What it is doing") on every step blocked by the disk-space check.
  One click re-measures free space for ALL such steps (`Engine.recheck_space`): those that now fit go back to pending (a running
  engine launches them straight away; otherwise the next run does); still-short ones get the new numbers in their message.
  With no run active it edits the saved job state under the job lock (`recheck_space_offline`). Steps blocked for other reasons
  are untouched. Events `step-unblocked` / `space-rechecked`.
- CP26 follow-up (first real use): refresh icons showed on every row (Qt force-shows direct cell widgets: now inside a
  container); "lock held" when the job is running in another window: friendly message, saved state followed live
  (`job_runner`, 3 s watch, running steps shown as running instead of interrupted).

## CP27 — logging and debug bundle
- `core/applog.py`: rotating `fstk.log` (5×5 MB) under `<home>\logs` — GUI actions, every engine event (progress every 30 s),
  every tool command + exit, warnings/errors, uncaught exceptions (main and threads) with tracebacks, Qt warnings;
  `fstk-crash.log` via faulthandler for hard crashes. Started by the GUI and the real CLI (not by tests).
- `diagnostics.py` / `fstk bundle` / GUI **Save debug bundle…** + **Open logs**: zip of logs, environment (doctor, versions,
  free space, admin?), profile, ledger, timings, and per episode manifest, plans, job state, tool logs (tail-capped);
  home folder → `~`, user name → `<user>`; no media.

## CP28 — restart-safe progress
- Closing the window mid-run asks, then stops the run cleanly (tools ended, state saved) instead of orphaning it.
- `efm-decode`: a stopped run is no longer recorded as a failed capture; the unfinished capture's partial files are removed (markers also let the next start sweep leftovers from a hard kill); captures already verified are saved to `reports/efm-decode-progress.json` and adopted by the next run, so only the rest is decoded.
- `stack-reference`: each finished Orc refresh pass is saved in scratch (keyed by project text + capture sizes/times) and reused after a restart.
- Still restarts from the beginning of the step: encode, cadence, QC and other single long processes.

## CP29 — Settings window, output settings (defaults + per episode)
- `fstk.config.output`: six software encoders (x265 10/12-bit, x264 10/8-bit, SVT-AV1 10-bit, FFV1). x265 always
  `-tune grain`; colour range always limited; average-bitrate mode = two passes (x26x only). Audio: LD stereo
  (FLAC default) · DVD 5.1 when present (FLAC default) · lossy compatibility stereo (AAC 256 default, owner: on).
- Defaults in the profile (`output`), per-episode groups in the manifest (`settings.output`, whole-group override).
- `encode-final` uses the effective settings, records `deliverable.video_encoding` (QC checks codec/profile/pix_fmt
  against it). `remux` encodes non-FLAC tracks to their own files first, stream-copies them (identity proof kept),
  records `deliverable.audio_layout`; LD stays a:0 and DVD 5.1 a:1, compatibility last. QC follows both records;
  older deliverables keep the original x265/FLAC expectations.
- UI: Settings… (General, Folders, Video, Audio, Output, Processing, Tools, Logging) with a scope switch; saving
  episode settings for an encoded episode offers a re-encode plan (encode → remux → QC → report).
- App prefs in the profile (`ui`): operator name, notifications, catalog interval, log level, tool priority
  (below normal by default).
- Known: a lossy codec on the *main* LD track adds encoder priming delay (AAC ≈ 21 ms); the A/V gates would catch
  it. Container stays MKV (MP4 not offered). Queue system is next.

## CP30 — Episode queue
- `engine.HEAVY`: one heavy (compute/GPU) job at a time across every run in the app; a run holding it may run
  its own heavy steps; waiters are served by queue position; `step-waiting` event names the holder. Downloads
  and prep never wait. A paused engine with nothing running now waits instead of ending its run.
- `workflow/queue.py`: `QueueStore` (`<data>/queue.json`; running items return to queued on load) and
  `QueueRunner` (2 items at once, one per episode, later items of an episode held until earlier ones are done;
  review/blocked/failed are flagged and skipped; Retry resumes the same job; plans written at start).
- Queue tab: Add to queue (intake / delivery / both / re-encode / several episodes), reorder, retry, stop item,
  remove, clear finished, sleep when done (1-minute grace, cancellable). Closing the app shuts the queue down and
  it resumes on the next start.

## CP31 — deep-review fixes, volume advice, sync-gate uncertainty, override flag, tooltips

Source: `DEEP-REVIEW-1` (project doc). Defects A1–A5 reproduced, then fixed with regression tests in
`tests/unit/test_review_fixes.py`:

- Damaged JSON (profile, queue, caches, progress files) now raises `JsonFileError` (an `FstkError` that is also a
  `ValueError`) naming the file, instead of crashing a window. Queue and profile keep a rolling `.bak`; a damaged queue
  is moved aside as `*.bad-<stamp>` and the app starts clean.
- Queue: three-way merge on save so two windows cannot overwrite each other; single queue runner per machine via
  `queue-runner.lock`; removing an episode is refused while a job runs or the queue holds it (and the queue drops it
  after removal). `_resume_queue` was missing in `ui/app.py` (a saved running queue would have crashed start-up).
- pid liveness uses process start time (`ptime`) so a recycled pid is not mistaken for the lock owner.
- Space block messages end with a concrete suggestion (`workspace/volumes.py`: measured needs from v38; network and
  removable drives are never suggested). The Settings Folders page shows the same advice; the main window warns once at
  start-up when a folder sits on a drive that cannot hold the work.
- Between-track sync gate: limits unchanged (|slope| ≤ 30 ppm, rms ≤ 40 ms). New `slope_verdict` uses the fit's own
  standard error (2σ, 6 ppm margin): pass / marginal / fail / ambiguous. A marginal or ambiguous result is re-measured
  automatically with more windows (`more_sync_points`); the limit is never relaxed. Note for the review's wording:
  E09's +34.7 ppm *failed* v38's gate and was fixed by rate refinement; 4.7–38.2 ppm is the feasible window, not an
  accepted slope.
- Episodes with their own output settings show a flag in the main window (what differs, who, when, and whether the
  defaults changed since). Every interactive control now has a hover tooltip (`ui/tips.py`); `tests/unit/test_tooltips.py`
  fails when a new control is added without one.
