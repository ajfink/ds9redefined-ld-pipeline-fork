# ADR 0004 — Orc's CLV time-code PAD frames are removed from the stack master

Date: 2026-11-24 · Status: accepted (owner decision: "Drop the timecode pads and simplify the whole app so that we
no longer need the padding functionality")

## Context
The S02E03 DVD alignment drifted by one frame at every multiple of 900 master frames. Dense per-frame matching
showed the LD→DVD offset flat between those seams and slipping by −0.94 frames at each one; the DVD itself was
clean. The cause is Orc (decode-orc 2.11.1), not the disc:

- Orc builds each capture's frame map from the CLV time code, counted at 30 fps. NTSC is 29.97 fps, so the time
  code skips one number per 900 frames (every 10th skip absent: none at 9000, 18000, …). Orc's documented rule
  is "if PN jumps forward: insert PAD frames", so every skip becomes a `PAD_1` in the frame map.
- Rendered, a PAD is an all-zero frame (Y = U = V = 0). On the S01E20 master a PAD packet is 7,664 bytes against
  a median of about 445 KB. The S02E03 master has 84 of them: 83 on the 900-frame grid, one at 57,653.
- The LD frames either side of a PAD are consecutive DVD frames: the PAD is inserted, not a missing picture, and
  it shifts everything after it by one frame.
- Every capture's map has the same PADs at the same aligned positions (the `alignmentMap` gives each capture its
  lead-in offset). On the real S01E20 project, 83 are removed per capture; a few captures keep 1–4 irregular PADs
  of their own (L1 1, L2 1, L3 4, P3 2, P4 1, P5 1), which are real gaps in that capture and stay.

v38 treated the PADs as damage: it stretched the no-DVD audio by about 0.1 % (E07: 44,056 Hz) so the audio
covered them, and invented a picture for each one (RIFE midpoint, a DVD frame borrowed on cuts, or a held frame),
with an audit, a repair stage, an identity QC gate and a PAD-aligned black-frame gate around it.

## Decision
1. **The stack master has no time-code PADs.** `stack-reference` removes the 900-frame-grid PADs from every
   capture's frame map (`orc_project.strip_timecode_pads`) before Orc renders, and records how many it removed.
   It fails closed if only some captures have the grid, if a grid gap is not 900 or 1800 frames, or if the
   captures' PADs do not line up after their alignment offsets. Irregular PADs are kept.
2. **Existing masters are migrated losslessly.** `stack-render` reusing a master that still has PADs drops
   exactly those frames by stream copy (FFV1 is intra-only), after checking that every packet is a keyframe,
   timestamps rise, each PAD packet is tiny and each PAD frame decodes to all zeros. The copy's remaining packets
   must equal the originals byte for byte. The original is kept as `<master>-with-timecode-pads.mkv` for the
   owner to delete; nothing is deleted automatically.
3. **No-DVD audio plays at the disc's own rate.** Picture and audio come from one disc clock, so the EFM audio
   is used at 44,100 Hz (plus any reviewer drift correction). The audio vs stack duration check stays, at
   ±2 frames: measured +0.32 frames (E06) and +1.32 frames (E07) against the pad-free stack. New reports carry
   `audio.rate_method = dvd-free-native-rate`; QC still verifies deliveries made with v38's recipe
   (`dvd-free-duration-pad`) under that recipe's own rule.
4. **The PAD machinery is gone**: the `pad-audit` and `pad-repair` stages, RIFE (engine, model, installer,
   smoke stand-in), the replacement-identity QC gate and the PAD-aligned black gate. QC's decode gate now
   *warns* about any single black frame and opens a review interval for it; a picture missing from every
   capture is fixed in Frame fixes (interpolate, region patch or DVD borrow — the v38 DVD-borrow method
   survives there as `media/frame_replace.py`).
5. `audio.pad_frames` (the selected capture's lead-in offset in the alignment map) is a different thing and
   stays. Manifest fields `pads.*`, `qc.pad_aligned_black_hits` and `qc.identity_*` stay registered so older
   manifests still read; nothing writes them.

## Consequences
- Every downstream step of an episode whose master had PADs re-runs once (its inputs changed). The migration
  needs free space equal to the master's size while the copy is written.
- Episodes delivered by v38 contain invented frames at the PAD positions and audio stretched about 0.1 %. They are
  not changed by this decision; re-running them through fstk produces the corrected delivery.
- One fewer optional tool on every machine (RIFE), one fewer pinned output-affecting component (the RIFE model).
- This reverses the v38 PAD rule; the v38 reference tree stays frozen and the characterization tests that
  encoded the PAD model were rewritten, each marked as a deliberate change.
