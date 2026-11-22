# Finding 0004 — Choosing the EFM capture automatically when none wins on every measure

Date: 2026-11-21 · Status: **implemented (`least-audible-damage-v1`); S01E20 pick differs from v38's manual pick**

## Background
v38 picked a capture automatically only when one was no worse on all six Orc EFM measures and
strictly better on one (unique Pareto). Otherwise an operator chose. The owner's decision
(2026-11-21): no human EFM review; when no capture is best on every measure, fstk picks the
least-bad one automatically.

## What the measures mean
Orc's audio stage (ld-decode's `efm-decoder-audio` lineage) handles samples that C1/C2 cannot
correct in one of two ways:

* **Concealed**: replaced by the average of the neighbouring good samples. An isolated
  interpolated sample is very rarely audible.
* **Silenced ("muted")**: set to zero amplitude. A run of silenced samples is a dropout and is
  audible.

Missing sections are whole 1/75-second blocks that never decoded. Post-C2 loss, C2 and C1 are
error-correction rates: they predict damage that is otherwise hidden.

## The rule
1. If a unique Pareto winner exists, take it (unchanged from v38).
2. Otherwise, consider only the Pareto front, i.e. captures that no other capture beats on
   every measure.
3. Among the front, compare these measures in order and take the first difference:
   silenced samples, missing sections, concealed samples, post-C2 loss, C2, then C1.
4. If any report is missing a measure, stop: report parsing fails closed, so this means a broken
   decode.

The note recorded with the selection names the front and lists the decisive differences.
An operator can still override with a note (EFM capture review window), but nothing waits on
one.

## S01E20 (real Orc 2.11.1 data, 12 captures)
* Pareto front: M1, P2, P6.
* **M1** is chosen. Compared with P2 it has 23,472 vs 26,184 silenced samples, 6 vs 9 missing
  sections, and lower post-C2, C2 and C1 rates. P2 has fewer interpolated samples (756 vs
  1,176).
* On v38 the owner picked **P2** manually ("fewest concealed, low C1 and C2 errors"). Under this
  rule P2 comes second: it has 2,712 more silenced samples (≈ 31 ms of single-channel
  audio) and 3 more missing sections, against 420 fewer interpolated samples.
* `audio.pad_frames` follows the capture. The saved alignment map
  (`1+0, 2+16, 3+23, 4+23, 5+18, 6+118, 7+118, …` in Orc source order P6, L1, L2, L3, M1, P1, P2, …)
  gives **M1 = 18** and P2 = 118. Changing the capture changes every downstream audio timing
  value: rate, skip and A/V offsets are re-measured, never carried over.

## Better than choosing: multi-capture repair (proposed)
The counts can't show *where* the damage is. Two captures with similar totals can differ a lot
in how audible they are: silence inside dialogue versus silence over a fade to black.
`efm-decoder-audio` can write per-region flags of concealed and silenced samples
(`--audacity-labels`). Given those flags and the 12 decodes of the same programme, fstk could:

1. align every decode to the chosen one, sample-accurately by cross-correlation per chunk;
2. for each flagged region in the chosen capture, substitute the samples from another capture
   whose same region is unflagged and agrees with a third capture;
3. record every substitution as evidence.

The result would have close to zero concealed or silenced samples rather than "least bad". It
needs (a) Orc's EFMSink to expose the flag output (to be confirmed on the Orc CLI), and (b) the
decoded WAVs on a machine fstk can read.
