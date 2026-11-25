# ADR 0006 — The programme start is the DVD's opening run, found on the LD; packs ship it for people without the DVD

Date: 2026-11-25 · Status: accepted (owner: "a quick analysis of the first 10-15 seconds of the DVD episode, then look
for those matching frames in the LD"; "package up some initial frames from the DVD releases … for people who DON'T
have the DVDs")

## Context
The head trim needs the LD frame where the programme starts. Frame-by-frame matching let the LD's warning cards
match DVD pictures loosely (S02E02: origin 4.0 s instead of 49.5 s, CP63). Without a DVD, the start was the fade-up
nearest v38's prior, which is an estimate.

## Decision
1. The start is found as a **sequence**: the DVD's first textured run (12 s; 20 or 30 s when it barely changes over
   half a second) slid over the LD's first minutes, accepted only at mean NCC ≥ 0.80 and ≥ 0.15 above the best score
   outside the match's own peak (`alignment.find_opening`, `OPENING`).
2. That run, as alignment descriptors only (48×36 grey, not viewable video), is packaged per episode with the app
   (`fstk/data/dvd_openings/<series>/<code>.npz`, `fstk openings export`). Without a DVD, `video-trim` uses the
   pack; a pack that does not match clearly falls back to the fade-up rule, said in the report.
3. A pack records its DVD start frame, run length, ffmpeg build and distinctiveness; export refuses a run that scores
   above 0.65 elsewhere in its own DVD's first minutes.

## Consequences
- The programme start is identified, not estimated, for every packaged episode, DVD or not.
- Packs are ~0.2–0.7 MB each (DS9 S1–S2D1: 23 packs, 7.3 MB). Different ffmpeg builds differ on soft-pulldown
  frames only; the sequence match is unaffected (measured on S01E20 with three builds).
- Packs for further episodes are made with `fstk openings export EPISODE --dvd FILE` by anyone with the disc.
