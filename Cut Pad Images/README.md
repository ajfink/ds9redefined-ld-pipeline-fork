# Curated cut-PAD stills

These are lossless, single-frame PNG repair aids for confirmed one-frame black
PADs that coincide with an LD shot cut. They are not substitutes for the DVD:
they provide no DVD audio, subtitles, picture map, act-break schedule, or general
episode reference. Use an image only after independently confirming that its
scene/timestamp corresponds to the particular cut PAD; otherwise hold the prior
native LD frame and do not interpolate across the cut.

## Filename convention

`DVD-HH-MM-SS.mmm-fNNNNNNNN.png` records the DVD timeline timestamp and decoded
DVD frame index. The timestamp comes from a validated sequential decode/timeline,
not a raw non-monotonic packet PTS or an unsafe `-ss` seek. The frame index is
retained because millisecond timestamps can collide after rounding.

## Included verified images

| Episode | DVD timestamp / decoded frame | LD output cut PAD | Source DVD | PNG SHA-256 | Processing |
|---|---|---:|---|---|---|
| S01E04 — A Man Alone | 00:18:14.191 / 26234 | 26160 | `Deep Space 9 - S01E04 - A Man Alone.mkv`; SHA-256 `7ea99ba59ab1815125389b3cbd3852a2a94b92e1818c3b59e192e28fbed07969` | `7ed02c9d9a4e76982b9ba116300be4abc49cb90f25754b93ca95ae04f369f1ee` | `DVD-00-18-14.191-f00026234.png`; cropped/scaled to 768x484 and stable-background color-matched to the preceding LD frame; verified as the last A-scene frame before the cut. |
| S01E07 — Q-Less | 00:12:42.762 / 18288 | 18222 | `Deep Space 9 - S01E07 - Q-Less.mkv`; SHA-256 `62833fb418ed3080cd4a8ab5bb342601ced9a59d527727d806fa846662584d74` | `ebcbbf9afdc7c8303450ea28d2441fa29b98d8065e08aaa50a3981619eec7620` | `DVD-00-12-42.762-f00018288.png`; cropped/scaled to 768x484 and stable-background color-matched; scene/cut timing and delivered-frame identity verified in the S01E07 V5 rerun log. |

The source DVDs remain external and are not included. All DVD decoding used for
the source extraction was sequential; the stills are the only media files in this
curated directory. Verify each PNG's hash before using or republishing it.
