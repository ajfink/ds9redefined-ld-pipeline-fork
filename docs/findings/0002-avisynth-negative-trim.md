# Finding 0002 — v38 Windows chain: `Trim(start, -tail)` keeps *tail* frames, not "all but tail"

Date: 2026-11-21 · Status: **confirmed by documentation; avoided in fstk; v38 fix prepared (owner applies)**

## What was found
v38 `scripts/build_map.ps1` writes the final AviSynth line

    out = fm.TDecimate(...).Trim($TrimStart, $avsTrimEnd)    # $avsTrimEnd = -$TrimEnd when TrimEnd > 0

intending "drop `tail_trim` frames from the end". In AviSynth a negative second argument is a
**frame count**: `Trim(first, -n)` returns `n` frames starting at `first` (AviSynth+ docs,
corefilters/trim). With `video.tail_trim = 12`, the Windows encode script would therefore
yield a 12-frame clip.

v38's macOS port (`build_map_vpy.py`) implements the intended behaviour
(`std.Trim(out, TRIM_START, out.num_frames - TRIM_END)`), although its comment repeats the
incorrect AviSynth reading.

## Impact
* Any Windows episode with `tail_trim > 0` would fail v38's `video.frames`/QC gates (so no bad
  deliverable should have shipped), but the Windows path could not complete such an episode.
* Episodes delivered with `tail_trim = 0` are unaffected (`Trim(start, 0)` = to end).

## In fstk
AviSynth is retired (REFACTOR-PLAN §7.3). `fstk.media.cadence_map.ivtc_vpy_script` keeps the
VapourSynth trim and asserts the expected output frame count inside the generated script, so a
trim error fails at script evaluation instead of at QC. Test:
`tests/unit/test_cadence_map.py::test_tail_trim_drops_frames_from_end`.

## v38 fix (owner approved 2026-11-21)

Prepared in `docs/v38-patches/2026-11-21-findings-0001-0002/` (applies cleanly to v38 at
`WORKFLOW_VERSION` 38; inspected, not executed — no PowerShell/AviSynth in the dev container):
`dec = fm.TDecimate(...)`, `out = dec.Trim($TrimStart, dec.FrameCount - 1 - $TrimEnd)`, plus an
`Assert(out.FrameCount == video.frames)` when the manifest has a measured frame count.
All delivered/worked v38 examples used `tail_trim = 0`, so nothing shipped was affected.

### Original suggestion
`build_map.ps1`: replace the negative form with an explicit end frame, e.g.
`.Trim($TrimStart, $fm_decimated.FrameCount - 1 - $TrimEnd)`, or use the named `end=` form.
