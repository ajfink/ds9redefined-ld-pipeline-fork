# Project re-audit 1 — against the goals and the highest-quality-output approach

Date: 2026-11-21 · scope: whole `fstk` tree at commit `0fca7e9` (349 tests, ruff clean) · method: code
reading of the delivery path (intake → stack → cadence → trim → audio → PAD → encode → remux → QC → A/V
check) against the owner's goals: *quality first, CPU time irrelevant, maximum automation, one human
review (A/V), done = LD video + aligned LD stereo + aligned DVD 5.1 + DVD subs*.

Nothing here has been run on real Windows VapourSynth / RIFE / Orc output by me; the container has none.
"Verified" below means covered by an automated test on synthetic or small real media.

## 1. Verdict

The pipeline is **structurally complete for both routes** and every stage fails closed. The remaining
risk is not missing features but **unvalidated seams with real material**, and a handful of places where
a quality-first choice is still inherited from v38 rather than measured. Ranked below.

## 2. Findings (ranked by effect on output quality or on delivery reliability)

### A. Quality of the picture

1. **Chroma downsampling is left to swscale.** `encode-final` pipes the chain's output (the master's
   subsampling, kept by the script) into FFmpeg with `-pix_fmt yuv420p10le`. If the stack master is 4:2:2
   (ProRes), swscale does the 4:2:2→4:2:0 step with its default kernel and no chroma-siting
   information. Doing it in the VapourSynth chain with zimg (`resize.Spline36`/`Bicubic`, `chromaloc`
   = left for BT.601 SD, dither error-diffusion off because the source is already 10-bit) is
   deterministic, higher quality, and testable. **Recommend: move the conversion into the script, make
   the encode verify `pix_fmt` already equals the target.** (Confirm the master's actual format first:
   `ffprobe` of one real master; I could not.)
2. **PAD replacements are 8-bit PNGs.** The delivered chain is 10-bit; RIFE and the DVD borrow both go
   through 8-bit RGB (as v38 did). One frame per PAD, so low visibility, but it is free to fix: write
   16-bit PNG/RGB48 for the RIFE inputs and the payload clip (`ffv1 rgb48le`). **Recommend: do it.**
3. **DVD borrow is the least-validated algorithm in the tree.** The geometry/colour estimate is mine
   (ECC affine + robust gain/offset), not v38's. Safeguards exist (unique flank match exactly two DVD
   frames apart, post-fit NCC ≥ 0.90, fallback to hold with the reason recorded), but the *quality* of an
   accepted borrow is unmeasured on real material. **Recommend an automatic self-test** (see §3, E1):
   for each real cut in the episode that is *not* a PAD, hide the frame, borrow it from the DVD, and
   measure the error against the real LD frame. That gives a per-episode accuracy number and a data-driven
   NCC gate instead of my guessed 0.90.
4. **Cut threshold 0.45 is calibrated from one episode's published numbers** (non-cut ≤ 0.3705, cuts
   ≥ 0.73). Ambiguous scores are treated as cuts, so errors are on the safe side (a hold instead of a
   RIFE across a cut), but too many false "cuts" means more held frames than needed. **Recommend:** the
   audit already stores every flank score; after three real episodes, refit from the stored rows.
5. **Adjacent PAD repairs abort the whole episode.** `padfix_script` refuses PADs one frame apart
   (a neighbour would itself be a PAD). It is rare but fatal for an unattended run. **Recommend:** treat
   a run of adjacent PADs as one gap: RIFE at t = 1/3 and 2/3 between the real neighbours (non-cut), or
   hold (cut), instead of raising.
6. **Encode recipe is v38's, never A/B'd.** CRF 14 / slow / `tune=grain` is defensible for noisy LD
   material, but "quality first, time irrelevant" allows `preset=veryslow`, `rd`/`rdoq` and
   `psy-rd`/`psy-rdoq` tuning, and 2-pass is pointless for CRF. **Recommend:** an offline experiment on
   three 30 s real chunks measuring VMAF/SSIM-vs-lossless against bitrate, then freeze the winner as the
   recipe (still one final encode). Needs the owner's machine.

### B. Reliability / automation

7. **Nothing has run end to end on Windows.** First runs owed: video-trim/FstkN survival through the
   real TFM+TDecimate chain, `vs.scan` and `vs.frames` (new this session), RIFE, real 5.1 render,
   full delivery plan, live Archive catalog refresh. These are the highest-probability sources of a first
   failure. **Recommend a "smoke episode" command** (`fstk smoke`) that runs the whole plan on a
   30-second real excerpt in minutes and prints which seam broke.
8. **Golden-media tests are synthetic.** The tests prove logic, not tool behaviour. **Recommend:** commit
   a tiny real excerpt (a few seconds of LD stack master crop + its DVD frames, a few MB, git-lfs or
   outside the repo) and use it for the cadence, PAD scan and borrow tests.
9. **A/V check is the only review, but nothing pre-screens it.** Audio sync correlation against the DVD is
   already measured (`qc.av_*`). **Recommend:** if every chunk's measured offset is within, say, ±20 ms
   *and* the slope is flat, surface "auto-confident" in the review and pre-select "in sync" — the human
   still confirms, but the common case becomes a glance. No-DVD episodes keep the full check.
10. **Space ledger vs padfix payload.** The PAD work area (`probe/pad-repair-*`) holds PNGs plus the payload
    clip (tens of MB). Negligible, but it is the one new heavy-ish write not covered by a `SpaceNeed`.
    **Recommend:** declare it (small, but the invariant is "every write").
11. **Stale-artifact guard now exists for padfix; not for others.** `encode-final` refuses a padfix built
    from an older base script. The same pattern (hash in the artifact header) would protect `ld_synced`
    vs `video.frames`/`head_trim` changes. **Recommend:** generalise the check.

### C. Process

12. **`pad-audit` scans the full IVTC chain (QTGMC ranges included).** Correct (it must see what will be
    encoded) and CPU time is irrelevant, but it doubles decode time. If a real run shows hours, cache the
    scan by script hash (the report already has it).
13. **Docs.** `stage-reference.md` should list `pad-audit`, `pad-repair` and the proxy step; regenerate
    from the stage registry (it is derivable) in CI instead of by hand.

## 3. Recommended work, in order

| # | Item | Why | Needs owner machine? |
|---|---|---|---|
| E1 | DVD-borrow self-test on real non-PAD cuts + data-fit gate | measures the one unvalidated algorithm | partly (real DVD) |
| E2 | Chroma 4:2:2→4:2:0 in zimg, 16-bit PAD path | removes two silent precision losses | no |
| E3 | Adjacent-PAD gap handling | removes a hard stop | no |
| E4 | `fstk smoke` 30 s excerpt run | finds the first Windows failure fast | yes |
| E5 | Encode A/B (VMAF/SSIM) → frozen recipe | only way to justify the recipe | yes |
| E6 | A/V pre-screen from measured offsets | shrinks the one human step | no |
| E7 | Generalised stale-artifact headers; auto stage reference | hygiene | no |

E2, E3, E6, E7 can be done without your machine; I can start them next. E1, E4, E5 need real media.

## 4. What I would not change

Fail-closed gates, the manifest journal, one final encode, the 10-bit Main10 target, exact-ratio audio
resampling, the act-model DVD alignment, and the "hold rather than guess" fallbacks: each has a test and
a recorded rationale, and none showed a quality or reliability problem in this audit.
