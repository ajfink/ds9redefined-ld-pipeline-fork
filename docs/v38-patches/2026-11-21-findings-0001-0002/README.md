# v38 patch — findings 0001 and 0002 (2026-11-21)

Applies the recommendations you approved to the v38 line. Not applied automatically (writing
into the live v38 tree was blocked), so apply it yourself:

* `v38.patch` — unified diff against v38 at `WORKFLOW_VERSION` 38 (`git apply v38.patch` from
  `Z:\fromstuntokill-v15-app`; CRLF/LF endings preserved per line). Or copy the files in `files/`:
  - `files/build_map.ps1` → `scripts\build_map.ps1`
  - `files/measure_audio_rate.py` → `scripts\measure_audio_rate.py`
  - `files/test_audio_rate_centre_reference.py` → `tests\`
  - `files/PENDING-CORE-CHANGES-2026-11-21.md` → `Versioning\` (what changed, why, validation)

Validated against an unmodified copy of v38 (`build_map.ps1` and `measure_audio_rate.py` were
identical to the snapshot when staged): full non-GUI suite 515 passed / 11 skipped (= 512 + 3 new).
`build_map.ps1` was checked by inspection only — run a `-DryRun` and open one generated .avs on an
episode with `tail_trim > 0` before relying on it. `WORKFLOW_VERSION` is not bumped.
