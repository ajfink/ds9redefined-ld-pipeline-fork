# Pending core changes (applied 2026-11-21 at the owner's request; version NOT incremented)

Source: findings from the fstk rebuild (`Z:\FromStunToKill\docs\findings\`). These are core
workflow changes under `WORKFLOW_VERSIONING.md`; `WORKFLOW_VERSION` was deliberately left at 38
so the owner can fold them into the next increment and record `FROM Version 38 -> Version 39`.

## 1. Window-centre audio-rate origin (fstk finding 0001)
* **Files:** `scripts/measure_audio_rate.py`, `tests/test_audio_rate_centre_reference.py` (new).
* **Reason:** each 50 s correlation window was referenced at its start. Correlating a window
  between clocks differing by r ppm aligns window *centres*, so start-referenced pairs bias the
  fitted intercept (A/V origin) by ≈ r × 25 s (+21.6 ms at 864 ppm) while the slope is unchanged.
* **Change (additive, report-only):** the window TSV gains `master_equivalent_centre_time_s`
  (the matched DVD-audio window centre mapped through the same picture map); the summary JSON
  (live and `summarize_saved_report`) gains a `centre_referenced` block with OLS/Theil–Sen fits of
  centre pairs. Older TSVs without the column get a labelled approximation (`exact: false`) via
  the picture-map slope. Existing fields, columns and their meanings are unchanged; nothing
  approves or writes `ld_corrected`/`skip_s`.
* **Validation:** new tests (exact column, approximation path, mask handling); full non-GUI
  suite in the fstk container: 515 passed, 11 skipped (Tkinter modules not collectable there).
  No real-media run: no saved audio-rate TSV exists on the device yet.

## 2. Windows map: explicit tail trim (fstk finding 0002)
* **File:** `scripts/build_map.ps1`.
* **Reason:** AviSynth `Trim(first, -n)` returns n frames, so `.Trim($TrimStart, -$TrimEnd)` kept
  only `$TrimEnd` frames whenever `video.tail_trim > 0`. All delivered/worked examples used
  `tail_trim = 0`, so no shipped output was affected.
* **Change:** decimated clip is named `dec`; output is `dec.Trim($TrimStart, dec.FrameCount - 1 - $TrimEnd)`;
  when the manifest has a measured `video.frames`, the generated script also asserts
  `out.FrameCount == video.frames`.
* **Validation:** reviewed by inspection only — PowerShell/AviSynth were not available in the
  container. **Owner: run `build_map.ps1 -Episode <code> -DryRun`, then open the generated .avs
  once (e.g. in AvsPmod or with ffprobe) on an episode with tail_trim > 0 before relying on it.**
