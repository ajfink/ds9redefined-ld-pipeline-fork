# Project re-audit 2 — quality layer (2026-11-22)

Scope: `video-quality`, `audio-quality`, `episode-report` and the pieces they touch. Same caveat as AUDIT-1: verified means
automated tests on synthetic/small real media (real VapourSynth and ffmpeg on the device VM); nothing here has run on a
full real episode with QTGMC, RIFE and Orc.

## Grain safeguards (the owner's paramount rule)
1. Dropout concealment copies **real pixels from the previous/next LD frame**, never synthesised or DVD pixels, and only when the
   surrounding ring matches within the grain-expected cost and the patch/ring grain-std ratio is within (0.7, 1.4). Unaccepted
   defects are reported as "left as captured".
2. Chroma denoise is chroma-plane only and runs only when the measured chroma noise exceeds the trigger; `_prove` shows luma is
   bit-identical to the pre-finish chain outside concealed patches and that grain retention on quiet frames is unchanged.
3. No luma NR exists in the chain. The previous halo/sharpness heuristics were replaced by the grain-retention measure
   (residual std after Gaussian blur in flat regions, before vs after).
4. Audio repair is limited to short gaps/clicks the signal demonstrably continues past; everything else is bit-identical.

## Open risks
- **Unvalidated on real hardware:** QTGMC needs `resize2` (absent on the VM), real RIFE, Orc, production `slow` preset; thresholds
  (`CHROMA_NOISE_TRIGGER`, dropout ring cost, MAX_DEFECT_HEIGHT) were tuned on synthetic and a 100 s excerpt only.
- **Hybrid delivery not built:** classification, segments and Matroska v2 timecodes exist; muxing needs mkvmerge and QC gates assume CFR.
- Dropout detection is single-frame-defect oriented; multi-frame damage is reported, not repaired.
- LPC fill cannot restore bursts longer than a few ms; these are reported with times for the A/V reviewer.
- Colour-tag check covers the delivered video stream only; the HDR/range of the 5.1 DVD path is out of scope.
