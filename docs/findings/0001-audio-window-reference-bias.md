# Finding 0001 — window-start referencing biases the audio-rate intercept

Date: 2026-11-21 · Status: **decided 2026-11-21 — window-centre convention + cross-method agreement (tolerance provisional)** · Evidence: `tests/unit/test_audio_rate_method.py`, `tests/unit/test_audio_origin.py`

## What was found
v38 `measure_audio_rate.py` correlates a 50 s LD-audio envelope window against the DVD-audio
envelope and records each match as (`wav_start`, `dvd_audio_start`) — the **start** of each
window. When the two clocks differ by a rate error `r` (ppm), the correlation of a stretched
window peaks where the window *centres* coincide, so a start-referenced pair is offset by
about `r × W/2` (W = 50 s). That offset is constant across windows, so:

* the fitted **slope (rate) is unaffected**;
* the fitted **intercept (A/V origin) is biased** by ≈ `r × 25 s`.

Synthetic end-to-end measurement (envelope → sliding correlation → parabolic refinement →
Theil–Sen), true offset 0.37 s, 1500 s of audio, windows every 120 s:

| true drift | intercept error, window **start** | intercept error, window **centre** | slope error |
|---|---|---|---|
| 0 ppm | −0.7 ms | −0.7 ms | −0.1 ppm |
| +300 ppm | +8.0 ms | +0.5 ms | +0.3 ppm |
| +864 ppm (≈ E07) | **+26.1 ms** | +4.6 ms | −5.6 ppm |
| −500 ppm | −12.0 ms | +0.5 ms | +0.3 ppm |
| +1500 ppm | **+35.6 ms** | −2.0 ms | +2.8 ppm |

## Why it matters
v38 keeps DVD-backed `ld_corrected`, `audio.skip_s`, and `ld_synced` uncommitted "pending
reconciliation of the exact origin convention" (Versioning/NEXT-APP-RELEASE.md). A drift-dependent
origin error of 10–40 ms is exactly the size of discrepancy that would make start-referenced
intercepts disagree with fade-midpoint and duration-identity origins. PORTING §5 also records
S01E05 measuring 87 ms late from a one-frame convention error; this is a separate, smaller,
rate-proportional term.

## Decision (owner, 2026-11-21)

* **Convention:** window-centre pairs everywhere (`fstk.media.audio_origin.CONVENTION`).
* **Agreement gate:** an A/V origin is committed only when independent estimates (centre-referenced
  rate-fit intercept, fade-midpoint intercept, no-DVD duration/PAD identity) agree within
  `ORIGIN_AGREEMENT_S` = ¼ output frame ≈ 10.4 ms. **Provisional** until calibrated on S01E20's saved
  window data and E07 — record the calibration here when done. Disagreement → A/V review clips;
  nothing is chosen automatically.
* **v38:** `docs/v38-patches/2026-11-21-findings-0001-0002/` adds an exact centre-referenced fit
  block to `measure_audio_rate.py` reports (additive; existing fields unchanged). Writing into the
  live v38 tree was blocked, so the owner applies it. Nothing delivered used the biased intercepts
  (DVD-backed correction was always blocked; E07/E10 used the duration/PAD route).

## Original proposal
* fstk's measurement stage records **window-centre** pairs (`wav_start + W/2`,
  `dvd_audio_start + W/2`); the start columns stay in the report for comparison with v38.
* The intercept is reported with this convention named explicitly; it remains evidence, not an
  approval (no threshold is invented).
* Before anything is committed, re-run S01E20's saved window TSV both ways and compare against
  the fade-midpoint fit intercept (`fstk.media.fade_fit`), which uses midpoints and is not
  affected.

The ported v38 functions are unchanged; `summarize_saved_report` still reproduces v38 numbers.
