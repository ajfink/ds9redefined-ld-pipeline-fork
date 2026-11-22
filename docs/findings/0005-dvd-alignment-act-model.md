# Finding 0005 — DVD↔LD offset is not constant between act breaks (S01E20)

Date: 2026-11-22 · Status: **model changed and validated on S01E20's real descriptors; auto-accepted**

## What was measured
fstk's alignment was run on the descriptors behind S01E20's owner-approved v38 alignment
(`Z:\ld-pipeline-device\probe\S01E20-*-desc-20261121T002523797121Z-437553a2.npy`; master
83,335 frames, DVD 81,844).

* **The offset drifts within acts.** Inside each act the DVD−LD offset changes steadily: the
  transfers run at different speeds. A shared slope across all acts fits **−988 ppm**, i.e.
  1000:1001 to within about 1%. Per-act slopes range from −560 to −1720 ppm, so the speed also
  wanders. v38's integer path steps through this in 1-frame increments. With a constant-offset
  segment model the first fstk port found 362 "segments" and only 30–50% exact frame matches.
* **Neighbouring offsets often tie.** The DVD is soft-telecined (23.976 film carried as 29.97),
  so 3:2 duplicates and static shots match equally well one or more frames apart. Short
  excursions of the path (for example 762–767 s) are this ambiguity, not different content.
* **The act breaks** are offset jumps of +5 at 283 s, +6.4 at 664 s, −4.1 at 971 s,
  +15.5 at 1452–1455 s, +15.1 at 1800–1803 s and −3 at 1953 s. They are where v38's diagnostics
  showed low-NCC spans.
* **v38's path had an error** at 347–350 s: an excursion to −1501 (≈ 38 frames off) with a
  negative margin, which the visual review passed. fstk's act curve does not make that excursion.

## Model now used (`fstk.media.alignment`)
1. Forward Viterbi path (linear-time, with an unmatched state); sub-frame offsets by parabolic
   peak interpolation.
2. **Breaks:** detrended offset jumps of ≥ 2.5 frames that persist 20 s on both sides. Each
   break is then moved to the split that best separates the two neighbouring curves.
3. **Acts:** a continuous piecewise-linear curve per act, with knots every 30 s and a Huber
   robust fit with a second-difference penalty. This follows speed wander and stays continuous
   within the act.
4. **Acceptance (all automatic):**
   - ≥ 98% of textured master frames have their best DVD match (±8 frames) within 0.02 NCC of
     the curve's (±1);
   - every act of 20 s or more scores ≥ 95% on the same test;
   - no stretch of ≥ 3 s fails it;
   - ≥ 95% of the reverse path's (DVD→LD) samples land within ±1.5 frames of the curves;
   - act slopes are within 2,500 ppm of the common slope;
   - no more than 10 s of interior LD picture lacks a DVD match;
   - nothing sits on the search edge.

## S01E20 result
| | v38 approved | fstk |
|---|---|---|
| Origin (first matched master frame) | not measured (sampling started at frame 3000) | **1515** (= v38's 50.5505 s anchor) |
| Agreement with v38 path | — | 97.5% within ±1 frame; 64.6% exact (curve vs staircase) |
| Frame-by-frame consistency | — | **99.69%** (worst act 99.5%) |
| Reverse agreement | — | 96.5% |
| Decision | visual approval | **AUTO-ACCEPTED** |

Robustness check: the DVD was decoded again with a different FFmpeg build (4.4 on the Linux
VM). The frame count was the same (81,844), but the soft-telecine duplicate sat one frame
apart from the Windows build in ~13% of rows. Result: AUTO-ACCEPTED again, consistency 99.73%,
same origin, same act breaks, plus one extra small break (−2.8 at 331 s). Reverse agreement was
95.5%, close to its threshold.

## Consequences / open points
* `align_npy` now holds the curve rounded to whole frames, not the raw staircase. The raw path,
  the sub-frame offsets and the model offset per sample are in the TSV.
* Breaks of under 4 frames sit near the detection threshold: the 331 s break appears with one
  FFmpeg build and not the other. Retime must not treat a small break as an edit without
  measuring it on the audio (break-edge measurement).
* The ~1000 ppm speed difference is the picture-side counterpart of the DVD-referenced audio
  rate. The audio-rate stage should agree with it, and that makes a cross-check gate.
* The thresholds are calibrated on one episode. Run `tools/validate_v38_alignment.py` on S01E19
  and later episodes (needs their stack masters' descriptors) before trusting them widely.
